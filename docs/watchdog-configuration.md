# 会话监控配置与运维

会话监控是 Local Project Console 中独立于项目启动面板的本地功能。它定期检查用户明确加入目录的 Codex 会话和 OpenAI 兼容 API 渠道，并记录每次检查为何静默、续跑或需要人工处理。

默认情况下，监控会运行，但自动续跑保持关闭；每个会话的“无人值守执行”也默认关闭。建议先完成只读检查和专用测试会话验证，再开启全局续跑开关，并只为确实需要自动恢复的会话单独授权。

## 运行前提

- Windows，Python 可通过 `py` 或 `python` 启动。
- Codex CLI 已安装，并且 `codex` 或 `codex.cmd` 可在 `PATH` 中找到。
- 当前 Codex CLI 支持 `app-server --listen stdio://`、`thread/list`、`thread/read`、`thread/resume` 和 `turn/start` 协议。
- 兼容渠道允许向其聊天补全端点发送一次最小探测请求。

项目没有写死某个 Codex CLI 版本。升级 Codex CLI 后，先记录版本并运行只读协议探测：

```powershell
codex --version
python scripts/probe_codex_app_server.py --list --limit 5
```

第二条命令只读取本地会话，不发送消息。控制台启动时会验证 App Server 初始化握手；实际监控只读取用户明确添加的目标会话。若初始化、`thread/list` 或 `thread/read` 报协议错误，请保持自动续跑关闭，先解决 Codex CLI 兼容性问题。`thread/resume` 和 `turn/start` 无法在不修改会话的情况下探测，因此必须使用后文的专用测试会话闸门验证；任何不确定发送结果都会转为人工确认，不会盲目重试。

## 启动和关闭

在仓库根目录运行：

```powershell
python app.py
```

Windows 也可以双击 `start-console.bat`。随后访问：

- 项目控制台：`http://127.0.0.1:8765/`
- 会话监控：`http://127.0.0.1:8765/watchdog`

调度器随控制台进程启动。手工运行时，正常关闭终端或中断 `app.py` 后，HTTP 服务、调度线程和由控制台创建的 Codex App Server 子进程会一并关闭。

需要后台常驻时，可以双击仓库根目录的 `install-system-startup.bat`。它会为当前 Windows 用户注册登录启动、异常重启的计划任务；使用方法和卸载命令见 README 的“Windows 后台常驻”。

## 添加监控渠道

“添加监控渠道”是配置需要定期探测的 OpenAI 兼容 API，不是添加 Codex 会话。一个渠道可以绑定多个监控会话，也可以配置多个渠道供不同会话选择。

必填字段：

- `渠道名称`：便于识别的本地名称。
- `Base URL`：有效的 HTTP 或 HTTPS 地址，不允许在 URL 中嵌入用户名或密码。
- `测试模型`：渠道支持的模型标识。
- `API Key`：用于探测请求的 Bearer 密钥。
- `超时`：1 到 120 秒，默认 15 秒。

可选字段：

- `高级探测 URL`：填写后直接向该地址探测；留空时使用 `Base URL`，若其末尾不是 `/chat/completions`，则自动追加该路径。
- `渠道状态`：停用后不会用于会话检查。

探测会发送一个非流式、`max_tokens: 1` 的最小聊天补全请求。结果分类如下：

| 分类 | 含义 |
| --- | --- |
| `healthy` | HTTP 2xx，且响应包含 completion ID 或 `choices` |
| `rate_limited` | HTTP 429 |
| `upstream_error` | HTTP 502、503 或 504 |
| `auth_error` | HTTP 401 或 403 |
| `network_error` | DNS、连接或超时错误 |
| `protocol_error` | HTTP 成功，但响应不是兼容 JSON |
| `other_http_error` | 其他非 2xx HTTP 状态 |

编辑已有渠道时，API Key 输入框留空表示保留原密钥。页面和 API 只返回“已保存”标记，不回显密钥。

## 添加监控会话

只有用户保存且启用的会话会进入调度目录。工具不会无条件扫描或监控所有 Codex 会话。

