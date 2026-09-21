# Changelog

版本号遵循 `MAJOR.MINOR.PATCH`：
- MAJOR：不兼容的配置或 API 变更
- MINOR：新增功能（新平台支持、新决策维度）
- PATCH：bug 修复、参数微调

---

## [1.2.0] - 2026-09-21

Gemini 风控判定升级为「结构化错误信封 + 证据链 + 对照组仲裁」，
从关键词猜谜变为有证据、有对照、可审计的判定。

### 修复

- **v1 判据会把最常见的地区风控判反**：`400 FAILED_PRECONDITION
  "User location is not supported"` 被 `status in (400,401) -> auth_ok`
  直接当成「IP 已放行」。现在任何状态码都先查 geo/abuse 短语
- **403 全文扫 `location/region` 单词误命中字段名**：改为解析
  `{"error":{code,status,message}}` 信封 + 多词精确短语，不再全文扫
- `unknown_403` 一律当风控导致的全盘回滚：`unknown` 改为
  **验证不通过但绝不拉黑**（留证待判）

### 新增

- **判定分级**：`geo_blocked`（地区风控实锤）/ `ip_flagged`（滥用封禁）/
  `quota_limited`（配额受限，不拉黑）/ `auth_ok`（IP 已放行）/ `ok` /
  `unknown`（留证）。每次判定带 `evidence` 证据原文
- **加重惩罚**：实锤风控一次记 2 个失败计数（两次独立确认即可拉黑），
  unreachable/unknown 维持 1
- **对照组仲裁（L3）**：实锤前经旁路监听切 `DIRECT`（家庭宽带）复测 ——
  对照组同中 → 判据可疑暂缓拉黑；对照组正常 → 风控确认
- **判据校准守卫**：600s 内 ≥3 个不同节点被同套判据「实锤」→
  全局 `criterion_suspect` 暂缓一切拉黑，任一探活转正常自动解除
- **二级探针（L2，可选）**：`GEMINI_API_KEY` 激活 `POST :generateContent`
  真实业务探针（1 token，key 走 TLS header 不进 URL）；不配则用无 key 边缘探针
- **三级探针 / 巡游探检（L4）**：
  - `patrol.mode: shadow` —— 经 Mihomo HTTP listener（`proxy` 绑定探活组）
    对任意候选节点预检，生产组零接触；切 `DIRECT` 即对照组（dry-run 可跑）
  - `patrol.mode: roam` —— 零配置，借生产组「切→探→切回」，dry-run 自动跳过
  - `POST /api/patrol` 手动触发；面板「立即风控巡检」按钮
- **风控档案**：`node_risk` 记录 verdict / 证据原文 / 出口 IP / 来源，
  `GET /api/risks` + 面板「Gemini 风控档案」卡（实锤置顶）
- **出口 IP 回显**（`engine.ip_echo_url`）：审计「测的到底是谁」，
  切换后出口没变（路由未生效）一眼可辨
- 面板：探活卡展示判定依据与出口 IP、`criterion_suspect` 全局告警横幅

---

## [1.1.0] - 2026-09-21

面板可用性与运维能力大版本：修复 3 个显示 bug，补上「探活分」可视化与多目标支持，
新增暂停 / 强制切换 / 解除黑名单 / 触发测速 / 自检等运维接口。

### 修复（P0）

- **「切换 / 回滚」统计卡从未被填充**：改为读取累计计数（`switch / rollback / hold`，
  随决策落盘跨重启延续）
- **WebSocket 推送会把决策历史清成 1 行**：前端引入统一状态存储，
  WS 推送与轮询共用同一合并入口（按 `ts|target|action` 去重）
- **OpenClash 徽章假绿 / 假红**：徽章拆分为「面板 / OpenClash / Jev / 模式」四枚，
  OpenClash 与 Jev 状态来自真实连接结果，面板失联单独显示
- **候选表补「探活分」列**；探活结果（probe_cache，带 TTL）现在真正进入候选打分，
  被风控 / 未通过的节点直接压 0 分
- **评分公式图例跟随配置**：`/api/status` 输出 `weights`，面板动态渲染，
  改 `config.yaml` 权重后面板不再说谎

### 新增（P1）

- **多目标 Tab**：多平台配置时按 target 过滤决策流，先验 / 探活 / 候选分别渲染，
  不再张冠李戴
- **黑名单明细**：节点名、剩余时间、失败次数，支持单个 / 全部解除
- **运维控制卡**：暂停 / 恢复自动决策、强制切换（dry-run 只预演可达路径）、
  触发 provider 全量测速、三路连通性自检（`/api/health` 结果缓存 60s）
- **探活判据细分**：展示 `verdict`（IP 已放行 vs 地区受限 vs 403 未判别），
  DRY-RUN 口径提示（本机出口 ≠ 候选出口），探活历史时间线
