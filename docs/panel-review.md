# SmartRoute 面板分析与改进建议

> 分析对象：`static/index.html`（SmartRoute 控制台）及其数据契约 `app.py` / `core/*`。
> 版本：v1.0.2（`dry_run` 已开启真实切换）

---

## 一、面板现状

### 1.1 信息结构（6 个区块）

| 区块 | 内容 | 数据源 |
|---|---|---|
| Header | 标题 + 3 枚状态徽章（OpenClash / Jev / 模式）+「立即决策」 | `/api/status` |
| 横幅 | DRY-RUN 警示 / last_error | `/api/status` |
| 统计卡 ×3 | 决策轮次、节点池、切换/回滚 | `/api/status`、`/api/nodes` |
| 决策流 | 时间/动作/切换/理由（含可达路径） | `/api/decisions`、WS |
| 右栏 | Jev 地区先验条形图 + 最新探活 | 最近一条 decision 内嵌 |
| 候选打分 | 节点 × 7 列（延迟分/先验/综合/备注） | 最近一条 decision 内嵌 |
| 策略组 | 组名/类型/当前指向/成员数 | `/api/groups` |

### 1.2 数据流

```
loadHistory()  每 30s  ─→ /api/decisions + /api/nodes     （全量重渲染）
refreshStatus() 每 10s ─→ /api/status
loadGroups()   仅启动时 ─→ /api/groups                      （★之后只靠 WS）
WebSocket /ws/live     ─→ decision / nodes / groups / error 推送
按钮「立即决策」       ─→ POST /api/run（阻塞至整轮跑完）
```

### 1.3 设计上值得保留的优点

1. **安全第一的 DRY-RUN 横幅** —— 模式徽章 + 顶部警示条，上线前的防呆做得好。
2. **决策可解释性** —— 理由列展示打分对比（`0.83 vs 0.71`）、冷却倒计时、
   以及嵌套组的可达路径 `♊ Gemini → 🚀 默认代理 → … → JP-X5-1`，这正是本项目
   核心技术点，展示到位。
3. **三路信号的可视化意图清晰** —— 地区先验条、候选打分条、探活卡片各司其职。
4. **安全渲染** —— 全量输出过 `esc()` 转义，无 `innerHTML` 裸插值。
5. **零构建单文件** —— 436 行 HTML/CSS/JS，随 FastAPI 直接托管，部署极简。
6. **视觉层次** —— 浅色主题、卡片化、地区色标、动作三色 chip（switch/hold/rollback），
   信息密度与留白平衡得当。

---

## 二、问题清单

### P0 · 显示 Bug（会误导使用者）

#### 1. 「切换 / 回滚」统计卡从未被填充
HTML 里 `id="s-sw"` 初始为 `–`，但整个 `<script>` 中**没有任何赋值**（grep 确认 0 处）。
用户永远看到 `– / 黑名单 0`，会以为从未切换过 —— 而 v1.0.2 已经真实切换了。
`/api/switches` 里明明有现成数据。

#### 2. WebSocket 推送会把决策历史表清成 1 行
`loadHistory()` 渲染了 40 条历史，但**从不写 `window.__dec`**；
WS `decision` 消息到达时 `JSON.parse(window.__dec||'[]')` 得到 `[]`，
`unshift` 后只有 1 条就 `renderDecisions` —— 表格被瞬间清空。
30s 后下一次轮询又恢复 40 条，界面在 1 行 ↔ 40 条之间反复跳动。
根因：决策列表状态存在 `window.__dec` 这个 JSON 字符串 hack 里，没有统一 store。

#### 3. OpenClash 徽章语义错位（双重误报）
- `/api/status` 成功 → 无条件显示「OpenClash 正常」。但 `/api/status`
  **根本不检查 OpenClash**（没有调 `oc.version()`），徽章是假绿。
- `/api/status` 失败 → 显示「OpenClash 断开」。但 fetch 失败意味着
  **FastAPI 面板本身失联**，与 OpenClash 无关，冤枉了对象。
