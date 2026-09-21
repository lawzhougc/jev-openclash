# SmartRoute · OpenClash 智能选路

在 OpenClash（Mihomo 内核）之上做**平台级智能选路**：拉节点实时延迟 → Jev 判断地区风险先验 → 真实探活验证 → 自动切换策略组。

不改 OpenClash 配置文件，全部通过 External Controller API 操作。代理商更新订阅、换节点、换密码都不影响本服务。

---

## 它解决什么问题

原项目（jev-openclash）是个演示原型，选路逻辑是 `random.choice(nodes)`。

真实痛点有三个是原方案完全没覆盖的：

1. **延迟正常但平台不认这个出口 IP。** 79ms 的节点完全可能被 Google 403 拒绝。延迟数字测不出 IP 风控。
2. **策略组嵌套导致"选了等于没选"。** 如 `♊ Gemini` 是 Selector，成员全是组不是节点，`PUT /proxies/♊ Gemini` 直接传节点名会返回 `400 proxy not exist`。
3. **平台流量锁死在单一地区。** 如 `♊ Gemini` 组里 16 个候选，实际只用了 4 个美国节点。

SmartRoute 针对这三点分别给出：真实探活、可达路径解析、多地区候选池。

---

## 架构

```
采集层  collector.py   拉 /providers/proxies/{provider}，拿全部节点延迟与存活
                      find_path_to() 解析跨组切换路径（本项目的核心技术点）
探活层  prober.py      对当前出口真实请求目标平台，解析响应体区分"缺 key"与"地区风控"
先验层  jev.py         TypeSafe Jev 做地区级风险判断（原子问题，带缓存）
决策层  engine.py      三路信号加权打分 -> 切换 -> 验证 -> 不通过则回滚
服务层  app.py         FastAPI + WebSocket，前端实时看板
```

### 三路信号如何合成

```
综合分 = 延迟分 × 0.40  +  探活分 × 0.45  +  先验分 × 0.15
```

- **延迟分**：来自 provider 的 `history.delay`，50ms 满分、400ms 归零
- **探活分**：真实请求的响应耗时与判据结果，被风控直接归零；
  探活结果带 TTL 缓存后进入候选打分，未实测的节点按先验折算
- **先验分**：Jev 给出的该地区被平台放行概率

权重可在 `config.yaml` 的 `engine` 段调整。

---

## 关键实现细节（踩过的坑）

### 1. 节点必须从 provider 拿，不是 /proxies

```
GET /proxies                      -> 只有策略组，39 条
GET /providers/proxies/{provider} -> 真实节点，56 条，带 alive 和 history.delay
```

`GET /proxies/{节点名}/delay` 对 provider 内节点**一律 404**，这是 Mihomo 的行为。

### 2. 切换必须走"可达路径"

Mihomo 的 `PUT /proxies/{组}` 只能切到该组的**直接成员**。跨组传节点名会返回：

```
400 {"message":"Selector update error: proxy not exist"}
```

而 `♊ Gemini` 的直接成员全是组。所以要用 `find_path_to()` 递归找一条路径：

```
♊ Gemini → 🚀 默认代理 → 🔯 日本故转 → JP-X5-1
```

`apply_path()` 按顺序逐级切换。

### 3. 组的 `now` 可能是另一个组名

`♊ Gemini` 的 `now` 可能是 `🔯 Gemini故转`（一个 Fallback 组），不是真实节点。
直接拿去和节点打分表比对会得到 0 分，导致引擎误判"当前节点很差"而反复切换。

必须用 `resolve_now()` 递归下钻到真实节点。
另外 Smart / URLTest 型自动组的 `now` 是 `Smart - Select` 这类内部值，需要特殊处理。

### 4. 判定必须解析错误信封，且分清「地区风控 / 滥用封禁 / 缺鉴权 / 配额」

Google API 的错误体固定是 `{"error": {"code", "status", "message"}}`。
**只看状态码会把最常见的地区风控判反** —— 它可能是 400 而不是 403：

