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
- **探活分**：真实请求的响应耗时与判据结果，被风控直接归零
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

### 4. 403 不能一律当成"被风控"

Google API 的 403 有两种含义完全不同的情况：

| 响应体特征 | 真实含义 | 处理 |
|---|---|---|
| `unregistered callers` / `API key` | IP 已放行，只是没给 key | **可用** |
| `location` / `country` / `region` | IP 被地区限制 | 被风控 |

只看状态码会把所有节点误判为被风控，导致全盘回滚 —— 这是会造成实际故障的 bug。
`classify_body()` 解析响应体来做区分。

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
  secret: "np7fj4lv"
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
  fail_threshold: 3                 # 连续失败几次进黑名单
  blacklist_sec: 1800
  interval_sec: 120

jev:
  enabled: true
  model: "jev-latest"
  cache_sec: 3600                   # Jev 结果缓存，避免频繁调用
  apply_to_decision: true

server:
  dry_run: true                     # 先观察，确认无误后改 false
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
| `GET /api/status` | 运行状态、轮次、黑名单 |
| `GET /api/nodes` | 全部节点延迟快照 |
| `GET /api/decisions` | 决策历史 |
| `GET /api/switches` | 只含真实动作的决策 |
| `GET /api/groups` | 目标策略组当前状态 |
| `GET /api/health` | 连通性自检（OpenClash / Jev / 探活） |
| `POST /api/run` | 立即执行一轮决策 |
| `WS /ws/live` | 实时推送决策、节点、组状态 |

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
