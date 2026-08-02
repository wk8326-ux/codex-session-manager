# 本地项目控制台

本地项目控制台是一个面向 Windows 开发环境的轻量工作台，用于集中启动本地项目、打开远程网页入口，并监控指定的本地 Codex 会话。

项目包含两个彼此隔离的工作区：

- **项目控制台**：登记项目目录、启动命令、停止命令、端口和访问地址，一键启动、关闭或打开项目。
- **会话监控**：检测 OpenAI 兼容 API 渠道和用户指定的 Codex 会话，在明确满足恢复条件时安全续跑。

## 快速开始

要求：

- Windows 10 或 Windows 11
- Python 3.10 或更高版本
- 使用会话监控时，需要已安装并登录 Codex CLI / Codex Desktop

双击：

```text
start-console.bat
```

也可以在项目目录运行：

```powershell
python app.py
```

启动后访问：

- 项目控制台：<http://127.0.0.1:8765/>
- 会话监控：<http://127.0.0.1:8765/watchdog>

调度器随控制台进程启动和关闭。关闭控制台终端后，HTTP 服务、监控调度器以及控制台创建的 Codex App Server 子进程会一起停止。

## 项目控制台

### 本地项目

本地项目可以配置：

- 项目名称
- 工作目录
- 启动命令或启动脚本
- 可选的停止命令
- 监听端口
- 本地访问地址

控制台会检查端口是否监听，以及由控制台启动的进程是否仍然存活。启动命令既可以填写项目中的脚本：

```text
start-invoice-tool.bat
```

也可以直接填写命令：

```text
npm run dev -- --host 127.0.0.1 --port 4173 --strictPort
```

命令会在该项目配置的工作目录中执行。

### 外部网页

已经运行在其他设备或服务器上的工具，可以使用“外部网页”模式。只需填写 HTTP 或 HTTPS 地址，控制台会定期检测网页可达性并显示在线或离线状态，不会假装启动或关闭远程服务。

项目配置在首次运行后保存到本机 `projects.json`。该文件已被 Git 忽略，不会把设备路径、远程地址或进程 ID 上传到仓库。可移植示例见 [`projects.example.json`](projects.example.json)，启动日志保存在已忽略的 `logs/<project-id>.log`。

## 会话监控

会话监控页面包含三个视图：

- **监控会话**：用户明确添加的本地 Codex 会话、状态、检查周期、绑定渠道和手动检查入口。
- **执行记录**：记录每次检查为什么保持静默、触发续跑、执行失败或需要人工处理。
- **添加监控渠道**：配置用于可用性探测的 OpenAI 兼容 API 渠道。

工具只监控用户保存并启用的会话，不会无条件扫描所有 Codex 会话。打开“读取本地会话”选择器也不会自动把全部会话加入监控目录。

### 安全默认值

- 会话检查默认开启，但全局自动续跑默认关闭。
- 每个会话的无人值守命令和文件审批默认关闭，必须逐个明确授权。
- 全局检查周期默认 15 分钟，单个会话可以覆盖，最短 5 分钟。
- 默认续跑提示词是“继续当前开发任务”，每个会话都可以单独修改。
- API 渠道不可用、会话运行中、会话已完成、等待用户输入、状态未知或错误未命中恢复规则时保持静默。
- 同一恢复事件会去重；明确发送失败最多尝试 3 次，不确定的发送结果不会盲目重试。
- 执行记录默认保留 90 天、最多 10,000 条，调度器运行期间每天清理一次。

首次启用自动续跑前，请使用一次性测试会话完成发送验证，不要直接使用仍在开发的真实工作会话。

## Codex Desktop 桌面桥接

恢复通道支持两种模式：

- **Codex Desktop · 实时同步**：推荐。控制台创建持久化 bridge job，由 Codex Desktop Runner 领取并向目标会话发送提示词，运行过程会实时显示在桌面端。
- **独立 App Server · 兼容**：使用独立 App Server 的 `thread/resume` 和 `turn/start`，可以恢复任务，但运行中的状态不会实时同步到 Codex Desktop。

桌面桥接的链路如下：

```text
控制台检测 API 和目标会话
        ↓
命中恢复条件后生成 bridge job
        ↓
全局 Runner 每 5 分钟领取一次
        ↓
无任务：静默结束
有任务：向指定目标会话发送续跑提示词
        ↓
Runner 回传新 turn ID 后立即结束
        ↓
控制台在后续检查或事件中收敛最终状态
```

一台设备只需要一个全局 Runner。所有监控会话共用它，添加或删除监控会话不会创建或删除全局定时任务。删除监控会话会取消该会话尚未执行的 bridge job；删除或暂停全局 Runner 则会停止所有会话的 Desktop 实时恢复。

Git 只同步项目代码，不会同步 Codex Desktop 会话、自动化、API 渠道、监控会话或本地数据库。因此，每台新设备需要安装一次自己的桌面桥接。

### 新设备桥接安装提示词

