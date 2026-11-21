# HealthRelay MVP · Apple 健康 → 网页看板

iPhone 定时把睡眠 / 心率 / 步数 POST 到一台小服务器，女朋友用小米浏览器打开一个链接就能看。

> **第一次上手先看 → [步骤清单.md](步骤清单.md)**：从双击 `start.bat` 到部署上线，每一步都写了看到什么算成功。
> 本文档是同版本的参考手册（配置细节、接口、平台对比）。

```
Apple Watch ──► iPhone（Health Auto Export 或 快捷指令）
                        │  POST /ingest  (JSON + token)
                        ▼
                 FastAPI + SQLite（单文件）
                        │  GET /api/summary
                        ▼
                 手机网页（一个 index.html，无依赖）
```

## 文件

| 文件 | 作用 |
|---|---|
| `app.py` | 后端全部逻辑：收数据、去重入库、汇总、托管页面 |
| `index.html` | 前端看板（纯 HTML/CSS/JS，图表用内联 SVG，零外部依赖） |
| `requirements.txt` | 依赖只有 fastapi + uvicorn |
| `render.yaml` | Render 一键部署描述（自动生成两个口令） |
| `sample_payload.json` | Health Auto Export 的真实报文样例，用来测试 |
| `selftest.py` | 回归自测：重复推送不累加、旧库自动迁移、多段睡眠求和（`\.venv\Scripts\python.exe selftest.py`） |
| `sw.js` + `icon-*.png` | PWA：离线缓存与桌面图标（装到桌面后全屏无地址栏） |

## 一、本地先跑通（3 分钟）

```bash
cd apple-health-relay
pip install -r requirements.txt

# Windows PowerShell:  $env:INGEST_TOKEN="t1"; $env:VIEW_TOKEN="v1"
INGEST_TOKEN=t1 VIEW_TOKEN=v1 uvicorn app:app --reload --port 8000
```

打开 `http://127.0.0.1:8000/?token=v1`，如果没数据先灌 7 天假数据看效果：

```bash
curl -X POST "http://127.0.0.1:8000/api/demo?token=t1"
```

再用真实报文测一次接收：

```bash
curl -X POST "http://127.0.0.1:8000/ingest?token=t1" \
  -H "Content-Type: application/json" --data-binary @sample_payload.json
```

两个环境变量：

- `INGEST_TOKEN`：上报口令（iPhone 那边要带）。留空 = 不校验。
- `VIEW_TOKEN`：查看口令（女朋友的链接里带 `?token=`）。留空 = 谁拿到链接都能看。

> 健康数据属于敏感信息，正式部署时两个都别留空。

## 二、iPhone 端：怎么把数据 POST 出去

### 方案 A：Health Auto Export（推荐，5 分钟配好）

App Store 搜 **Health Auto Export**（免费版够用，高级功能内购）。它能按计划自动往任意 URL 发 JSON，格式见 `sample_payload.json`，后端已按这个格式解析。

1. App 内 → **Automations** → 新建 → 类型选 **REST API**
2. **URL**：`https://你的域名/ingest`（口令走 Header，不放 URL 里更安全）
3. **Headers**：`X-Auth-Token: <INGEST_TOKEN>`
4. **Data Type**：Health Metrics
5. **Select Metrics**：`Heart Rate`、`Resting Heart Rate`、`Sleep Analysis`、`Step Count`
6. **Aggregate Data**：开（这样一天一条，睡眠会带 `totalSleep`）
7. **Aggregation**：`Days`，**Export Format**：`JSON`
8. **Period**：`Last 7 Days`，**Sync Cadence**：`Every 1 hour`（或每天 08:30）

> ⚠️ **想要「今天」的数据，Period 不能选「过去 7 天」**：它只导出过去 7 个**完整**的日（到昨天为止），所以今天永远是空的。
>
> 推荐组合（Premium 支持多个自动化）：
> - 自动化 A（每小时）：**Period = `默认`**（前一天 + 今天到此刻）→ 今天的数据会实时更新
> - 自动化 B（每天 08:30）：**Period = `过去 7 天`** → 万一服务器数据被清空，能把一周历史补回来
>
> 只有一个自动化时，选 `默认` 体验最好（代价是服务器被清空后只能恢复 2 天）。

> 为什么选 Last 7 Days：每天都会把最近一周整体重发一次。后端按 `(metric, 时刻)` **后写覆盖**，同一天的新数值会替换旧值（当天还没过完时步数会一直变大，覆盖才不会累加成两倍），所以重复推送是无害的；而万一服务器数据被清空（免费平台会这样），下一次推送就能自动把 7 天历史补回来 —— 这是免费方案下最省事的"备份"。
>
> 注意：`Period` 选 `Today` + `Aggregation: Days` 只会来今天一条，历史永远是空的。

想每天只发一次，就把 **Sync Cadence** 改成自定义时间，并在 iPhone 设置里允许该 App 后台刷新。

### 方案 B：系统「快捷指令」（不装 App，但睡眠比较麻烦）

新建快捷指令，依次添加：