真正的连通性自检在 `/api/health`（分别验 OpenClash / Jev / 探活），面板却毫无入口。

#### 4. 评分公式与表格列不一致：权重最大的信号没有列
图例写明 `综合 = 延迟×0.40 + 探活×0.45 + 先验×0.15`，但候选表列只有
「延迟分 / 先验 / 综合」—— **占 45% 权重的探活分没有展示列**。
`Candidate.to_dict()` 里的 `probe_score` 已经下发，前端直接丢弃。
用户无法核对综合分怎么算出来的，恰好丢掉了项目最引以为傲的那路信号。

#### 5. 公式权重硬编码在 HTML 图例
`0.40 / 0.45 / 0.15` 写死在页面里，而真实权重来自 `config.yaml` 的 `engine` 段。
用户一改配置，面板就在说谎。`/api/status` 也不输出权重（grep 确认）。

### P1 · 功能缺口（后端能力没用上）

#### 6. 多目标显示错乱
面板写死 `lg-tgt = "目标 Gemini"`，候选/先验/探活永远渲染 `decisions[0]`。
`config.yaml` 的 `targets` 是数组（README 明示可加 TikTok），一旦多目标，
不同 target 的决策交错进入同一信息流，右栏和候选表会**张冠李戴**。
WS `hello` 消息里的 `config.targets` 列表已被发送但前端从未使用。

#### 7. 黑名单只剩一个数字
`/api/status` 下发 `blacklist{节点: 已过秒数}` 和 `fail_count{节点: 次数}`，
面板只显示 `黑名单 N`。看不到：哪些节点、为何被拉黑、还剩多久解封、手动解除。
候选表的「黑名单中」备注也没有解封倒计时。

#### 8. 探活面板信息价值不足
- 只显示最近一次探活，无历史（「事实推翻先验」的 rollback 是最宝贵的学习素材）。
- `verdict`（`auth_ok` / `geo_blocked` / `unknown_403`）没有展示 —— 这正是
  README 引以为傲的「403 ≠ 风控」区分，面板却只给「放行/被风控」二值。
- DRY-RUN 时探活测的是**本机出口而非候选节点**（README 已知限制 #2），
  面板却显示 `验证节点 __dry_run_current__` 这种内部字符串，用户极易误读。
- 内部标识 `__dry_run_current__` / `__direct__` 直接泄漏到 UI。

#### 9. 缺少手动控制手段
只有「立即决策」一个按钮。运维面板通常还需要：
- 强制指定节点（先预演路径，再执行）；
- 暂停 / 恢复引擎（当前改配置要重启容器）；
- 手动解除黑名单；
- 触发 provider 全量测速（`oc.healthcheck()` 已实现，无 UI 入口）。
另外 `dry_run: false` 之后「立即决策」可能真的切节点，按钮没有任何确认步骤。

#### 10. 时间戳只有 `HH:MM:SS`
`Decision.to_dict()` 的 `time` 用 `strftime("%H:%M:%S")`，
隔天的历史看起来像刚刚发生。`ts` 字段已下发，前端可自行格式化为
日期或相对时间（「3 分钟前」）。

#### 11. 候选表截断无提示
`list.slice(0,22)` 静默截断，56 个节点时用户不知道下面还有 34 个，
也没有「显示全部 / 搜索 / 按地区筛选」。

#### 12. 「当前生效节点」没有高亮
`isPick` 只匹配 `lastDecision.to_node`（最近一次**切换目标**）。
hold 决策 `to_node=null`，于是当前实际生效的节点在候选表里毫无标记。
应从 `/api/groups` 的 `now`（经 `resolve_now` 下钻后的真节点）反查高亮。

#### 13. 策略组表可能永久过期
`loadGroups()` 只在启动时调用一次，之后只靠 WS `groups` 推送。
WS 一旦断线重连失败，组状态表就停在启动那一刻（决策表/统计卡还有轮询兜底，它没有）。

