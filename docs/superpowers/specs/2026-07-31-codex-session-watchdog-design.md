# Codex 本地会话监控与安全续跑设计

- 日期：2026-07-31
- 状态：已由用户确认
- 项目：Local Project Console

## 1. 背景与目标

现有控制台负责本地项目的启动、关闭、端口检测和网页入口管理。新增功能用于监控用户明确登记的本地 Codex 会话，在其绑定的 OpenAI 兼容 API 渠道恢复可用、且会话上一轮因明确的可恢复 API 异常中断时，自动向该会话发送续跑提示词。

本功能必须优先避免误续跑和重复续跑。漏掉无法可靠判断的异常是可接受的；把正常完成、等待用户或手动中止的会话错误续跑是不可接受的。

## 2. 已确认的产品边界

1. 监控器跟随控制台后端进程启动和停止。关闭 `start-console.bat` 启动的终端后，监控器停止。
2. 关闭浏览器页面不影响监控，只要控制台后端仍在运行。
3. 首版只支持当前设备上的本地 Codex 会话，不支持 ChatGPT 云任务、远程主机任务或其他聊天平台。
4. 不写死设备名、用户名、Codex 安装目录或项目绝对路径。默认 Codex host 是运行控制台的当前设备。
5. 只监控用户显式添加并启用的会话。系统不会自动把所有 Codex 会话加入监控目录。
6. 添加会话时允许手填会话名称和 thread ID，也允许用户主动打开本地会话选择器辅助填写。选择器不会改变监控目录。
7. 支持多个 OpenAI 兼容 API 渠道，每个会话必须绑定一个渠道。
8. 全局默认检查周期为 15 分钟；单个会话可以覆盖该值；最低允许值为 5 分钟。
9. 每个会话支持独立续跑提示词，默认值为 `继续当前开发任务`。
10. 首版不做额外的“目标是否达成”语义判断。正常完成的 turn 本来就不会续跑。
11. 未来如增加目标判断，由目标会话自行生成结构化完成标记，控制台只验证和记录，不再调用第二个模型重复判断。

## 3. 信息架构与隔离边界

### 3.1 原项目控制台

原有项目总览、筛选、项目列表、启动/关闭、日志、端口和网页状态行为保持不变。

左侧导航在现有项目筛选区域下方新增一个独立的“会话监控”入口。点击原有“全部项目、运行中、已关闭、待配置、网页入口”等筛选项仍返回原项目总览。

### 3.2 会话监控页面

“会话监控”入口打开独立页面 `/watchdog`。该页面使用横向标签组织三个子视图：

1. 监控会话
2. 执行记录
3. 添加监控渠道

会话页面不显示项目启动命令、端口、网页入口或 `projects.json` 中的项目数据。

### 3.3 代码与数据隔离

- 现有 `index.html` 继续承担项目控制台。
- 新增独立 watchdog 页面、样式和脚本。
- 现有 `/api/projects` 路由和 `projects.json` 保持原义。
- 新功能 API 全部位于 `/api/watchdog/*`。
- 新功能数据保存在独立的 `watchdog.db`。
- 两个模块只共享 Python HTTP 服务进程、基础视觉 token 和左侧入口。

## 4. 总体架构

### 4.1 WatchdogScheduler

后台调度线程随控制台后端启动。它根据各会话的 `next_check_at` 选择到期且启用的会话，不使用固定的全量 15 分钟扫描。

调度器使用可中断等待，在控制台退出时接收 shutdown event 并停止，不留下独立后台服务。

### 4.2 ChannelProbe

负责向用户配置的 OpenAI 兼容渠道发送最小真实模型请求，返回标准化结果：

- `healthy`
- `auth_error`
- `rate_limited`
- `upstream_error`
- `network_error`
- `protocol_error`
- `other_http_error`

同一轮有多个到期会话绑定同一渠道时，只探测一次并复用结果。短时间重试可以复用不超过 60 秒的健康结果；超过 60 秒必须重新探测。

### 4.3 CodexAdapter