| 响应特征 | 真实含义 | verdict | 处理 |
|---|---|---|---|
| `FAILED_PRECONDITION` + `User location is not supported`（**常为 400**） | IP 被地区限制 | `geo_blocked` | 实锤风控，压 0 + 加重惩罚 |
| `PERMISSION_DENIED` + `location/country/region` 地区短语 | IP 被地区限制 | `geo_blocked` | 同上 |
| `automated queries` / `blocked due to abuse` | IP 被滥用封禁 | `ip_flagged` | 实锤风控，同上 |
| `unregistered callers` / `API key not valid` | IP 已放行，只是没 key | `auth_ok` | **可用** |
| 429 / `RESOURCE_EXHAUSTED` | 配额受限（共享 IP 常态） | `quota_limited` | 可用但打折 |
| 403 且无任何地区/滥用证据 | 无法判别 | `unknown` | 验证不过、回滚，**绝不拉黑** |

判据要点（prober.classify_response）：

1. **解析信封字段，不全文扫关键词** —— v1 扫 `location/region` 单词会误命中字段名；
   现在只匹配多词精确短语，且任何状态码都先查 geo/abuse。
2. **每次判定带证据原文**（evidence），面板「Gemini 风控档案」可审计。
3. **对照组仲裁（L3）**：判定实锤前，经旁路监听把探活组切到 `DIRECT`
   （家庭宽带出口）再探同一目标 —— 对照组同样命中 → 判据可疑，暂缓拉黑；
   对照组正常 → 节点风控确认。
4. **判据校准守卫**：10 分钟内 ≥3 个不同节点被同一套判据「实锤」→
   全局 `criterion_suspect`，暂缓一切拉黑（大概率 Google 改版/区域性故障），
   任一探活转 `ok/auth_ok` 自动解除。
5. **出口 IP 回显**：探活同时记录链路出口 IP（`ip_echo_url`），
   审计「测的到底是谁」——切换后出口没变（路由没生效）一眼可辨。

### 5. 本机开发时的网络隔离

- 访问 `192.168.3.2:9090` 必须走**直连**，走系统代理会得到 `502 Bad Gateway`
- 探活请求要清掉 `HTTP_PROXY` 等环境变量，否则测的是代理而不是节点

### 6. Jev 的输出质量取决于 state 的信息密度

同一个问题，两种问法差别巨大：

| 给 Jev 的上下文 | 日本 | 美国 |
|---|---|---|
| 只写"japan 出口（例: JP-X5-1）" | 0.55 | 0.49 |
| 写明延迟区间、线路等级（Dedicated/X5）、存活率、地区可用性 | **0.83** | 0.71 |

另外 **Jev 是 gut-check 模型，不做多因素推理**。问它"为 12 个候选节点选最优"会得到平的概率分布（confidence 只有 0.38）。必须拆成原子问题：
"日本出口 IP 访问 Gemini 被拒绝的概率低吗？" —— 这才是它擅长的。

---

## 快速开始

### 本机开发

```bash
pip install -r requirements.txt
export TYPESAFE_API_KEY="apikey_xxx_yyy"
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000

**首次务必保持 `config.yaml` 里 `server.dry_run: true`**，观察几轮决策确认无误后再改为 `false`。

### 部署到 iStoreOS

```bash
docker build -t smartroute:latest .
docker save smartroute:latest | gzip > smartroute.tar.gz
# 传到 iStoreOS 后
docker load < smartroute.tar.gz
docker run -d --name smartroute --restart unless-stopped \
  --network host \
  -e TYPESAFE_API_KEY="apikey_xxx_yyy" \
  -v /mnt/sata1-4/smartroute/config.yaml:/app/config.yaml \
  smartroute:latest
```

`--network host` 是必须的：容器内要访问 `127.0.0.1:9090`（OpenClash 控制器），
同时探活请求需要走宿主机的 Mihomo 路由。

部署后把 `config.yaml` 里的 `openclash.api` 改回 `http://127.0.0.1:9090`。

---

## 配置说明