#### 14. 决策历史不落盘
后端 `deque(maxlen=200)` 纯内存，重启即丢。面板也没有导出 CSV/JSON 的入口。
rollback 事件（先验 vs 事实的偏差记录）是最有复盘价值的数据。

### P2 · 体验与健壮性

15. **暗色模式缺失** —— 无 `prefers-color-scheme` 支持。路由器/NOC 面板夜间使用是常态。
16. **无趋势图** —— provider 的 `history.delay`、决策 `ts` 序列都可做 sparkline
    （延迟趋势 / 切换频次 / 分数变化），手绘 canvas 即可，不必引图表库。
17. **WS 无心跳检测与退避** —— 服务端每 20s 发 `ping`，前端不看；
    `onclose` 固定 3s 重连，无指数退避，也没有「实时连接已断开」横幅。
18. **错误态与空态不分** —— 各处 `catch(e){}` 吞掉异常，「尚无数据」和
    「API 挂了」在界面上长得一样，排障全靠猜。
19. **「立即决策」fetch 无超时** —— 一轮决策可能含 Jev（45s×4 重试）+ 探活，
    后端挂起时按钮永远停在「决策中…」（无 AbortController）。
20. **表格能力** —— 无排序（想按延迟/地区排）、无搜索；组表无滚动容器
    （`.scroll` 只给了前两张表）；行重绘丢失滚动位置。
21. **无障碍与细节** —— 装饰性 `dot`/徽章可加 `aria-label`；无 favicon；
    新决策到达时 `document.title` 无未读闪烁；「节点池」统计的是全 provider
    节点，与候选池（`candidate_groups` 展开）口径不同，两处数字可能对不上，易困惑。
22. **`esc()` 未转义引号** —— 当前无属性注入风险（动态值未进属性位），
    但属于埋雷，补齐 `"` `'` 转义成本极低。

### P3 · 架构与安全（面板相关）

23. **面板与 API 无认证** —— 局域网内任何人打开 `:8000` 就能 `POST /api/run`
    切换策略组。v1.0.2 起是真实切换，建议至少加 Basic Auth / token，
    或绑定 localhost + 反代鉴权。
24. **`config.yaml` 明文提交了 `secret: np7fj4lv`**（Mihomo 控制器密钥），
    应改环境变量注入，轮换现密钥。
25. **`/api/run` 与 scheduler 无互斥** —— 手动触发和定时循环可并发进入
    `run_cycle`，对同一 target 双写决策、竞态切换。加 `asyncio.Lock` 即可。
26. **前端状态管理** —— `window.__dec`、`lastDecision`、各 DOM 散落状态应收敛成
    一个 store（`{status, decisions, nodes, groups}`），WS 与轮询都走同一 update 入口，
    P0-2 那类 bug 自然消失。单文件超过 ~800 行后再拆 `app.js`。

---

## 三、改进建议路线图

| 优先级 | 改动 | 涉及 | 工作量 |
|---|---|---|---|
| P0 | 填充「切换/回滚」卡（接 `/api/switches`） | index.html | ★ |
| P0 | 统一决策 store，修 WS 清史 bug | index.html | ★★ |
| P0 | 徽章语义拆分：面板后端 / OpenClash / Jev 三态（接 `/api/health`） | index.html、app.py | ★ |
| P0 | 候选表加「探活分」列；图例权重改为读 `/api/status` | index.html、app.py | ★ |
| P1 | 目标 Tab / 下拉（多 targets 分别渲染） | index.html | ★★ |
| P1 | 黑名单明细卡（剩余时间、失败次数、解除按钮） | index.html、app.py | ★★ |
| P1 | 手动控制：强制切换（预演回显路径）、暂停引擎、触发测速 | index.html、app.py | ★★★ |
| P1 | 探活卡展示 verdict 细分 + DRY-RUN 口径提示 + 探活历史时间线 | index.html | ★★ |
| P1 | 时间戳改「相对时间 + 悬停日期」；决策落盘 jsonl + 导出 | index.html、app.py | ★★ |
| P2 | 暗色模式、sparkline 趋势、筛选/搜索、截断提示、空态/错误态、WS 退避与断线横幅 | index.html | ★★★ |
| P3 | 面板鉴权、secret 出库、run_cycle 互斥锁 | app.py、config | ★★ |