负责：

- 读取指定本地 thread ID 的概要和最新 turn。
- 标准化 thread/turn 状态。
- 向指定 thread ID 发送续跑提示词。
- 在用户主动打开选择器时，列举有限数量的本地会话供选择。

首选实现使用 Codex App Server。该接口当前是实验性能力，因此实现前必须先完成协议集成验证。适配器不可用或协议不兼容时，系统进入“Codex 连接不可用”状态，只记录，不续跑。

系统不通过解析 Codex 私有会话文件作为自动续跑兜底，也不在无可靠状态判断时直接调用 `codex exec resume`。

### 4.4 DecisionEngine

接收渠道结果、会话最新 turn、恢复规则和历史 incident，产生唯一决策：

- `silent_channel_unavailable`
- `silent_session_running`
- `silent_session_completed`
- `silent_manual_attention`
- `silent_unknown`
- `resume_candidate`
- `resume_sent`
- `resume_action_failed`
- `silent_already_handled`

DecisionEngine 是纯逻辑组件，不直接访问网络或数据库，以便使用表驱动测试覆盖全部分支。

### 4.5 WatchdogStore

使用 Python 标准库 `sqlite3` 保存配置、调度状态、incident 和执行记录。事务负责保证并发检查和重复调度不会产生重复发送。

### 4.6 SecretStore

首版 Windows 实现使用当前用户范围的 DPAPI 加密和解密 API Key。SecretStore 使用独立接口，开源版本不把 Windows、设备名或固定路径散落在业务逻辑中。

## 5. 数据模型

### 5.1 `watchdog_settings`

- `default_interval_minutes`：默认 15
- `minimum_interval_minutes`：固定下限 5
- `record_retention_days`：默认 90
- `record_limit`：默认 10000
- `scheduler_enabled`：控制台运行期间是否启用调度
- `resume_actions_enabled`：全局续跑安全开关，默认关闭；专用测试会话验证通过后才允许开启

### 5.2 `api_channels`

- `id`
- `name`
- `base_url`
- `probe_url_override`：可选高级配置
- `model`
- `api_key_ciphertext`
- `timeout_seconds`
- `enabled`
- `last_probe_status`
- `last_http_status`
- `last_probe_detail`
- `last_checked_at`
- `created_at`
- `updated_at`

Base URL、模型和 API Key 是必填项。默认探测地址按 OpenAI 兼容 Chat Completions 规则从 Base URL 构造；不兼容常见路径的渠道可以填写覆盖地址。

### 5.3 `monitored_sessions`

- `id`
- `name`
- `thread_id`：唯一
- `host_kind`：首版固定逻辑值 `local`，不是设备名
- `channel_id`
- `interval_minutes`：空值表示使用全局默认值
- `resume_prompt`
- `enabled`
- `last_session_state`
- `last_turn_id`
- `last_check_result`
- `last_checked_at`
- `next_check_at`
- `created_at`
- `updated_at`

绑定渠道只是用户声明的监控前提，不会改变 Codex 会话自身的模型或渠道配置。用户应确保绑定渠道与目标会话实际使用的渠道一致。

### 5.4 `recovery_rules`

- `id`
- `name`
- `scope`：`channel` 或 `session_turn`
- `match_type`：HTTP 状态码、错误类别或安全正则模式
- `pattern`
- `enabled`
- `description`

首版内置并启用：429、502、503、504、请求超时、连接超时、连接重置。后续发现新的明确可恢复异常时可以增加规则，不修改 DecisionEngine 主流程。

### 5.5 `recovery_incidents`

- `id`
- `fingerprint`：唯一
- `session_id`
- `turn_id`
- `error_signature`
- `first_seen_at`
- `status`：candidate、sending、sent、failed、dismissed
- `attempt_count`
- `last_attempt_at`
- `resolved_at`

指纹由 `session_id + latest_turn_id + normalized_error_signature` 生成。同一个异常 turn 只对应一个 incident。

### 5.6 `monitor_runs`

