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
import time
from pathlib import Path
from urllib.parse import quote

from store import Store

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DB_PATH = DATA_DIR / "health.db"
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "").strip()   # 上报口令，留空则不校验
VIEW_TOKEN = os.getenv("VIEW_TOKEN", "").strip()       # 查看口令，留空则公开

BASE = Path(__file__).parent
# Render 的服务器时区是 UTC，而数据（HealthKit 时间戳）和用户都在 +08:00。
# 统一按 +08:00 算「今天」和展示时间，否则北京时间 0 点后页面会认为"今天还没数据"。
TZ = datetime.timezone(datetime.timedelta(hours=8))
# vivo 健康写进 Apple Health 的睡眠时间比实际早 8 小时（实测 20:35-03:39 被写成 04:35-11:39），
# 这里统一往回挪，可用环境变量 SLEEP_SHIFT_HOURS 覆盖（设为 0 即不纠正）。
SLEEP_SHIFT_HOURS = float(os.getenv("SLEEP_SHIFT_HOURS", "0"))   # 你的睡眠时间是对的，默认不偏移


def _shift_time(ts, hours: float):
    """把 "2026-11-22 04:35:00 +0800" 这种时间串平移若干小时。"""
    if not hours:
        return ts
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.datetime.strptime(str(ts).strip(), fmt)
            return (dt + datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            continue
    return ts


def _now() -> datetime.datetime:
    return datetime.datetime.now(TZ)


def _fmt_ts(epoch, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.datetime.fromtimestamp(epoch, TZ).strftime(fmt)
DAY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
SLEEP_STAGE_RE = re.compile(r"(deep|core|rem|awake|inbed|in_bed|asleep)", re.I)


def _sleep_stage(raw):
    """把 HAE 原始样本里的 HKCategoryValueSleepAnalysis* 映射成阶段名。"""
    s = str(raw or "")
    if "SleepAnalysis" not in s and not SLEEP_STAGE_RE.search(s):
        return None
    low = s.lower()
    if "deep" in low:
        return "deep"
    if "core" in low:
        return "core"
    if "rem" in low:
        return "rem"
    if "awake" in low:
        return "awake"
    if "inbed" in low or "in_bed" in low:
        return "inbed"
    if "asleep" in low:
        return None  # 未细分，只算总时长
    return None
ALIAS = {
    "step_count": "steps",
    "steps": "steps",
    "heart_rate": "heart_rate",
    "resting_heart_rate": "resting_hr",
    "sleep_analysis": "sleep",
}
app = FastAPI(title="HealthRelay MVP")


# ---------------------------------------------------------------- storage
STORE = Store(DATA_DIR)


@app.on_event("startup")
def _init_store():
    """启动时建表（SQLite 自动迁移；Postgres 首次自动建表）。"""
    STORE.init()


def _is_raw(metric: str, row: dict) -> bool:
    """判断这一行是「原始样本」还是「按天汇总行」。

    不能只看时间戳（睡眠段可能正好从 00:00 开始），要按各指标的数据特征：
      - 睡眠：原始样本带 stage（core/deep/rem），汇总行只有 stages 字典
      - 心率：原始样本只有单值，汇总行带 min/max
      - 其它（步数/能量/距离）：汇总行的时间戳是当天 00:00，原始样本是具体时刻
    """
    extra = str(row.get("extra") or "")
    if metric == "sleep":
        return '"stage"' in extra
    if metric == "heart_rate":
        return "min" not in extra and "max" not in extra
    return " 00:00:00" not in str(row.get("ts") or "")


def _dedupe_sleep(rows):
    """同一天「同一段睡眠」可能被两个自动化各推一次（汇总行 + 会话行），只保留一条。

    判重依据是 extra.window（入睡–起床时间）；优先保留带阶段信息的那条。
    """
    out, seen = [], {}
    for r in rows:
        if r["metric"] != "sleep":
            out.append(r)
            continue
        try:
            ex = json.loads(r.get("extra") or "{}")
        except (TypeError, ValueError):
            ex = {}
        w = ex.get("window")
        if not (isinstance(w, list) and len(w) == 2):
            out.append(r)
            continue
        key = (r.get("day"), str(w[0]), str(w[1]))
        prev = seen.get(key)
        if prev is None:
            seen[key] = r
            out.append(r)
            continue
        prev_has = '"stages"' in str(prev.get("extra") or "")
        cur_has = '"stages"' in str(r.get("extra") or "")
        if cur_has and not prev_has:
            out[out.index(prev)] = r
            seen[key] = r
    return out


def _aggregate(rows):
    """按 (day, metric) 聚合。

    同一天同一指标可能同时有「汇总行」和「原始样本」：
    直接 SUM 会翻倍，所以**当天只要有原始样本就只用原始样本**，
    没有原始样本（老数据 / 只跑「过去 7 天」那个自动化）才用汇总行。
    """
    rows = _dedupe_sleep(rows)
    days, hr_range, recv = {}, {}, {}
    raw_sum, raw_n = {}, {}
    all_sum, all_n = {}, {}
    vmin, vmax = {}, {}

    for r in rows:
        day, metric, v = r["day"], r["metric"], r["value"]
        key = (day, metric)
        if v is not None:
            all_sum[key] = all_sum.get(key, 0.0) + v
            all_n[key] = all_n.get(key, 0) + 1
            vmin[key] = v if key not in vmin else min(vmin[key], v)
            vmax[key] = v if key not in vmax else max(vmax[key], v)
            if _is_raw(metric, r):
                raw_sum[key] = raw_sum.get(key, 0.0) + v
                raw_n[key] = raw_n.get(key, 0) + 1
        if r.get("recv"):
            recv[day] = max(recv.get(day, 0), r["recv"])
        if metric == "heart_rate" and r.get("extra"):
            try:
                ex = json.loads(r["extra"])
            except (TypeError, ValueError):
                ex = {}
            mn, mx = ex.get("min"), ex.get("max")
            cur = hr_range.setdefault(day, [None, None])
            if isinstance(mn, (int, float)):
                cur[0] = mn if cur[0] is None else min(cur[0], mn)
            if isinstance(mx, (int, float)):
                cur[1] = mx if cur[1] is None else max(cur[1], mx)

    for key, total in all_sum.items():
        day, metric = key
        has_raw = raw_n.get(key, 0) > 0
        bucket = days.setdefault(day, {})
        bucket[metric] = {
            "sum": raw_sum[key] if has_raw else total,
            "n": raw_n.get(key, 0) if has_raw else all_n[key],
            "min": vmin.get(key),
            "max": vmax.get(key),
        }

    for day, (mn, mx) in hr_range.items():
        hr = days.setdefault(day, {}).setdefault(
            "heart_rate", {"sum": 0.0, "n": 0, "min": None, "max": None})
        if isinstance(mn, (int, float)):
            hr["min"] = mn if hr.get("min") is None else min(hr["min"], mn)
        if isinstance(mx, (int, float)):
            hr["max"] = mx if hr.get("max") is None else max(hr["max"], mx)
    return days, recv


def _day_out(day: str, m: dict, recv: dict):
    sleep, hr, rhr, steps = m.get("sleep"), m.get("heart_rate"), m.get("resting_hr"), m.get("steps")
    workout = m.get("workout")
    extras = {}
    for k, v in m.items():
        if k in ("steps", "sleep", "heart_rate", "resting_hr", "workout"):
            continue
        extras[k] = round(v["sum"], 1)
    return {
        "day": day,
        "has_data": bool(m),
        "steps": int(steps["sum"]) if steps else None,
        "sleep_hours": round(sleep["sum"], 2) if sleep else None,
        "sleep_stages": {},
        "sleep_window": None,
        "workout_minutes": round(workout["sum"], 1) if workout else None,
        "workouts": [],
        "hr_avg": round(hr["sum"] / hr["n"]) if hr and hr.get("n") else None,
        "hr_min": round(hr["min"]) if hr and hr.get("min") is not None else None,
        "hr_max": round(hr["max"]) if hr and hr.get("max") is not None else None,
        "resting_hr": round(rhr["sum"] / rhr["n"]) if rhr and rhr.get("n") else None,
        "extras": extras,
        "updated_at": _fmt_ts(recv[day], "%Y-%m-%d %H:%M") if recv.get(day) else None,
    }


# ---------------------------------------------------------------- parsing
def today() -> str:
    return _now().strftime("%Y-%m-%d")


def _day_of(ts: str) -> str:
    m = DAY_RE.search(str(ts or ""))
    return m.group(1) if m else today()


def _num(v):
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _scaled(v, scale: float):
    n = _num(v)
    return None if n is None else round(n * scale, 4)


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
            workouts = payload["data"].get("workouts") or []
        elif isinstance(payload.get("metrics"), list):
            metrics = payload["metrics"]
            workouts = payload.get("workouts") or []
        else:
            workouts = []
    else:
        workouts = []

    # 运动（HAE 的「锻炼」数据类型）：一条 = 一次训练
    for w in workouts:
        if not isinstance(w, dict):
            continue
        name = w.get("name") or w.get("type") or "运动"
        start = w.get("start") or w.get("startDate")
        end = w.get("end") or w.get("endDate")
        minutes = _num(w.get("duration"))
        if minutes is None and start and end:
            minutes = round((_hours(start, end) or 0) * 60, 1)
        if not minutes:
            continue
        extra = {"name": str(name)}
        for src, dst in (("start", "start"), ("startDate", "start"),
                         ("end", "end"), ("endDate", "end"),
                         ("activeEnergyBurned", "energy"), ("activeEnergy", "energy"),
                         ("distance", "distance")):
            if w.get(src) is not None and dst not in extra:
                extra[dst] = w.get(src)
        add("workout", end or start, minutes, extra)

    for m in metrics:
        if not isinstance(m, dict):
            continue
        name, rows = m.get("name"), m.get("data") or []
        # 单位换算：Health Auto Export 可能按千焦(kJ)/英里导出，统一换成千卡(kcal)/公里(km)
        units = str(m.get("units") or "").strip().lower()
        scale = 1.0
        if units in ("kj", "kilojoule", "kilojoules", "千焦", "千焦耳"):
            scale = 1 / 4.184
        elif units in ("mi", "mile", "miles", "英里"):
            scale = 1.60934
        for row in rows:
            if not isinstance(row, dict):
                continue
            ts = (row.get("date") or row.get("start") or row.get("end")
                  or row.get("startDate") or row.get("endDate"))
            if name == "sleep_analysis":
                # 支持三种形态：
                #  A. 聚合导出 {"totalSleep":7.6,"deep":..,"core":..}
                #  B. 原始样本 v2 {"startDate":..,"endDate":..,"value":"HKCategoryValueSleepAnalysisAsleepCore"}
                #  C. 原始样本 v1 {"sleepStart":..,"sleepEnd":..}
                start = (row.get("sleepStart") or row.get("start")
                         or row.get("startDate") or row.get("start_time"))
                end = (row.get("sleepEnd") or row.get("end") or row.get("endDate")
                       or row.get("end_time"))
                raw_v = row.get("totalSleep")
                if raw_v is None:
                    raw_v = row.get("asleep")
                if raw_v is None:
                    raw_v = row.get("value")
                if raw_v is None:
                    raw_v = row.get("qty")
                # vivo 写进来的睡眠时间整体偏 +8 小时，这里统一挪回来
                if start:
                    start = _shift_time(start, SLEEP_SHIFT_HOURS)
                if end:
                    end = _shift_time(end, SLEEP_SHIFT_HOURS)
                stage = _sleep_stage(raw_v)
                v = _num(raw_v)
                if v is None and start and end:
                    v = _hours(start, end)
                # 阶段时长（深睡/核心/REM/清醒）留着给前端画睡眠结构
                stages = {k: row.get(k) for k in ("deep", "core", "rem", "awake", "inBed")}
                stages = {k: val for k, val in stages.items() if val is not None}
                extra = {}
                if stage:
                    extra["stage"] = stage
                if stages:
                    extra["stages"] = stages
                if start and end:
                    extra["window"] = [str(start), str(end)]
                # 清醒/卧床单独记，不混进「睡眠时长」
                # 归属按「起床时间」算：否则跨午夜的那段会被记到前一天
                sleep_ts = end or ts
                add("sleep_" + stage if stage in ("awake", "inbed") else "sleep",
                    sleep_ts, v, extra or None)
            elif "Avg" in row or "Min" in row or "Max" in row:
                add(name, ts, _scaled(row.get("Avg", row.get("qty")), scale),
                    {"min": _scaled(row.get("Min"), scale), "max": _scaled(row.get("Max"), scale)})
            else:
                add(name, ts, _scaled(row.get("qty", row.get("value")), scale))

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
        folded = _fold_daily(samples)
        STORE.upsert(folded)
        print(f"[ingest] {len(samples)} samples -> {len(folded)} daily rows", flush=True)
    return {"ok": True, "stored": len(samples),
            "folded": len(_fold_daily(samples)) if samples else 0,
            "metrics": sorted({s["metric"] for s in samples})}


def _fold_daily(samples):
    """入库前按 (指标, 天) 折叠成一行。

    关掉「汇总数据」后 App 每分钟会推成百上千条原始样本，全存下来几天就能把
    免费数据库撑爆（实测一天就 150 万行 / 490MB）。看板只需要按天的数，
    所以这里直接折成「一天一条」：

      - 心率：取平均，min/max 放进 extra
      - 睡眠：先按入睡-起床窗口去重（两个自动化会推同一晚），再求和，阶段累加
      - 其它：求和

    前提是每次推送都覆盖完整的一天（「默认」= 前一天 + 今天、「过去 7 天」= 完整 7 天）。
    同一天后一次推送会覆盖前一次，不会累加。
    """
    from collections import defaultdict

    buckets = defaultdict(list)
    for s in samples:
        buckets[(s["metric"], s["day"])].append(s)

    out = []
    for (metric, day), items in buckets.items():
        ts = f"{day} 00:00:00 +0800"
        extra = {}
        if metric == "heart_rate":
            vals = [i["value"] for i in items if i["value"] is not None]
            if not vals:
                continue
            value = sum(vals) / len(vals)
            lo, hi = min(vals), max(vals)
            # 汇总报文自带 Min/Max（一条代表一整天），不能只按 value 算，否则区间会塌成一个点
            for i in items:
                try:
                    ex = json.loads(i.get("extra") or "{}")
                except (TypeError, ValueError):
                    ex = {}
                for k in ("min", "max"):
                    v2 = ex.get(k)
                    if isinstance(v2, (int, float)):
                        lo, hi = min(lo, v2), max(hi, v2)
            extra = {"min": lo, "max": hi}
        elif metric == "workout":
            # 运动不求和成一条，保留每次训练的名字/时长/时间
            sessions, total = [], 0.0
            for i in items:
                try:
                    ex = json.loads(i.get("extra") or "{}")
                except (TypeError, ValueError):
                    ex = {}
                mins = i["value"] or 0
                total += mins
                sessions.append({
                    "name": ex.get("name") or "运动",
                    "minutes": round(mins, 1),
                    "start": ex.get("start"),
                    "end": ex.get("end"),
                    "energy": ex.get("energy"),
                    "distance": ex.get("distance"),
                })
            value = total
            extra = {"sessions": sessions}
        elif metric == "sleep":
            unseen, seen = [], set()
            for i in items:
                try:
                    ex = json.loads(i.get("extra") or "{}")
                except (TypeError, ValueError):
                    ex = {}
                w = ex.get("window")
                key = (str(w[0]), str(w[1])) if isinstance(w, list) and len(w) == 2 else None
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                unseen.append((i, ex))
            vals = [i["value"] for i, _ in unseen if i["value"] is not None]
            if not vals:
                continue
            value = sum(vals)
            stages = {}
            window = None
            for _, ex in unseen:
                for k, v in (ex.get("stages") or {}).items():
                    if isinstance(v, (int, float)):
                        stages[k] = round(stages.get(k, 0) + v, 2)
                w = ex.get("window")
                if isinstance(w, list) and len(w) == 2:
                    window = ([min(window[0], w[0]), max(window[1], w[1])] if window else w)
            if stages:
                extra["stages"] = stages
            if window:
                extra["window"] = window
        else:
            vals = [i["value"] for i in items if i["value"] is not None]
            if not vals:
                continue
            value = sum(vals)

        out.append((metric, day, ts, round(float(value), 4),
                    json.dumps(extra, ensure_ascii=False) if extra else None, time.time()))
    return out


@app.get("/api/summary")
def summary(request: Request, days: int = 7):
    if not _token_ok(request, VIEW_TOKEN):
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    days = max(1, min(days, 60))
    now = _now()
    rows = STORE.range_rows((now - datetime.timedelta(days=days - 1)).strftime("%Y-%m-%d"),
                            now.strftime("%Y-%m-%d"))
    agg, recv = _aggregate(rows)
    out = []
    for i in range(days - 1, -1, -1):
        day = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        item = _day_out(day, agg.get(day, {}), recv)
        out.append({k: item[k] for k in
                    ("day", "sleep_hours", "steps", "hr_avg", "hr_min", "hr_max", "resting_hr")})
    updated = max(recv.values()) if recv else None
    return {"ok": True, "today": out[-1], "days": out,
            "updated_at": _fmt_ts(updated) if updated else None}


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

    end_d = parse_day(end) or _now().date()
    start_d = parse_day(start) or (end_d - datetime.timedelta(days=29))
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    start_d = max(start_d, end_d - datetime.timedelta(days=399))  # 最多 400 天

    rows = _dedupe_sleep(STORE.range_rows(start_d.isoformat(), end_d.isoformat()))
    agg, recv = _aggregate(rows)

    # 睡眠阶段（深睡/核心/REM/清醒）与入睡-起床窗口
    workout_sessions = {}
    for r in rows:
        if r["metric"] != "workout" or not r.get("extra"):
            continue
        try:
            ex = json.loads(r["extra"])
        except (TypeError, ValueError):
            continue
        if isinstance(ex.get("sessions"), list):
            workout_sessions.setdefault(r["day"], []).extend(ex["sessions"])

    sleep_extra = {}
    for r in rows:
        if r["metric"] != "sleep" or not r.get("extra"):
            continue
        try:
            ex = json.loads(r["extra"])
        except (TypeError, ValueError):
            continue
        cur = sleep_extra.setdefault(r["day"], {"stages": {}, "window": None})
        for k, v in (ex.get("stages") or {}).items():
            if isinstance(v, (int, float)):
                cur["stages"][k] = round(cur["stages"].get(k, 0) + v, 2)
        # 原始样本：每条只带一个阶段，按阶段累加
        st = ex.get("stage")
        if st and isinstance(r.get("value"), (int, float)):
            cur["stages"][st] = round(cur["stages"].get(st, 0) + r["value"], 2)
        w = ex.get("window")
        if isinstance(w, list) and len(w) == 2:
            cur["window"] = ([min(cur["window"][0], w[0]), max(cur["window"][1], w[1])]
                             if cur["window"] else w)

    out = []
    for i in range((end_d - start_d).days + 1):
        day = (start_d + datetime.timedelta(days=i)).isoformat()
        item = _day_out(day, agg.get(day, {}), recv)
        se = sleep_extra.get(day) or {}
        item["sleep_stages"] = se.get("stages") or {}
        item["sleep_window"] = se.get("window")
        item["workouts"] = workout_sessions.get(day, [])
        out.append(item)
    return {"ok": True, "start": start_d.isoformat(), "end": end_d.isoformat(),
            "today": out[-1] if out else None, "days": out}


@app.get("/api/export")
def export_all(request: Request):
    """全量导出（部署前自动备份用）。"""
    if not _token_ok(request, VIEW_TOKEN):
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    rows = STORE.all_rows()
    return {"ok": True, "count": len(rows), "exported_at": _fmt_ts(time.time()),
            "rows": rows}


@app.post("/api/import")
async def import_all(request: Request):
    """全量恢复（部署后自动回灌用）。同一 (metric, ts) 覆盖。"""
    if not _token_ok(request, INGEST_TOKEN):
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    rows = payload.get("rows") or []
    data = [(r.get("metric"), r.get("day"), r.get("ts"), r.get("value"),
             r.get("extra"), r.get("recv") or time.time()) for r in rows if r.get("metric")]
    n = STORE.upsert(data)
    print(f"[import] restored {n} rows", flush=True)
    return {"ok": True, "imported": n}


@app.get("/api/raw")
def raw(request: Request, limit: int = 30):
    if not _token_ok(request, VIEW_TOKEN):
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    return {"ok": True, "items": STORE.recent(min(max(limit, 1), 500))}


@app.post("/api/demo")
def demo(request: Request):
    """灌 7 天假数据，方便先看效果：curl -X POST '.../api/demo?token=xxx'"""
    if not _token_ok(request, INGEST_TOKEN):
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    import random
    now = _now()
    for i in range(7):
        day = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        total = round(random.uniform(6.2, 8.6), 2)
        stages = {"deep": round(total * 0.22, 2), "core": round(total * 0.55, 2),
                  "rem": round(total * 0.20, 2), "awake": round(total * 0.03, 2)}
        window = [f"{day} 23:{random.randint(10, 55):02d}:00 +0800",
                  f"{day} 07:{random.randint(0, 40):02d}:00 +0800"]
        STORE.delete_day(day)
        STORE.upsert([
            ("sleep", day, f"{day} 07:00:00 +0800", total,
             json.dumps({"stages": stages, "window": window}), time.time()),
            ("steps", day, f"{day} 22:00:00 +0800", random.randint(3200, 12000), None, time.time()),
            ("heart_rate", day, f"{day} 12:00:00 +0800", random.randint(64, 82),
             json.dumps({"min": random.randint(48, 56), "max": random.randint(110, 145)}), time.time()),
            ("resting_hr", day, f"{day} 07:00:00 +0800", random.randint(52, 61), None, time.time()),
        ])
    return {"ok": True, "seeded_days": 7}


@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz():
    # rev 用来确认线上跑的是哪一版（排查部署是否生效）
    return {"ok": True, "rev": "2026-11-23T0215"}


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


@app.api_route("/", methods=["GET", "HEAD"])
def index():
    return FileResponse(BASE / "index.html", media_type="text/html; charset=utf-8")








































