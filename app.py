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
import datetime
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

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
                start, end = row.get("sleepStart") or row.get("start"), row.get("sleepEnd") or row.get("end")
                if v is None and start and end:
                    v = _hours(start, end)
                # 阶段时长（深睡/核心/REM/清醒）留着给前端画睡眠结构
                stages = {k: row.get(k) for k in ("deep", "core", "rem", "awake", "inBed")}
                stages = {k: val for k, val in stages.items() if val is not None}
                extra = {}
                if stages:
                    extra["stages"] = stages
                if start and end:
                    extra["window"] = [str(start), str(end)]
                add("sleep", ts, v, extra or None)
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
def _clean(token: str) -> str:
    """容忍客户端常见的手误：前后空格、成对引号、误带 "Bearer " 前缀。"""
    t = (token or "").strip()
    for _ in range(2):
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            t = t[1:-1].strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def _got_token(request: Request) -> str:
    got = _clean(
        request.query_params.get("token")
        or request.headers.get("x-auth-token")
        or request.headers.get("x-api-key")
        or request.headers.get("authorization", "")
    )
    if got:
        return got
    # 兜底：有些 App 的「请求头」是两个格子（键/值），很容易把口令填进「键」里，
    # 于是发出来的请求头名字就是口令本身、值为空。这种情况也认，
    # 并在日志里提示一次正确的写法（口令本身仍是唯一凭据，安全性不变）。
    for name in request.headers.keys():
        v = _clean(name)
        if v and (v == INGEST_TOKEN or v == VIEW_TOKEN):
            print(f"[auth] 口令被误填到「键」栏（header 名={_mask(v)}），"
                  f"请改成 键 X-Auth-Token / 值 {_mask(v)}", flush=True)
            return v
    return ""


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