在“添加会话”对话框中，可以：

1. 点击“读取本地会话”。这一步才会调用本机 Codex `thread/list`。
2. 从结果中选择会话，或手动粘贴 UUID 格式的会话 ID。
3. 填写名称并绑定一个已存在的监控渠道。
4. 选择使用全局周期，或设置该会话自己的检查周期。
5. 保留默认续跑提示词，或为该会话自定义提示词。
6. 根据风险选择是否开启“允许该会话无人值守执行”。默认关闭。
7. 保存后，该会话才会加入监控目录。

“无人值守执行”是会话级安全开关。开启后，仅当全局自动续跑也开启时，监控器才会自动批准该会话收到的命令执行和文件修改审批。若 App Server 的 `availableDecisions` 支持 `acceptForSession`，监控器优先使用本会话批准；否则降级为仅批准当前请求的 `accept`。这意味着恢复后的 Codex turn 可以继续运行命令和修改文件，权限范围与该 Codex 会话本身的沙箱和审批策略有关。`item/permissions/requestApproval` 形式的额外权限扩展不会自动批准，模型提出的用户问题也不会自动作答；这些情况会记录为“需要关注”。

也可以使用只读诊断脚本查找会话 ID：

```powershell
python scripts/probe_codex_app_server.py --list --limit 20
python scripts/probe_codex_app_server.py --thread-id <thread-uuid>
```

## 检查周期

- 全局默认周期：15 分钟。
- 单会话周期：留空时继承全局默认值。
- 最小周期：5 分钟，低于该值会被拒绝。
- 页面状态和会话摘要每 15 秒轻量刷新一次；这不是渠道探测周期。
- 调度器每秒检查是否有“到期”的已启用会话，但只对到期会话执行实际探测。

全局设置可通过 API 查看或修改：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/watchdog/settings

$body = @{
  defaultIntervalMinutes = 20
} | ConvertTo-Json
Invoke-RestMethod `
  -Method Put `
  -Uri http://127.0.0.1:8765/api/watchdog/settings `
  -ContentType 'application/json' `
  -Body $body
```

## 自动续跑规则

默认提示词为：

```text
继续当前开发任务
```

每个会话都可以保存独立提示词，最长 4000 个字符。

### 恢复通道

页面顶部可以选择两种恢复通道：

- `Codex Desktop · 实时同步`：推荐。控制台只创建持久化 bridge job，由 Codex Desktop heartbeat runner 领取并通过桌面原生任务接口发送，因此 commentary、工具调用、审批和最终状态会实时显示在桌面端。runner 登记新 turn ID 后立即退出，控制台通过后续检查或 App Server 事件收敛最终状态。
- `独立 App Server · 兼容`：保留原有 `thread/resume` + `turn/start` 路径，适用于未配置桌面 runner 的环境。该路径能恢复任务，但运行中状态不会实时同步到 Codex Desktop。

新数据库默认使用兼容模式，避免没有 runner 时任务停留在待领取状态。桌面桥接的设置步骤、heartbeat 提示词和回调协议见 [Codex Desktop 实时桥接](codex-desktop-bridge.md)。

Desktop runner 是当前控制台的全局单例。添加监控会话不会额外创建定时任务，所有已启用会话共用同一个 runner。删除会话会取消该会话尚未执行的 bridge job，但不会删除全局 runner；删除或暂停 runner 则会让所有会话的 Desktop 实时恢复停止工作。

一次检查只有同时满足以下条件才可能发送续跑提示词：

1. 会话已由用户加入监控目录且处于启用状态。
2. 绑定渠道启用，并且本次探测结果为 `healthy`。
3. 本机 Codex 可连接，且能可靠读取目标会话的最新 turn。
4. 最新 turn 为 `failed` 或 `interrupted`，并具有可识别的错误信息。
5. 错误命中一条已启用的恢复规则。
6. 全局 `resumeActionsEnabled` 已开启。
7. 若恢复后的任务需要执行命令或修改文件，该会话已显式开启“无人值守执行”。