1. **查找健康样本**：类型 `步数`，时间范围「今天」，限制关闭
2. **计算统计**：对上面结果求 `总和` → 存为变量 `steps`
3. **查找健康样本**：类型 `心率`，时间范围「今天」→ **计算统计** `平均值` → 变量 `hr`
4. 同样再来一次求 `最小值` / `最大值` → `hr_min` / `hr_max`
5. **查找健康样本**：类型 `静息心率`，时间范围「今天」，排序「最新在前」，限制 1 → 取第一项 → 变量 `rhr`
6. **查找健康样本**：类型 `睡眠`，时间范围「昨天 18:00 到今天 09:00」→ **计算统计** `总和`（得到的是时长，单位分钟）→ 除以 60 → 变量 `sleep_hours`
   - 注意：这一步会把"在床上/清醒"的时间也算进去，比真实深睡偏大。要求准确就用方案 A。
7. **文本**：填 JSON（变量用魔法变量插入）

```json
{"date": "2026-11-21 08:30:00 +0800", "steps": [steps], "heart_rate": [hr], "resting_hr": [rhr], "sleep_hours": [sleep_hours]}
```

8. **获取 URL 内容**：URL 填 `https://你的域名/ingest`，方法 `POST`，请求体 `文本`，头部
   - `Content-Type` → `application/json`
   - `X-Auth-Token` → 你的上报口令
9. 自动化：快捷指令 App → **自动化** → 新建 → **时间** → 每天 08:30 → 运行该快捷指令 → 关掉「运行前询问」

### 怎么验证通没通

- iPhone 上手动跑一次快捷指令 / 在 App 里点 "Run now"
- 浏览器打开 `https://你的域名/api/raw?token=查看口令`，能看到刚落库的样本就说明成功
- 页面顶部「更新 11-21 20:01」就是最后接收时间

## 三、接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/ingest` | 收数据，兼容三种格式：Health Auto Export、扁平 JSON、数组 |
| GET | `/api/summary?days=7` | 按天汇总（睡眠/步数/心率均值与区间/静息心率） |
| GET | `/api/days?start=2026-11-01&end=2026-11-30` | 任意日期区间的按天数据（日历、趋势图、当日详情都用它），额外指标放在 `extras` |
| GET | `/api/raw?limit=30` | 最近的原始样本，调试用 |
| POST | `/api/demo` | 灌 7 天假数据看效果 |
| GET | `/healthz` | 健康检查（Render 用） |
| GET | `/` | 看板页面 |

`/ingest` 返回 `{"ok":true,"stored":3,"metrics":["heart_rate","sleep","steps"]}`，`stored` 是本次解析出的条数（重复推送时同一时刻的旧值会被覆盖，条数照样是 3，但数据库里不会多出行）。

## 四、部署：两个免费平台

### 首选：Render（最省事，0 成本）

1. 把这几个文件推到 GitHub 仓库（`app.py`、`index.html`、`requirements.txt`、`render.yaml`）
2. Render 控制台 → **New → Blueprint** → 选仓库 → 建 Blueprint（`render.yaml` 会自动带好启动命令并生成两个随机口令）
   - 不用 Blueprint 也行：**New → Web Service** → Runtime 选 Python →
     Build: `pip install -r requirements.txt` →
     Start: `uvicorn app:app --host 0.0.0.0 --port $PORT` → Plan 选 **Free**
3. 部署完成后在 **Environment** 里能看到 `INGEST_TOKEN` / `VIEW_TOKEN`，复制出来
4. 打开 `https://<你的服务>.onrender.com/?token=<VIEW_TOKEN>`，收工

免费额度的真实限制（官方文档）：

- 15 分钟没请求就休眠，下次访问要冷启动约 **1 分钟**（页面会先卡一下，正常）
- 每月 750 实例小时，够一个服务跑满整月
- **文件系统是易失的**：重启/重新部署/休眠后 `health.db` 会消失 → 靠上面说的"每次推 7 天"自愈
- 免费 Postgres 有 30 天有效期，所以这版用 SQLite 反而更稳；想长期保存就升级 Web Service（$7/月）挂一块持久磁盘，SQLite 文件就留住了

**国内访问**：Render 在国内可以用，但首次冷启动 + 跨境线路会让首屏慢几秒。想更快见下面的备选。

### 备选：Zeabur / 腾讯云 CloudBase 云托管（国内访问快）

- Zeabur：Git 连仓库 → 自动识别 Python → 同样用 `uvicorn app:app --host 0.0.0.0 --port $PORT` 启动 → 设置两个环境变量即可，有免费额度，国内线路明显更快
- 腾讯云 CloudBase 云托管：国内延迟最低，按量计费、有免费额度；代码不用改，配置一个 Dockerfile 或用官方 Python 模板 + 环境变量

### 不推荐：Vercel

Vercel 的免费层是纯 Serverless、没有持久存储：每次请求可能是全新实例、`/tmp` 不共享，SQLite 写进去下次就没了。要用 Vercel 得额外接 Supabase / KV，MVP 阶段不值得。

### Railway

免费层现在限制较多（每天固定时段拒绝部署、按量计费），作为个人玩具项目不如 Render 省心。