```yaml
openclash:
  api: "http://192.168.3.2:9090"   # 本机开发填软路由 IP；部署到软路由后填 127.0.0.1
  secret: "np7fj4lv"               # 建议用环境变量 OPENCLASH_SECRET 注入
  provider: "glados"                # 节点来源 provider 名

targets:
  - name: "Gemini"
    group: "♊ Gemini"               # 要操作的策略组
    candidate_groups:               # 候选节点来源，可多个
      - "🇯🇵 日本节点"
      - "🇸🇬 新加坡节点"
      - "🇺🇲 Gemini节点"
    probe:
      url: "https://generativelanguage.googleapis.com/v1beta/models"
      ok_status: [200, 400, 401, 403, 429]
      blocked_status: [403]         # 需配合响应体解析，见 classify_body
      timeout: 8
      attempts: 2

engine:
  latency_weight: 0.40
  probe_weight: 0.45
  prior_weight: 0.15
  cooldown_sec: 90                  # 切换后多久不再切（防抖）
  hysteresis: 0.15                  # 新节点要领先多少比例才切（防抖）
  fail_threshold: 3                 # 失败计数达阈值进黑名单（实锤风控一次记 2）
  blacklist_sec: 1800
  interval_sec: 120
  probe_cache_sec: 1800             # 探活结果缓存（进入候选打分）
  ip_echo_url: https://api.ipify.org   # 探活出口 IP 回显，置空关闭
  criterion_window: 600             # 判据校准守卫窗口（秒）
  criterion_max_distinct: 3         # 窗口内多少个不同节点"实锤"后判据可疑
  patrol:                           # 巡游探检：主动逐个验证候选节点是否被风控
    enabled: false
    mode: roam                      # roam=借生产组切检（有打扰）; shadow=旁路监听（零打扰）
    top_k: 3                        # 每轮体检候选数（优先未实测、延迟低）
    interval_sec: 1800
    shadow_url: ""                  # 例: http://127.0.0.1:17890
    shadow_group: ""                # 例: ♟️ 探活组

jev:
  enabled: true
  model: "jev-latest"
  cache_sec: 3600                   # Jev 结果缓存，避免频繁调用
  apply_to_decision: true

server:
  dry_run: true                     # 先观察，确认无误后改 false
  token: ""                         # 面板访问令牌，留空不鉴权；也可用 SMARTROUTE_TOKEN
```

### 加一个新平台

在 `targets` 下追加一项即可，不用改代码：

```yaml
  - name: "TikTok"
    group: "🎵 TikTok"
    candidate_groups: ["♻️ 日本自动", "♻️ 新加坡自动"]
    service_desc: "TikTok 短视频平台，对出口 IP 的风控极敏感"
    probe:
      url: "https://www.tiktok.com/api/"
      ok_status: [200, 400, 403]
      blocked_status: [403]
```

---

## API

| 端点 | 说明 |
|---|---|
| `GET /api/status` | 运行状态、轮次、权重、累计切换/回滚、黑名单明细、OpenClash/Jev 状态 |
| `GET /api/nodes` | 全部节点延迟快照（含延迟历史，供走势图） |
| `GET /api/decisions` | 决策历史 |
| `GET /api/switches` | 只含真实动作的决策 |
| `GET /api/groups` | 目标策略组当前状态（含 `resolved` 下钻后的真实节点） |
| `GET /api/risks` | Gemini 风控档案：判定 + 证据原文 + 出口 IP + 来源 |
| `GET /api/health` | 连通性自检（OpenClash / Jev / 探活），结果缓存 60s，`?force=1` 强制重测 |
| `POST /api/run` | 立即执行一轮决策（与调度器互斥，冲突返回 409） |
| `POST /api/pause` | `{"on": true/false}` 暂停 / 恢复自动决策（手动不受影响） |
| `POST /api/force` | `{"target","node"}` 强制切换；dry-run 只预演可达路径 |
| `POST /api/unblacklist` | `{"node":"..."}` 或 `{"node":"*"}` 解除黑名单 |
| `POST /api/healthcheck` | 触发 provider 全量测速（约 1 分钟） |
| `POST /api/patrol` | 立即触发一轮风控巡检（逐个验证候选节点） |
| `WS /ws/live` | 实时推送决策、节点、组状态、风控档案 |

### 面板

单页控制台（`static/index.html`），提供：

- 决策流（相对时间 + 可达路径 + 导出 JSON/CSV）、Jev 地区先验、探活判据细分与历史
- 候选打分（延迟分 / 探活分 / 先验 / 综合 + 走势 sparkline，支持搜索 / 地区筛选）
- 多目标 Tab、当前节点高亮、黑名单明细与解除
- 运维控制：暂停引擎、强制切换、触发测速、三路自检
- 暗色模式（跟随系统）、WS 断线横幅与自动重连