以下情况始终静默，不会自动续跑：

- 渠道不可用，包括 429、502、503、504、鉴权、网络或协议错误。
- 会话正在运行、已经完成、没有 turn，或状态未知。
- 会话需要额外权限扩展或模型要求用户输入。
- 会话没有开启“无人值守执行”，而恢复后的 turn 请求命令或文件修改审批。
- `interrupted` 但没有可靠错误信息。
- 错误没有命中恢复规则。
- Codex 数据不可用或监控本身出错。

### 关于“运行中”与 `notLoaded`

监控器启动的是独立的 Codex App Server 进程。`thread/read` 能可靠读取已经写入本地记录的最新 turn；如果该 turn 为 `inProgress`，控制台会显示“会话运行中”并静默。但是 App Server 的 `active` / `notLoaded` 是该 App Server 进程自己的内存状态，不能代表桌面 Codex 应用中是否仍打开或正在显示同一会话。

因此，当记录显示 `interrupted` 但没有 429、502、503、504 等可恢复 API 错误时，控制台会明确记录“最新 turn 已中断，但没有可确认的可恢复 API 错误”，并保持静默。它不会把桌面端视觉上的活动状态推断为可续跑，避免对真实工作会话发送重复提示词。
- 全局续跑开关关闭；此时仍会记录“仅观察到续跑候选”。

内置恢复规则可识别最新 Codex turn 中的 HTTP 429、502、503、504，以及若干超时、限流和上游错误模式。除了 Codex 的结构化 HTTP 状态字段，控制台也会严格识别已知上游错误文本中的 `unexpected status 503`、`last status: 429`、`upstream_status: HTTP 502` 等格式；普通会话文本不参与匹配。恢复规则严格匹配；不要把未知异常宽泛地标记为可恢复。

### 维护恢复规则

“错误类型”页签列出全部恢复规则。HTTP 429、502、503、504、`timeout` 和 `connection_reset` 由系统以稳定 ID 写入，重复启动不会重复创建，且不能在页面或 API 中修改、停用或删除。

对于没有明确状态码、但报错正文稳定的异常，可以点击“添加文本错误”创建自定义规则：

1. 使用便于识别的名称，例如“模型容量已满”。
2. 粘贴报错中稳定出现的文本，例如 `Selected model is at capacity. Please try a different model.`。
3. 保存后按“文本包含”匹配，忽略大小写以及换行、连续空格等空白差异。
4. 错误文本长度为 8 到 500 个字符。只保留能够唯一识别该异常的稳定片段，不要填写 `error`、`failed` 等宽泛词语。

自定义规则可以编辑、启用、停用或删除。规则只读取最新失败 turn 的错误信息，不会匹配普通用户消息或助手正文。命中规则也不会单独触发发送：渠道健康、会话状态、全局续跑开关、事件去重和审批条件仍必须全部通过。

### 开启全局续跑

首次使用时保持只读模式。先创建标题严格为 `watchdog-integration-test` 的一次性 Codex 会话，然后执行：

```powershell
python scripts/probe_codex_app_server.py --thread-id <test-thread-uuid>
python scripts/probe_codex_app_server.py `
  --thread-id <test-thread-uuid> `
  --allow-send `
  --prompt "watchdog integration test"