@app.get("/api/days")
def days_api(request: Request, start: str = "", end: str = ""):
    """任意日期区间的按天数据（日历 / 趋势图 / 当日详情都用这个）。

    /api/days?start=2026-11-01&end=2026-11-30&token=xxx
    """
    if not _token_ok(request, VIEW_TOKEN):
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)

    def parse_day(s):
        try:
            return datetime.date.fromisoformat((s or "").strip())
        except ValueError:
            return None

    end_d = parse_day(end) or datetime.date.today()
    start_d = parse_day(start) or (end_d - datetime.timedelta(days=29))
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    start_d = max(start_d, end_d - datetime.timedelta(days=399))  # 最多 400 天

    conn = db()
    rows = conn.execute(
        "SELECT metric, day, SUM(value), AVG(value), MIN(value), MAX(value), COUNT(*) "
        "FROM samples WHERE day>=? AND day<=? GROUP BY metric, day",
        (start_d.isoformat(), end_d.isoformat()),
    ).fetchall()
    ranges = {
        day: (mn, mx)
        for day, mn, mx in conn.execute(
            "SELECT day, MIN(CAST(json_extract(extra,'$.min') AS REAL)), "
            "MAX(CAST(json_extract(extra,'$.max') AS REAL)) "
            "FROM samples WHERE metric='heart_rate' AND extra IS NOT NULL "
            "AND day>=? AND day<=? GROUP BY day",
            (start_d.isoformat(), end_d.isoformat()),
        )
    }
    recv = {
        day: ts
        for day, ts in conn.execute(
            "SELECT day, MAX(recv) FROM samples WHERE day>=? AND day<=? GROUP BY day",
            (start_d.isoformat(), end_d.isoformat()),
        )
    }
    # 睡眠阶段（深睡/核心/REM/清醒）时长与入睡-起床窗口，从 extra 里汇总
    sleep_extra = {}
    for day, ex_json in conn.execute(
        "SELECT day, extra FROM samples WHERE metric='sleep' AND extra IS NOT NULL "
        "AND day>=? AND day<=?",
        (start_d.isoformat(), end_d.isoformat()),
    ):
        try:
            ex = json.loads(ex_json)
        except (TypeError, ValueError):
            continue
        cur = sleep_extra.setdefault(day, {"stages": {}, "window": None})
        for k, v in (ex.get("stages") or {}).items():
            if isinstance(v, (int, float)):
                cur["stages"][k] = round(cur["stages"].get(k, 0) + v, 2)
        w = ex.get("window")
        if isinstance(w, list) and len(w) == 2:
            cur["window"] = w
    conn.close()

    agg = {}
    for metric, day, total, avg, mn, mx, n in rows:
        agg.setdefault(day, {})[metric] = {"sum": total, "avg": avg, "min": mn, "max": mx, "n": n}
    for day, (mn, mx) in ranges.items():
        hr = agg.setdefault(day, {}).setdefault(
            "heart_rate", {"sum": 0, "avg": None, "min": None, "max": None, "n": 0})
        if mn is not None:
            hr["min"] = mn if hr["min"] is None else min(hr["min"], mn)
        if mx is not None:
            hr["max"] = mx if mx is None else max(hr["max"], mx)

    known = {"steps", "sleep", "heart_rate", "resting_hr"}
    out = []
    for i in range((end_d - start_d).days + 1):
        d = (start_d + datetime.timedelta(days=i)).isoformat()
        m = agg.get(d, {})
        sleep, hr, rhr, steps_ = m.get("sleep"), m.get("heart_rate"), m.get("resting_hr"), m.get("steps")
        extras = {}
        for k, v in m.items():
            if k in known:
                continue
            if v.get("sum") is not None:
                extras[k] = round(v["sum"], 1)
        out.append({
            "day": d,
            "has_data": bool(m),
            "steps": int(steps_["sum"]) if steps_ else None,
            "sleep_hours": round(sleep["sum"], 2) if sleep else None,
            "sleep_stages": (sleep_extra.get(d) or {}).get("stages") or {},
            "sleep_window": (sleep_extra.get(d) or {}).get("window"),
            "hr_avg": round(hr["avg"]) if hr and hr["avg"] is not None else None,
            "hr_min": round(hr["min"]) if hr and hr["min"] is not None else None,
            "hr_max": round(hr["max"]) if hr and hr["max"] is not None else None,
            "resting_hr": round(rhr["avg"]) if rhr and rhr["avg"] is not None else None,
            "extras": extras,
            "updated_at": (time.strftime("%Y-%m-%d %H:%M", time.localtime(recv[d]))
                           if d in recv and recv[d] else None),
        })
    return {"ok": True, "start": start_d.isoformat(), "end": end_d.isoformat(),
            "today": out[-1] if out else None, "days": out}


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
            total = round(random.uniform(6.2, 8.6), 2)
            stages = {"deep": round(total * 0.22, 2), "core": round(total * 0.55, 2),
                      "rem": round(total * 0.20, 2), "awake": round(total * 0.03, 2)}
            window = [f"{day} 23:{random.randint(10, 55)}:00 +0800",
                      f"{day} 07:{random.randint(0, 40):02d}:00 +0800"]
            conn.execute("DELETE FROM samples WHERE day=?", (day,))
            conn.executemany(
                "INSERT OR REPLACE INTO samples(metric,day,ts,value,extra,recv) VALUES(?,?,?,?,?,?)",
                [
                    ("sleep", day, f"{day} 07:00:00 +0800", total,
                     json.dumps({"stages": stages, "window": window}), time.time()),
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
    # rev 用来确认线上跑的是哪一版（排查部署是否生效）
    return {"ok": True, "rev": "2026-11-22T0105"}


# ---------------------------------------------------------------- PWA
@app.get("/manifest.webmanifest")
def manifest(request: Request, token: str = ""):
    """PWA 清单：start_url 里带上查看口令，装到桌面后打开就直接进看板。"""
    t = (token or VIEW_TOKEN or "").strip()
    start = "/?src=pwa" + (f"&token={quote(t)}" if t else "")
    return JSONResponse(
        {
            "name": "健康日报",
            "short_name": "健康日报",
            "description": "Apple Watch 睡眠、步数、心率的每日看板",
            "lang": "zh-CN",
            "start_url": start,
            "scope": "/",
            "display": "standalone",
            "background_color": "#f2f5fa",
            "theme_color": "#6c7cf0",
            "orientation": "portrait",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        },
        media_type="application/manifest+json",
    )


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    resp = FileResponse(BASE / "sw.js", media_type="application/javascript; charset=utf-8")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _png(name: str):
    return FileResponse(BASE / name, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/icon-192.png", include_in_schema=False)
def icon192():
    return _png("icon-192.png")


@app.get("/icon-512.png", include_in_schema=False)
def icon512():
    return _png("icon-512.png")


@app.get("/icon-maskable-512.png", include_in_schema=False)
def icon_maskable():
    return _png("icon-maskable-512.png")


@app.get("/apple-touch-icon.png", include_in_schema=False)
def apple_icon():
    return _png("apple-touch-icon.png")


@app.get("/")
def index():
    return FileResponse(BASE / "index.html", media_type="text/html; charset=utf-8")