### 访问控制（可选）

设置 `server.token`（或环境变量 `SMARTROUTE_TOKEN`）后，面板与 API 需要认证：

- 浏览器：Basic 认证，**用户名任意，密码 = token**
- 程序调用：`Authorization: Bearer <token>` 或 `?token=<token>`
- WebSocket：同上（浏览器自动带上 Basic 凭据；自定义客户端可用 `?token=`）

留空则不鉴权（默认）。局域网内共享部署时建议开启。

### 相关环境变量

| 变量 | 作用 |
|---|---|
| `TYPESAFE_API_KEY` | Jev API key（优先于 config） |
| `OPENCLASH_SECRET` | OpenClash 控制器 secret（优先于 config，避免入库） |
| `SMARTROUTE_TOKEN` | 面板访问令牌（优先于 config） |
| `GEMINI_API_KEY` | 可选：激活二级「真实业务探针」（generateContent）；不配则用无 key 边缘探针 |
| `SMARTROUTE_CONFIG` | config.yaml 路径 |
| `SMARTROUTE_DATA` | 决策落盘目录（默认 `./data`） |

决策历史落盘在 `data/decisions.jsonl`（已加入 .gitignore），重启后计数与历史自动恢复。

### 三级探针与巡游探检（判断节点是否被 Gemini 风控）

| 级别 | 手段 | 回答的问题 | 启用方式 |
|---|---|---|---|
| 1. edge | `GET /v1beta/models`（无 key） | Google 边缘收不收这个出口 | 默认 |
| 2. key | `POST :generateContent`（1 token） | **Gemini 业务层认不认这个出口** | 设 `GEMINI_API_KEY` |
| 3. shadow | Mihomo HTTP listener 绑定探活组 | **任意候选节点**认不认（零打扰）+ `DIRECT` 对照组 | 加一次监听配置（见下） |

二级探针是唯一能真正回答「Gemini 认不认这个出口 IP」的信号（key 走 TLS header，
不进 URL、不出库）。三级探针配合 `patrol.shadow_group` 切 `DIRECT` 即得
「家庭宽带对照组」，用于判据仲裁（见上文「判定必须解析错误信封」）。

**旁路监听一次性配置**（OpenClash 自定义配置，不被订阅更新覆盖）：

```yaml
# Mihomo 配置片段：探活专用 inbound，流量按绑定组路由
listeners:
  - name: probe-in
    type: http
    port: 17890
    listen: 127.0.0.1
    proxy: ♟️ 探活组
proxies:
  - name: ♟️ 探活组          # 不被任何生产规则引用，随便切
    type: select
    proxies: [DIRECT, 🇯🇵 日本节点, 🇸🇬 新加坡节点, 🇺🇲 Gemini节点]
```

然后 `engine.patrol` 配 `mode: shadow`、`shadow_url: http://127.0.0.1:17890`、
`shadow_group: ♟️ 探活组`。不想加配置就用默认 `mode: roam`
（借生产组「切过去→探活→切回来」，每次切换毫秒级打扰，dry-run 自动跳过）。

风控判定结果统一进**风控档案**（`/api/risks` + 面板「Gemini 风控档案」卡）：
节点 / verdict / 证据原文 / 出口 IP / 来源（切换验证 · 巡检 · 对照组），全程可审计。

---

## 已知限制

1. **切换会短暂中断该组的连接。** 这是 Mihomo 切组的固有代价，任何方案都躲不掉。
   冷却期（`cooldown_sec`）+ 滞后阈值（`hysteresis`）用来把切换频率压到最低。

2. **本机开发时探活测的是本机出口，不是节点出口。**
   因为在 Windows 上无法让单个进程走指定节点。部署到 iStoreOS 后，
   宿主机流量本身走 Mihomo，探活才有实际意义。

3. **可达路径深度上限 3 层。** `find_path_to` 的 `max_depth`。
   超过这个深度说明组结构过于复杂，建议直接改软路由配置。

4. **Jev 的判断是概率不是事实。** 所以必须配合探活验证 ——
   先验给出方向，事实做最终裁决。

---

## License

MIT