## 五、把它交到女朋友手里

1. 把 `https://域名/?token=查看口令` 发给她
2. 小米浏览器打开 → 菜单 → **添加到桌面**，会生成一个图标，点开就是看板
3. 页面每 5 分钟自动刷新，从后台切回来也会刷新
4. 第一次打开如果卡 30 秒以上，是服务在冷启动，刷新一下就好

### 装成 App（PWA）

项目自带 PWA 支持，装到桌面后**全屏、无地址栏、有独立图标**，断网还能看上次的数据：

- **安卓（小米/华为等）**：用 Chrome 或系统浏览器打开看板 → 右下角会弹出「装到桌面」→ 点**安装**（或菜单 → 安装应用/添加到主屏幕）。
- **iPhone**：Safari 打开 → 底部**分享**按钮 → **添加到主屏幕**。
- 图标是渐变底 + 心电图线；`manifest.webmanifest` 会自动把当前 `token` 写进 `start_url`，所以装好后点图标直接进看板，不用再输口令。
- 离线：`sw.js` 会缓存页面外壳和最近一次接口数据，断网时打开显示上次的数据。

> 想换图标：改 `tools/make_icons.py`（或直接替换 `icon-192.png` / `icon-512.png` / `apple-touch-icon.png`）后重新部署即可。

## 六、还能怎么长

- 加一行"今日步数达成 8000"的鼓励语，或按她习惯的目标值改 `index.html` 里的 `goal` / `stepGoal`
- 想要更细的心率曲线：Health Auto Export 里把 Aggregation 改成 `Hours`，后端两种粒度都能正确处理（同一小时内覆盖、跨小时求和），页面无需改
- 想让数据永久留存：把 `db()` 换成 Postgres（Supabase 免费层），或直接换 Upstash Redis
- 想做"异常提醒"：在 `/ingest` 里判断静息心率高于阈值就调 Server酱 / 企业微信机器人推一条

---

## 变更记录

**2026-11-22（时区 & 空数据处理）**

- **修时区**：Render 服务器是 UTC，比北京时间慢 8 小时，导致北京时间 0 点后「今天」对不上（页面显示暂无数据）。现在全后端统一按 **+08:00** 计算「今天」和展示时间。
- **今天没数据不再白屏**：自动回退显示「最近一个有数据的那天」，并提示「今天还没有新数据，下面是 X 月 X 日的数据」。
- 顶部状态改为相对时间（「3 分钟前同步」），页面每 60 秒自动刷新今天的数据，切回前台也刷新。
- 7 天趋势的星期标签与柱状图对齐修正。

**2026-11-22（睡眠结构 + 探活）**

- 今日卡片把「静息心率」合并进「心率」卡（平均 + 区间 + 静息一行看完）
- 当日详情分节：**睡眠 / 心率 / 活动 / 其他**；睡眠里显示总时长、深睡/核心/快速眼动/清醒的时长与占比、入睡–起床时间（数据里有才显示）
- 后端解析睡眠阶段（`deep/core/rem/awake`）与睡眠窗口，通过 `/api/days` 的 `sleep_stages`、`sleep_window` 暴露
- 免费探活：`keepalive.bat` + Windows 计划任务「HealthRelayKeepAlive」每 10 分钟访问 `/healthz`，避免免费实例休眠（`deploy_github_curl.py` 里的 `.github/workflows/keepalive.yml` 需要带 `workflow` 权限的 token 才能推送，暂用本机任务代替）

**2026-11-22（看板改版）**

- 前端重做：今日睡眠环形进度、步数/静息心率双卡、心率区间卡
- 7 天趋势图柱子**显示具体数值**，点任意柱子弹出当天完整明细
- 新增**月历**：可切换月份，每天的步数强度用橙条表示、有睡眠时右上角有蓝点；点某天看详情
- 新增月统计：日均步数、日均睡眠、达标天数
- 新增 `/api/days?start=&end=` 接口（按任意区间取每天的聚合数据，含 `extras` 里的其它指标）
- 支持 `#d=2026-11-20` 直接打开某天详情（方便收藏/分享）
- 深色模式适配

**2026-11-21（接管后修复）**

- 去重键从 `(metric, ts, value)` 改成 `(metric, ts)`，写入方式由「忽略重复」改成「后写覆盖」。原因：`Aggregation: Days` 时同一天的累计值每小时都在变大（08:00 时 4000 步、20:00 时 9000 步），旧逻辑会把它当成两条独立样本相加，导致今日步数一路翻倍。现在同一时刻只保留最新一次上报。
- 旧数据库启动时自动迁移（`PRAGMA user_version` 2）：先按 `(metric, ts)` 去重保留最新一行，再建新唯一索引，不需要手动删 `health.db`。
- 睡眠由 `MAX` 改成 `SUM`：`Aggregation: Hours` 或一天有两段睡眠（夜睡 + 午睡）时，求和才等于真实总时长；`Days` 聚合下两者结果相同。
- 新增 `selftest.py`，9 条断言覆盖上面三条。

---

数据只在你自己的服务器上，别把带 token 的链接发到群里。