★ ≈ 30min，★★ ≈ 半天，★★★ ≈ 1~2 天

---

## 四、如果只做 5 件事

1. **修三个显示 bug**（s-sw 空卡、WS 清史、OpenClash 假绿/假红）——面板可信度的底线；
2. **补上探活分列 + 动态权重图例**——让「三路信号」真正被看见、可核对；
3. **目标 Tab**——多平台配置一旦启用，现在的面板会直接显示错误数据；
4. **黑名单明细 + 当前节点高亮**——回答「现在走的是谁、谁被禁了、为什么」；
5. **探活 verdict 细分 + DRY-RUN 口径提示**——把项目最核心的判据优势
   （403 ≠ 风控）显性化，同时避免用户误读本机出口的探活结果。

以上 1–5 均为纯增量改动，不推翻现有单文件结构。

---

## 附：实施状态（2026-09-21，v1.1.0）

| 项 | 状态 | 落点 |
|---|---|---|
| P0-1 切换/回滚统计卡 | ✅ 已修 | `/api/status.counts` + 落盘延续 |
| P0-2 WS 清史 bug | ✅ 已修 | 前端统一 store，`decKey` 去重合并 |
| P0-3 徽章语义 | ✅ 已修 | 面板/OpenClash/Jev/模式 四枚徽章分离 |
| P0-4 探活分列 | ✅ 已补 | 候选表新增列；engine 把 probe_cache 接入打分 |
| P0-5 权重硬编码 | ✅ 已修 | `/api/status.weights` 动态图例 |
| P1-6 多目标 | ✅ 已加 | 目标 Tab，按 target 过滤与渲染 |
| P1-7 黑名单明细 | ✅ 已加 | 明细 + 剩余时间 + 单个/全部解除 |
| P1-8 探活信息 | ✅ 已加 | verdict 细分 + DRY-RUN 口径提示 + 历史时间线 |
| P1-9 手动控制 | ✅ 已加 | 强制切换 / 暂停引擎 / 触发测速 / 三路自检 |
| P1-10 时间戳 | ✅ 已改 | 相对时间 + 悬停完整时间 |
| P1-11 截断提示 | ✅ 已加 | 显示 N/M + 显示全部 + 搜索/地区筛选 |
| P1-12 当前节点高亮 | ✅ 已加 | `resolve_now` 下钻结果标「当前」 |
| P1-13 组表过期 | ✅ 已修 | 轮询兜底（30s） |
| P1-14 决策落盘 | ✅ 已加 | `data/decisions.jsonl` + 导出 JSON/CSV |
| P2-15 暗色模式 | ✅ 已加 | `prefers-color-scheme` 全套变量 |
| P2-16 趋势图 | ✅ 已加 | 节点延迟走势 sparkline（SVG） |
| P2-17 WS 健壮性 | ✅ 已加 | 指数退避 + 心跳假死检测 + 断线横幅 |
| P2-18 空态/错误态 | ✅ 已区分 | alertbar + 各卡 empty 文案 |
| P2-19 run 超时 | ✅ 已加 | AbortController 240s |
| P2-20 表格能力 | ✅ 已加 | 搜索 / 地区筛选 / 显示全部 |
| P2-21 细节 | ✅ 已加 | favicon、标题未读闪烁、节点池口径说明 |
| P2-22 esc 引号 | ✅ 已修 | `"` `'` 转义补齐 |
| P3-23 面板鉴权 | ✅ 已加 | 可选 token（Basic / Bearer / ?token=） |
| P3-24 secret 出库 | ✅ 已支持 | `OPENCLASH_SECRET` 环境变量注入 |
| P3-25 run_cycle 竞态 | ✅ 已修 | asyncio.Lock，冲突 409 |
| P3-26 前端状态管理 | ✅ 已收敛 | 单一 store；单文件仍维持（部署简单优先） |
