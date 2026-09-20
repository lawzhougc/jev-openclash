# Changelog

版本号遵循 `MAJOR.MINOR.PATCH`：
- MAJOR：不兼容的配置或 API 变更
- MINOR：新增功能（新平台支持、新决策维度）
- PATCH：bug 修复、参数微调

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