- `id`
- `session_id`
- `channel_id`
- `started_at`
- `finished_at`
- `channel_status`
- `http_status`
- `session_state`
- `turn_id`
- `error_category`
- `decision`
- `resume_attempt`
- `duration_ms`
- `detail_sanitized`

记录不保存 API Key、完整 API 请求体、完整模型响应或完整续跑提示词。

## 6. 调度与决策流程

每个到期会话按以下顺序处理：

1. 确认会话仍启用并获取有效检查周期。
2. 获取绑定渠道；渠道不存在或禁用时记录并静默结束。
3. 探测渠道，或复用本轮同渠道探测结果。
4. 渠道不是 `healthy` 时记录状态，不读取会话，不发送提示词。
5. 渠道健康时，通过 CodexAdapter 直接读取该 thread ID。
6. thread 不存在或 CodexAdapter 不可用时记录并静默结束。
7. 根据最新 turn 状态进行分类。
8. 只有明确的 `failed/interrupted` 且错误命中启用的可恢复规则时，创建或读取 incident。
9. 全局续跑安全开关关闭时，只记录 `resume_candidate_observed`，不创建发送动作。
10. 获取单会话锁，检查 incident 是否已成功处理、是否正在发送、是否超过尝试上限。
11. 再次确认渠道健康结果未超过 60 秒；过期则重新探测。
12. 发送该会话配置的续跑提示词。
13. 成功后把 incident 标记为 `sent`；失败则记录 `resume_action_failed` 并进入退避。
14. 写入执行记录并计算 `next_check_at`。

## 7. 会话状态映射

thread 的 `active/idle/notLoaded` 只作为概要，最终决策以最新 turn 为准。

| 最新 turn 情况 | 页面状态 | 行为 |
| --- | --- | --- |
| `inProgress` 或排队中 | 正在执行 | 静默 |
| `completed` | 正常完成/空闲 | 静默 |
| 明确等待用户输入或批准 | 等待用户 | 静默 |
| 用户手动停止 | 手动停止 | 静默 |
| `interrupted/failed`，但没有可靠错误原因 | 原因未知 | 静默并提示人工查看 |
| `interrupted/failed`，错误不在恢复规则中 | 其他异常 | 静默并提示人工查看 |
| `interrupted/failed`，错误命中恢复规则 | 可恢复中断 | 进入安全门 |
| thread 不存在 | 会话不存在 | 静默 |

`notLoaded` 不等于异常，也不触发续跑。适配器应继续读取指定 thread 的最新 turn；读取失败时按未知状态处理。

## 8. API 渠道探测规则

探测必须调用真实模型能力，而不是只检测 TCP、主页或 `/models`：

- 使用非流式最小 Chat Completions 请求。
- 生成内容限制为最小值，降低耗时和 token 消耗。
- 只有 2xx 且响应符合所配置兼容协议才视为 `healthy`。
- 401/403 分类为认证异常。
- 429 分类为限流。
- 502/503/504 分类为上游异常。
- DNS、TLS、超时和连接重置分类为网络异常。
- 其他 HTTP 状态或非法 JSON/协议结构分类为其他异常。

所有非 `healthy` 结果都禁止续跑。“添加监控渠道”页提供“立即测试”按钮，并显示分类、HTTP 状态、耗时和检查时间。

## 9. 防重复、锁和重试

系统使用三层保护：

1. incident 指纹保证相同异常 turn 被识别为同一事件。
2. SQLite 对 `fingerprint` 使用唯一约束，并以事务更新发送状态。
3. 每个会话使用互斥锁，避免定时检查和手动立即检查同时发送。

续跑动作每个 incident 最多执行 3 次发送尝试，包含首次发送。失败后使用退避，不把续跑动作失败改写成 API 渠道故障。每次重试仍必须满足渠道健康前提；健康结果超过 60 秒时重新探测。

一旦某次发送确认成功，该 incident 永不再次发送。若发送结果不确定，优先标记为需要人工确认，不进行盲目重复发送。