- **决策落盘** `data/decisions.jsonl`：重启恢复计数与历史，面板可导出 JSON / CSV
- **时间改相对显示**（悬停看完整时间）；候选表截断提示 + 显示全部 + 搜索 / 地区筛选
- **当前生效节点高亮**（策略组 `now` 经 `resolve_now` 下钻后标记「当前」）

### 体验（P2）

- 暗色模式（`prefers-color-scheme`）；节点延迟走势 sparkline
- WS 指数退避重连 + 心跳假死检测 + 断线横幅；空态 / 错误态区分
- 「立即决策」240s 超时保护；标题未读闪烁提示；favicon

### 安全 / 服务端（P3）

- **可选访问令牌**：`server.token` 或 `SMARTROUTE_TOKEN`（Basic 认证，
  用户名任意、密码 = token；WS 支持 `?token=`）
- **secret 环境变量注入**：`OPENCLASH_SECRET` 优先于 config
- **run_cycle 互斥锁**：手动 `/api/run` 与调度器不再竞态双切（冲突返回 409）
- 组状态输出 `resolved`（嵌套 now 下钻结果）；节点快照带延迟历史
- 决策数据目录 `data/` 已加入 .gitignore

---

## [1.0.2] - 2026-09-20

开启真实切换，并补上部署时关键配置项的同步机制。

### 新增

- **`config.yaml` 关键开关自动同步**：此前远端配置策略是「存在则保留」，
  导致改了 `dry_run`、`openclash.api` 这类值后远端不跟进，出现
  「配置改了但服务行为没变」的假象。现在这些开关始终以本地为准。
  - 采用「拉远端原文 → 只改目标行 → 整文件写回」，**线上其他改动不受影响**
  - `api` 键带段落限定（`config.yaml` 里 `openclash.api` 与 `jev.api` 同名，
    直接全局替换会改错）

### 变更

- `server.dry_run` 由 `true` 改为 **`false`** —— 决策正式生效，会真实切换策略组

### 注意

- 开启后冷却期 90s、滞后阈值 0.15、失败 3 次拉黑 1800s 均生效；
  若出现意料外的频繁切换，先看 `/api/decisions` 的 `reason` 字段

---

## [1.0.1] - 2026-09-20

修复首次部署到 iStoreOS 时暴露的两个构建/运维问题。

### 修复

- **`requirements.txt` 内容串行**：文件头部误混入了 Dockerfile 的
  `FROM`/`python:3.11-slim` 两行，导致 `pip install` 报
  `Invalid requirement: 'python:3.11-slim'`。已改为纯依赖列表
- **国内网络下 pip 装包失败**：iStoreOS 直连 `pypi.org` 频繁出现
  `SSL: UNEXPECTED_EOF_WHILE_READING`。Dockerfile 改用清华 TUNA 镜像源
- **日志泄漏 API key**：`release.py` 会把 TypeSafe API key 明文打印到
  部署日志。新增 `_mask()` 脱敏，只保留首 8 位与末 4 位

### 变更

- 默认 `config.yaml` 的 `openclash.api` 由 `http://192.168.3.2:9090`
  改为 `http://127.0.0.1:9090`（容器使用 `--network host`，走回环更稳）

---

## [1.0.0] - 2026-09-20

首个可用版本。从原演示原型（`legacy/`）重写为生产可用的智能选路服务。

### 新增

- **采集层**：从 `GET /providers/proxies/{provider}` 读取真实节点延迟与存活状态
- **路径解析**：`find_path_to()` 递归查找跨组切换路径。
  Mihomo 只能切换策略组的直接成员，而平台组的成员往往全是子组，
  必须逐级下钻（如 `♊ Gemini → 🚀 默认代理 → 🔯 日本故转 → JP-X5-1`）
- **探活层**：真实请求目标平台端点，解析响应体区分"缺 key 的 403"与"地区风控的 403"
- **先验层**：接入 TypeSafe Jev，做地区级风险判断（原子问题 + 结果缓存）
- **决策层**：三路信号加权打分 + 切换 → 验证 → 不通过则回滚的闭环
- **防抖机制**：冷却期、滞后阈值、失败黑名单
- **DRY-RUN 模式**：只计算与记录，不真正切换
- **Web 看板**：FastAPI + WebSocket + 单文件前端
- **配置化**：新增平台只需改 `config.yaml`，无需改代码
- **Docker 部署**：多阶段构建，适配 iStoreOS

### 修复（相对原演示原型）

- 选路逻辑由 `random.choice(nodes)` 改为基于真实延迟 + 平台可用性判断
- 概率分布不再随机生成（原版概率和不等于 1）
- WebSocket 连接不再泄漏（原版连接只加不减）
- 不再向所有客户端广播同一条记录

### 关键实现细节

- `resolve_now()` 递归下钻策略组的 `now`，处理嵌套引用
- 两个网络隔离坑：访问局域网 API 必须直连；探活需清代理环境变量
- Jev 是 gut-check 模型，不做多因素推理，必须拆成原子问题；
  输出质量取决于 state 的信息密度