在新设备上拉取项目并启动控制台后，把下面整段提示词发送给该设备上的 Codex。Codex 会创建或复用专用 Runner 会话和全局 heartbeat 自动化，不会写死其他设备的会话 ID。

```text
请为 Local Project Console 安装 Codex Desktop 桌面桥接。

控制台地址：
http://127.0.0.1:8765

请严格执行：

1. 检查以下接口是否可访问：
   GET http://127.0.0.1:8765/api/watchdog/status
   GET http://127.0.0.1:8765/api/watchdog/bridge/status

   如果控制台不可访问，停止操作并提示我先启动控制台。

2. 检查当前设备是否已经存在：
   - 标题为“Local Project Console 桌面桥接 Runner”的专用 Codex 任务
   - 名称为“Local Project Console 桌面桥接”的 heartbeat 自动化

   如果已有配置正确，则复用并更新，不要重复创建。

3. 如果没有专用任务，创建一个本地 projectless Codex 任务，并将标题设置为：
   Local Project Console 桌面桥接 Runner

   该任务只用于桌面桥接，不修改项目文件，不作为业务会话使用。

4. 为该专用任务创建或更新一个 heartbeat 自动化：
   名称：Local Project Console 桌面桥接
   周期：每 5 分钟
   状态：启用
   通知：仅失败时通知

5. heartbeat 使用下面的提示词：

   你是 Local Project Console 的 Codex Desktop 桌面桥接 runner。每次 heartbeat 只处理一个任务。

   控制台地址：http://127.0.0.1:8765
   runnerId：codex-desktop-local

   严格执行：
   - POST /api/watchdog/bridge/jobs/claim，JSON 为：
     {"runnerId":"codex-desktop-local","leaseSeconds":180}
   - 控制台不可访问或返回空任务时，静默结束。
   - 领取任务后，只处理返回的 threadId 和 prompt。
   - 先用 read_thread(threadId, turnLimit=1) 记录目标会话发送前的最新 turn ID。
   - 调用 send_message_to_thread(threadId, prompt)。若被明确拒绝，POST /api/watchdog/bridge/jobs/{id}/finish，outcome 使用 dispatch_failed，然后结束。
   - 发送被接受后，在 60 秒内短暂重试读取目标会话，直到获得与发送前不同的新 turn ID，并 POST /api/watchdog/bridge/jobs/{id}/started。
   - 成功回传 `/started` 后立即结束；不要等待目标任务，不要调用 wait 类工具，也不要为成功发送调用 `/finish`。目标任务的最终状态由控制台后续检查和事件订阅收敛。
   - 如果发送已被接受但 60 秒内无法确认新 turn ID，POST /api/watchdog/bridge/jobs/{id}/finish，outcome 使用 manual_attention，然后结束；绝不重复发送。
   - send_message_to_thread 一旦被接受，绝不对同一个 job 重复发送。
   - 每轮最多处理一个任务，不扫描其他会话，不修改项目文件。

6. PUT /api/watchdog/settings，JSON 为：
   {"resumeDispatchMode":"desktop_bridge"}

   不要自动开启 resumeActionsEnabled，由我在控制台页面中手动开启。

7. 最后向我报告：
   - 专用桥接任务 ID
   - 自动化 ID
   - 自动化状态
   - 桥接周期
   - 控制台连接结果
   - 是否发现重复的旧桥接任务

不要创建测试监控会话，不要向任何业务会话发送测试消息。
```

## 兼容性检查

会话监控当前依赖 Windows DPAPI 加密 API Key，也依赖 Codex CLI 提供 App Server stdio 协议。开启续跑前建议先执行只读检查：

```powershell
codex --version
python scripts/probe_codex_app_server.py --list --limit 5
```

项目不写死 Codex CLI 版本。升级后如果只读探测失败，应保持自动续跑关闭，直到协议兼容性恢复。

## 本地数据与安全

会话监控配置和执行记录保存在 `watchdog.db`。API Key 使用当前 Windows 用户作用域的 DPAPI 加密，不会由 API 返回，也不会以明文写入日志。数据库复制到其他设备或其他 Windows 用户后通常无法解密原密钥，需要重新填写渠道密钥。

仓库已忽略：

- `projects.json`
- `logs/`
- `watchdog.db`
- `watchdog.db-shm`
- `watchdog.db-wal`
- `.superpowers/`

不要提交真实 API Key、本地数据库、个人路径、生产会话 ID 或设备专用的自动化配置。

## 详细文档

- [会话监控配置与运维](docs/watchdog-configuration.md)
- [Codex Desktop 实时桥接](docs/codex-desktop-bridge.md)

常用只读诊断接口：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/watchdog/status
Invoke-RestMethod http://127.0.0.1:8765/api/watchdog/settings
Invoke-RestMethod 'http://127.0.0.1:8765/api/watchdog/runs?limit=20'
```

所有会话监控接口都位于 `/api/watchdog/` 命名空间，不会读写项目控制台的 `/api/projects` 数据。