## 10. HTTP API 边界

建议使用以下路由：

- `GET /api/watchdog/status`
- `GET|PUT /api/watchdog/settings`
- `GET|POST /api/watchdog/channels`
- `GET|PUT|DELETE /api/watchdog/channels/{id}`
- `POST /api/watchdog/channels/{id}/probe`
- `GET|POST /api/watchdog/sessions`
- `GET|PUT|DELETE /api/watchdog/sessions/{id}`
- `POST /api/watchdog/sessions/{id}/check`
- `GET /api/watchdog/local-codex-sessions`
- `GET /api/watchdog/runs`

`local-codex-sessions` 只在用户打开“从本地选择”时调用，返回有限数量的本地会话，不创建监控项。

手动“立即检查”调用与定时检查相同的 DecisionEngine 和安全门。如果会话满足全部自动续跑条件，该操作可能发送续跑提示词；界面必须在按钮说明中明确这一点。手动检查不能绕过渠道健康、恢复规则、incident、锁或尝试次数限制。

删除 API 渠道时，如果仍有会话绑定该渠道，返回冲突并要求先重新绑定或停用会话。

## 11. 页面设计

### 11.1 监控会话

顶部显示已启用、正在执行、正常空闲和需要关注数量。列表字段包括：

- 会话名称和 thread ID
- 绑定渠道
- 实际检查周期
- 当前状态
- 上次和下次检查时间
- 启停、立即检查、编辑和删除操作

添加/编辑字段包括名称、thread ID、绑定渠道、检查周期覆盖值、续跑提示词和启用状态。

### 11.2 执行记录

支持按会话、渠道、决策和时间过滤。每条记录明确显示：

- 静默或续跑
- 渠道分类和 HTTP 状态
- 会话/turn 状态
- 静默或续跑原因
- 续跑尝试次数
- 检查耗时和时间

### 11.3 添加监控渠道

支持渠道增删改、启停、掩码显示 API Key 和立即测试。编辑时不回传明文 Key；留空表示保持原密钥，输入新值表示替换。

## 12. 安全与隐私

1. API Key 不写入 `projects.json`、普通日志、HTTP 响应或浏览器持久化存储。
2. watchdog HTTP 服务继续只监听 `127.0.0.1`。
3. 监控渠道接口不返回密钥明文。
4. 日志对 URL 查询参数、Authorization、错误正文和提示词进行清理。
5. 用户填写的 Base URL 只允许 HTTP/HTTPS，拒绝带用户名或密码的 URL。
6. 续跑提示词作为会话配置保存，但执行记录只保存其哈希或版本标识。
7. CodexAdapter 对 App Server 版本和能力做启动检查；不兼容时禁止发送。
8. 自动续跑不会覆盖会话原有审批策略，也不会自动批准命令、文件修改、网络升级或用户输入请求。App Server 向 watchdog 客户端发起此类请求时，适配器安全拒绝并把会话标记为需要人工关注。

## 13. 记录保留与维护

- 默认保留 90 天执行记录。
- 默认最多保留 10000 条，超过上限优先删除最旧记录。
- 清理在低优先级维护周期执行，不阻塞正常检查。
- incident 不随普通记录过期。只有已经观察到该会话进入更新的 turn，且相关执行记录也已过期时，才允许删除旧 incident；只要会话仍停留在原异常 turn，其指纹必须继续保留。

## 14. 错误处理

- 单个渠道或会话失败不终止调度线程。
- SQLite 写入失败时不发送续跑，避免无法建立防重复记录。
- App Server 不可用时所有会话显示“Codex 连接不可用”，不调用 CLI 盲目续跑。
- API 渠道不可用时保持静默，只记录，不产生浏览器通知或弹窗。
- 续跑动作三次尝试均失败时标记“需要关注”，后续周期不自动无限重试。
- 控制台重启后从 SQLite 恢复 next check、incident 和发送状态。
- 控制台启动时发现遗留 `sending` incident，说明上次发送结果无法确认；必须转为 `manual_attention`，不得自动重发。