```

`--allow-send` 是显式写操作开关，而且脚本只允许向标题恰好为 `watchdog-integration-test` 的会话发送。只有输出同时包含新的 `confirmedTurnId` 和 `"status": "completed"` 才表示发送闸门通过；再确认该测试会话只收到一条消息，然后在页面顶部开启自动续跑。

关闭全局续跑立即生效，同时会让所有会话级无人值守审批失效，但不会停止只读监控。若需要完全暂停定时检查，可通过设置 API 将 `schedulerEnabled` 设为 `false`。

### 续跑开始与最终结果

桌面桥接任务创建后先记录为 `resume_queued`。runner 回传新的 turn ID 后更新为 `resume_started`，表示续跑已经启动，并不表示任务已经完成。桌面 runner 会等待目标任务并回传终态。兼容直连模式则继续订阅独立 App Server 的事件：

- `turn/completed` 且状态为 `completed`：更新为 `resume_completed`。
- 最终状态为 `failed`：更新为 `resume_failed`；若新 turn 本身又是明确的可恢复 API 异常，后续检查会把它视为新的 incident。
- 最终状态为 `interrupted`：更新为 `resume_interrupted`。
- 命令或文件审批未获会话级授权、请求额外权限或要求用户输入：更新为 `resume_manual_attention`。

事件可能比 `turn/start` 的数据库记录更早到达，监控器会短暂缓冲并在 turn 登记后重放。若控制台重启后无法观察到终态，而会话已经出现更新的 turn，旧恢复事件会保守转为“需要关注”。

## 重试和人工关注

每个可恢复事件由“会话、turn、错误签名”形成稳定指纹，已成功处理的同一事件不会重复发送。

- 明确发送失败最多尝试 3 次。
- 第一次失败后约 30 秒重试，第二次失败后约 120 秒重试。
- 三次仍明确失败后转为“需要关注”。
- `turn/start` 发送结果不确定时不会盲目重试，而是立即转为人工确认。
- `turn/start` 被接受不等于任务恢复完成；只有后续 `turn/completed` 的最终状态为 `completed` 才记为续跑完成。
- 控制台异常退出时，兼容直连模式中仍处于发送中的事件会在下次启动时转为人工确认；已持久化的待领取桌面 bridge job 会保留并在控制台恢复后继续等待 runner。

“立即检查”走同一套渠道、会话、恢复规则、去重和重试逻辑，不会绕过安全限制。

## 密钥和本地数据

渠道 API Key 使用 Windows DPAPI 的当前用户作用域加密，密文保存在项目目录的 `watchdog.db` 中。

这意味着：

- 只有同一 Windows 用户上下文通常能够解密。
- 复制数据库到其他设备或其他 Windows 用户后，原 API Key 通常不可恢复；迁移时应重新录入渠道密钥。
- 数据库备份可以保留会话、渠道元数据和执行记录，但不能视为可移植的密钥备份。
- 不要提交 `watchdog.db`、`watchdog.db-shm` 或 `watchdog.db-wal`。
- 页面和 API 不回显完整 API Key；执行记录不保存或展示完整提示词和渠道响应正文。

仓库的 `.gitignore` 已排除上述数据库文件和本地 `.superpowers/` 目录。

### SecretStore 扩展点

业务代码只依赖 `SecretStore.protect(value) -> bytes` 与 `SecretStore.unprotect(value) -> str` 接口，当前 `DpapiSecretStore` 是 Windows 当前用户作用域实现。若移植到其他平台，应新增操作系统密钥环或等价安全存储实现，并在运行时构造处注入；不得把明文密钥、设备名、用户目录或固定密钥写入业务逻辑，也不得以“兼容”为由退化为明文数据库字段。迁移实现必须继续通过密钥不回显、不入日志和加密往返测试。

## 执行记录与保留

执行记录包括时间、会话、渠道状态、HTTP 状态、会话状态、处理决定、尝试次数、耗时和脱敏原因。可以按 `sessionId`、`channelId`、`decision`、`from`、`to` 筛选，单次 API 查询上限为 500 条。

默认保留策略：

- 最长 90 天。
- 最多 10,000 条。
- 调度器运行期间，每个本地自然日执行一次清理。

可通过设置 API 修改保留天数和条数：

```powershell
$body = @{
  recordRetentionDays = 30
  recordLimit = 5000
} | ConvertTo-Json
Invoke-RestMethod `
  -Method Put `
  -Uri http://127.0.0.1:8765/api/watchdog/settings `
  -ContentType 'application/json' `
  -Body $body
```

## API 速查

