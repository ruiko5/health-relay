"""HealthRelay MVP — 接收 Apple 健康数据 → 存储 → 给手机浏览器看的极简看板。

单文件后端：
  POST /ingest        接收 webhook（Health Auto Export / 快捷指令）
  GET  /api/summary   汇总最近 N 天
  GET  /api/raw       最近的原始样本（调试用）
  GET  /              前端页面
"""
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DB_PATH = DATA_DIR / "health.db"
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "").strip()   # 上报口令，留空则不校验
VIEW_TOKEN = os.getenv("VIEW_TOKEN", "").strip()       # 查看口令，留空则公开

BASE = Path(__file__).parent
DAY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
ALIAS = {
    "step_count": "steps",
    "steps": "steps",
    "heart_rate": "heart_rate",
    "resting_heart_rate": "resting_hr",
    "sleep_analysis": "sleep",
}
app = FastAPI(title="HealthRelay MVP")


# ---------------------------------------------------------------- storage
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # 建表、迁移、建索引放在一个事务里：中途异常会整体回滚，不会留下半迁移的库
    with conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS samples(
                   id     INTEGER PRIMARY KEY AUTOINCREMENT,
                   metric TEXT NOT NULL,
                   day    TEXT NOT NULL,
                   ts     TEXT NOT NULL,
                   value  REAL,
                   extra  TEXT,
                   recv   REAL)"""
        )
        # 同一「指标 + 时刻」只保留最后一次上报（Health Auto Export 会把最近 7 天重叠重发，
        # 而且当天还没过完时同一个时间戳的累计值会不断变大 —— 后写覆盖才不会把步数累加成两倍）
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if ver < 2:
            # 旧库：唯一索引含 value、可能已有同一时刻的多行 → 先去重（保留最新一行）
            conn.execute("DROP INDEX IF EXISTS uq_sample")
            conn.execute(
                "DELETE FROM samples WHERE id NOT IN (SELECT MAX(id) FROM samples GROUP BY metric, ts)"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_sample ON samples(metric, ts)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_day ON samples(metric, day)")
        if ver < 2:
            conn.execute("PRAGMA user_version = 2")
    return conn


# ---------------------------------------------------------------- parsing
def today() -> str:
    return time.strftime("%Y-%m-%d")


def _day_of(ts: str) -> str:
    m = DAY_RE.search(str(ts or ""))
    return m.group(1) if m else today()


def _num(v):
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def parse(payload):
    """兼容三种格式：Health Auto Export、扁平 JSON、数组。返回 [{metric, day, ts, value, extra}]"""
    out = []

    def add(name, ts, value, extra=None):
        v = _num(value)
        if v is None:
            return
        metric = ALIAS.get(str(name).lower().strip(), str(name).lower().strip())
        out.append(
            {
                "metric": metric,
                "day": _day_of(ts),
                "ts": str(ts or f"{today()} 00:00:00 +0800"),
                "value": v,
                "extra": json.dumps(extra, ensure_ascii=False) if extra else None,
            }
        )

    # 格式 1：{"data": {"metrics": [{"name","units","data":[{...}]}]}}
    metrics = []
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), dict):
            metrics = payload["data"].get("metrics") or []
        elif isinstance(payload.get("metrics"), list):
            metrics = payload["metrics"]

    for m in metrics:
        if not isinstance(m, dict):
            continue
        name, rows = m.get("name"), m.get("data") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ts = row.get("date") or row.get("start") or row.get("end")
            if name == "sleep_analysis":
                # totalSleep 单位为小时（聚合导出）；否则用 end-start
                v = row.get("totalSleep")
                if v is None:
                    v = row.get("asleep")
                if v is None and row.get("sleepStart") and row.get("sleepEnd"):
                    v = _hours(row["sleepStart"], row["sleepEnd"])
                add("sleep", ts, v, {"stages": {k: row.get(k) for k in ("deep", "core", "rem")}})
            elif "Avg" in row or "Min" in row or "Max" in row:
                add(name, ts, row.get("Avg", row.get("qty")), {"min": row.get("Min"), "max": row.get("Max")})
            else:
                add(name, ts, row.get("qty", row.get("value")))

    # 格式 2：扁平 {"date": "...", "heart_rate": 62, "steps": 8000, "sleep_hours": 7.5}
    if not metrics and isinstance(payload, dict):
        ts = payload.get("date") or payload.get("ts") or f"{today()} 12:00:00 +0800"
        flat = {"sleep_hours": "sleep", "sleep": "sleep", "steps": "steps",
                "heart_rate": "heart_rate", "hr": "heart_rate", "resting_hr": "resting_hr"}
        for k, v in payload.items():
            if k == "sleep_minutes":  # 快捷指令里算出来的是分钟
                add("sleep", ts, _num(v) / 60 if _num(v) is not None else None)
            elif k in flat:
                add(flat[k], ts, v)

    # 格式 3：[{"metric": "steps", "value": 8000, "date": "..."}]
    if not out and isinstance(payload, list):
        for row in payload:
            if isinstance(row, dict) and "metric" in row:
                add(row["metric"], row.get("date") or row.get("start"), row.get("value", row.get("qty")))

    return out


def _hours(start, end):
    def p(s):
        for f in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return time.mktime(time.strptime(str(s).strip(), f)) + time.timezone
            except ValueError:
                continue
        return None
    a, b = p(start), p(end)
    return round((b - a) / 3600, 2) if a and b and b > a else None


# ---------------------------------------------------------------- auth
def _got_token(request: Request) -> str:
    return (
        request.query_params.get("token")
        or request.headers.get("x-auth-token")
        or request.headers.get("x-api-key")
        or (request.headers.get("authorization", "").removeprefix("Bearer ").strip())
    ) or ""


def _mask(token: str) -> str:
    """只露前 4 位和后 2 位，用来排查空格/引号/长度不一致，不泄漏完整口令。"""
    if len(token) <= 8:
        return repr(token)
    return repr(token[:4] + "*" * (len(token) - 6) + token[-2:])


def _token_ok(request: Request, expect: str) -> bool:
    if not expect:
        return True
    return _got_token(request) == expect


# ---------------------------------------------------------------- routes
@app.post("/ingest")
async def ingest(request: Request):
    got = _got_token(request)
    if INGEST_TOKEN and got != INGEST_TOKEN:
        # 排查用：能看出客户端到底发了什么（带空格 / 带引号 / 长度不对）
        print(f"[auth] ingest 401 got={_mask(got)} len={len(got)} "
              f"expect_len={len(INGEST_TOKEN)} headers={sorted(request.headers.keys())}",
              flush=True)
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)

    samples = parse(payload)
    if not samples:
        # 没解析出样本时，把原始报文写进日志（Render 的 Logs 里能看到），
        # 方便换成别的上报 App 后照着它的真实格式补解析规则。
        try:
            raw = json.dumps(payload, ensure_ascii=False)
        except Exception:
            raw = str(payload)
        print(f"[ingest] unparsed payload ({len(raw)}B): {raw[:1200]}", flush=True)
    if samples:
        conn = db()
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO samples(metric,day,ts,value,extra,recv) VALUES(?,?,?,?,?,?)",
                [(s["metric"], s["day"], s["ts"], s["value"], s["extra"], time.time()) for s in samples],
            )
        conn.close()
    return {"ok": True, "stored": len(samples), "metrics": sorted({s["metric"] for s in samples})}


@app.get("/api/summary")
def summary(request: Request, days: int = 7):
    if not _token_ok(request, VIEW_TOKEN):
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    days = max(1, min(days, 60))
    conn = db()
    rows = conn.execute(
        "SELECT metric, day, SUM(value), AVG(value), MIN(value), MAX(value), COUNT(*) "
        "FROM samples GROUP BY metric, day"
    ).fetchall()
    updated = conn.execute("SELECT MAX(recv) FROM samples").fetchone()[0]
    # 心率样本若自带 Min/Max（Health Auto Export 的聚合格式），一并纳入区间
    ranges = {}
    for day, mn, mx in conn.execute(
        "SELECT day, MIN(CAST(json_extract(extra,'$.min') AS REAL)), "
        "MAX(CAST(json_extract(extra,'$.max') AS REAL)) "
        "FROM samples WHERE metric='heart_rate' AND extra IS NOT NULL GROUP BY day"
    ):
        ranges[day] = (mn, mx)
    conn.close()

    agg = {}
    for metric, day, total, avg, mn, mx, n in rows:
        agg.setdefault(day, {})[metric] = {"sum": total, "avg": avg, "min": mn, "max": mx, "n": n}
    for day, (mn, mx) in ranges.items():
        hr = agg.setdefault(day, {}).setdefault("heart_rate", {"sum": 0, "avg": None, "min": None, "max": None, "n": 0})
        if mn is not None:
            hr["min"] = mn if hr["min"] is None else min(hr["min"], mn)
        if mx is not None:
            hr["max"] = mx if hr["max"] is None else max(hr["max"], mx)

    # 补齐空白日期，前端画图不用处理空洞
    out, now = [], time.time()
    for i in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        m = agg.get(day, {})
        sleep = m.get("sleep")
        hr = m.get("heart_rate")
        rhr = m.get("resting_hr")
        steps = m.get("steps")
        out.append(
            {
                "day": day,
                # 睡眠用 SUM：Days 聚合时一天只有一行（等于最大/总和），
                # Hours 聚合或多段睡眠（夜睡 + 午睡）时求和才是真实的当日总睡眠
                "sleep_hours": round(sleep["sum"], 2) if sleep else None,
                "steps": int(steps["sum"]) if steps else None,
                "hr_avg": round(hr["avg"]) if hr else None,
                "hr_min": round(hr["min"]) if hr else None,
                "hr_max": round(hr["max"]) if hr else None,
                "resting_hr": round(rhr["avg"]) if rhr else None,
            }
        )
    return {
        "ok": True,
        "today": out[-1],
        "days": out,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated)) if updated else None,
    }


@app.get("/api/raw")
def raw(request: Request, limit: int = 30):
    if not _token_ok(request, VIEW_TOKEN):
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    conn = db()
    rows = conn.execute(
        "SELECT metric,day,ts,value,extra FROM samples ORDER BY id DESC LIMIT ?", (min(limit, 500),)
    ).fetchall()
    conn.close()
    keys = ("metric", "day", "ts", "value", "extra")
    return {"ok": True, "items": [dict(zip(keys, r)) for r in rows]}


@app.post("/api/demo")
def demo(request: Request):
    """灌 7 天假数据，方便先看效果：curl -X POST '.../api/demo?token=xxx'"""
    if not _token_ok(request, INGEST_TOKEN):
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    import random
    conn = db()
    with conn:
        for i in range(7):
            day = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            conn.execute("DELETE FROM samples WHERE day=?", (day,))
            conn.executemany(
                "INSERT OR REPLACE INTO samples(metric,day,ts,value,extra,recv) VALUES(?,?,?,?,?,?)",
                [
                    ("sleep", day, f"{day} 07:00:00 +0800", round(random.uniform(6.2, 8.6), 2), None, time.time()),
                    ("steps", day, f"{day} 22:00:00 +0800", random.randint(3200, 12000), None, time.time()),
                    ("heart_rate", day, f"{day} 12:00:00 +0800", random.randint(64, 82),
                     json.dumps({"min": random.randint(48, 56), "max": random.randint(110, 145)}), time.time()),
                    ("resting_hr", day, f"{day} 07:00:00 +0800", random.randint(52, 61), None, time.time()),
                ],
            )
    conn.close()
    return {"ok": True, "seeded_days": 7}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(BASE / "index.html", media_type="text/html; charset=utf-8")