## 15. 测试策略

### 15.1 单元测试

- DecisionEngine 状态矩阵。
- 错误分类和 recovery rule 匹配。
- 检查周期和 `next_check_at` 计算。
- incident 指纹和状态转换。
- 日志清理和 URL 校验。

### 15.2 存储和并发测试

- SQLite schema 创建和版本迁移。
- 渠道、会话和设置 CRUD。
- 删除仍被引用渠道的冲突。
- 唯一约束、事务和单会话锁。
- 定时检查与手动检查并发时只发送一次。
- 记录保留和上限清理。

### 15.3 API 探测测试

使用本地模拟 OpenAI 兼容服务覆盖：2xx 合法响应、401、403、429、502、503、504、其他状态、非法 JSON、超时和连接重置。

### 15.4 CodexAdapter 测试

- 先使用模拟 App Server 协议测试状态映射和发送。
- 再创建一个专用测试会话，人工确认读取和续跑。
- 不使用现有 woxsheet、发票识别等真实工作会话做首次发送测试。
- 协议验证失败时停止在只读/禁用状态，不继续实现自动发送。

### 15.5 回归测试

现有项目 URL 校验、外部网页探测、项目状态、排序、启动和关闭行为必须保持通过。新增 watchdog 路由不得改变 `/api/projects` 的响应和原页面轮询行为。

## 16. 分阶段交付

### 阶段 0：Codex App Server 集成验证

验证当前安装版本能否由本地 Python 适配器稳定完成：连接、读取指定 thread、读取最新 turn、向专用测试 thread 发送提示词。

### 阶段 1：只读监控

完成数据库、API 渠道配置、会话 CRUD、调度器、状态读取和执行记录。自动续跑保持关闭，用真实使用观察状态分类。

### 阶段 2：受控续跑

启用 recovery rules、incident、防重复和最多三次发送尝试。先对专用测试会话启用，再由用户逐个启用真实会话。

### 阶段 3：完善与开源准备

补充配置说明、迁移说明、SecretStore 扩展点、App Server 兼容性说明和异常规则维护文档。

## 17. 验收标准

1. 控制台启动后调度器自动运行，关闭控制台终端后停止。
2. 原项目控制台除左侧新增入口外，功能和数据保持不变。
3. 用户可以管理多个 API 渠道并立即测试。
4. 用户可以添加、编辑、删除、启停本地会话，并设置独立周期和提示词。
5. 未添加的 Codex 会话从不被后台读取或续跑。
6. API 渠道不可用时不读取会话、不发送提示词，只写执行记录。
7. 正常运行、正常完成、等待用户、手动停止和未知状态不会自动续跑。
8. 只有明确可恢复 API 异常中断会进入续跑流程。
9. 同一异常 incident 成功续跑后不会重复发送。
10. 续跑失败最多进行三次发送尝试，失败后进入需要关注状态。
11. API Key 不以明文出现在数据库、日志、接口响应或浏览器存储中。
12. 执行记录能够解释每次检查为什么静默或为什么续跑。
13. 自动续跑在专用测试会话验证前保持全局关闭，开关状态在会话监控页可见。

## 18. 明确不在首版范围内

- 自动把全部 Codex 会话加入监控。
- ChatGPT 云任务和远程主机会话。
- 根据自然语言目标调用第二个模型判断完成度。
- 自动切换或修改 Codex 会话实际使用的 API 渠道。
- 无可靠错误证据时猜测会话异常。
- 解析 Codex 私有会话文件后盲目调用 CLI 续跑。
- 控制台关闭后继续运行的 Windows Service 或计划任务。

## 19. 后续扩展

1. 目标会话返回结构化完成标记，控制台据此自动停用监控。
2. 续跑提示词模板变量，例如会话名、异常码、最后检查时间。
3. 新的 recovery rule 和错误分类管理界面。
4. 其他 Codex host 或其他任务客户端适配器。
5. 可选通知策略，但 API 不可用仍默认静默。