所有会话监控接口都位于独立的 `/api/watchdog/` 命名空间，不读取或修改 `/api/projects`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/watchdog/status` | 调度器、Codex、续跑开关和下次检查状态 |
| `GET`, `PUT` | `/api/watchdog/settings` | 全局周期、记录保留和开关 |
| `GET`, `POST` | `/api/watchdog/channels` | 列出或添加渠道 |
| `GET`, `PUT`, `DELETE` | `/api/watchdog/channels/{id}` | 读取、修改或删除渠道 |
| `POST` | `/api/watchdog/channels/{id}/probe` | 立即测试渠道 |
| `GET`, `POST` | `/api/watchdog/recovery-rules` | 列出规则或添加自定义文本错误 |
| `GET`, `PUT`, `DELETE` | `/api/watchdog/recovery-rules/{id}` | 读取、修改或删除自定义规则；内置规则只读 |
| `GET`, `POST` | `/api/watchdog/sessions` | 列出或添加监控会话 |
| `GET`, `PUT`, `DELETE` | `/api/watchdog/sessions/{id}` | 读取、修改或删除会话 |
| `POST` | `/api/watchdog/sessions/{id}/check` | 立即按完整规则检查会话 |
| `GET` | `/api/watchdog/local-codex-sessions?limit=20` | 用户触发后列出本机会话 |
| `GET` | `/api/watchdog/runs` | 查询执行记录 |
| `GET` | `/api/watchdog/bridge/status` | 查询桌面桥接队列状态 |
| `POST` | `/api/watchdog/bridge/jobs/claim` | 桌面 runner 原子领取一个任务 |
| `POST` | `/api/watchdog/bridge/jobs/{id}/started` | 回传桌面 turn ID |
| `POST` | `/api/watchdog/bridge/jobs/{id}/finish` | 回传明确发送失败、人工关注，或兼容旧 runner 的最终状态 |

添加渠道的 API 示例使用环境变量承载密钥，避免把密钥写进脚本或 shell 历史：

```powershell
$body = @{
  name = 'Primary compatible channel'
  baseUrl = 'https://api.example.com/v1'
  model = 'compatible-model'
  apiKey = $env:WATCHDOG_API_KEY
  timeoutSeconds = 15
  enabled = $true
} | ConvertTo-Json
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/api/watchdog/channels `
  -ContentType 'application/json' `
  -Body $body
```

## 诊断

### 控制台或调度器状态

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/watchdog/status
```

重点字段：

- `schedulerRunning`：调度线程是否存活。
- `schedulerEnabled`：是否允许执行到期任务。
- `codexConnected`：控制台启动时是否成功连接 Codex App Server。
- `resumeActionsEnabled`：是否允许发送续跑提示词。
- `resumeDispatchMode`：`desktop_bridge` 或 `direct_app_server`。
- `desktopBridge`：待领取、已领取、运行中、已结束数量和最近领取时间。
- `nextCheckAt`：所有启用会话中最早的下次检查时间，使用 UTC。

### 渠道异常

在“添加监控渠道”页点击“立即测试”，或调用：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/api/watchdog/channels/<channel-id>/probe `
  -ContentType 'application/json' `
  -Body '{}'
```

根据 `category`、`httpStatus`、`detail` 和 `durationMs` 排查。401/403 通常是密钥或权限问题；429 是限流；502/503/504 是上游不可用；`protocol_error` 表示渠道返回格式不兼容。

### Codex 不可用

1. 运行 `codex --version`，确认命令可用。
2. 运行只读 `python scripts/probe_codex_app_server.py --list --limit 5`。
3. 若读取特定会话失败，运行 `--thread-id <thread-uuid>` 获取明确协议错误。
4. 保持 `resumeActionsEnabled: false`，不要用真实工作会话测试发送。

### 查看最近执行决定

```powershell
Invoke-RestMethod 'http://127.0.0.1:8765/api/watchdog/runs?limit=20'
```

时间筛选必须使用 UTC `YYYY-MM-DDTHH:MM:SSZ` 格式。例如：

```text
/api/watchdog/runs?decision=resume_action_failed&from=2026-01-01T00:00:00Z&limit=100
```

执行记录只保留脱敏摘要。若需要调查发送结果不确定或“需要关注”，应打开对应 Codex 会话确认最新 turn，而不是直接重复发送。
