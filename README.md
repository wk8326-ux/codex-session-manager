# 本地项目控制台

本地项目控制台是一个面向 Windows 开发环境的轻量工作台，用于集中启动本地项目、打开远程网页入口，并监控指定的本地 Codex 会话。

项目包含三个彼此隔离的工作区：

- **项目控制台**：登记项目目录、启动命令、停止命令、端口和访问地址，一键启动、关闭或打开项目。
- **会话监控**：检测 OpenAI 兼容 API 渠道和用户指定的 Codex 会话，在明确满足恢复条件时安全续跑。
- **远程会话**：从本机 Codex 选择需要同步的会话，通过一次性二维码配对手机，接收状态更新、向原会话发送消息并处理该链路产生的授权请求。

## 快速开始

要求：

- Windows 10 或 Windows 11
- Python 3.10 或更高版本
- 使用会话监控时，需要已安装并登录 Codex CLI / Codex Desktop
- 使用自由区域截图时，需要开源截图组件 Flameshot 14.0.0

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
- 远程会话管理：<http://127.0.0.1:8765/remote>

## 自由区域截图

本机远程会话页面使用开源工具 [Flameshot](https://github.com/flameshot-org/flameshot)
完成 Windows 桌面自由框选，不调用浏览器的“选择标签页或窗口”截图弹窗。

首次使用可双击 install-screenshot-tool.bat。后台常驻安装脚本
install-system-startup.bat 也会自动检查并安装 Flameshot。安装脚本使用 Windows
Package Manager 固定安装 Flameshot.Flameshot 14.0.0，由 winget 校验官方安装包哈希。

GitHub 直连不可用时，可传入本机 HTTP 代理：

    powershell -ExecutionPolicy Bypass -File scripts/install-screenshot-tool.ps1 -Proxy http://127.0.0.1:10808

控制台依次从 LPC_FLAMESHOT_PATH、系统 PATH、Program Files 和当前用户安装目录查找
flameshot.exe。点击“区域截图”或使用页面配置的截图快捷键后，框选区域即可直接进入附件预览。
Flameshot 作为独立程序按 GPL-3.0 授权，详见 [第三方声明](THIRD_PARTY_NOTICES.md)。

项目控制台会先在 `127.0.0.1:8765` 独立响应，再延迟启动隔离的辅助进程。辅助进程在 `127.0.0.1:8767` 承载会话监控和本机远程管理 API，并在 `0.0.0.0:8766` 启动经过设备认证的远程服务。8767 只允许本机访问；8766 不暴露项目启动命令、API 渠道密钥或监控管理接口。

直接运行 `python app.py` 时，关闭终端会同时停止核心服务和辅助进程。双击 `start-console.bat` 使用后台计划任务，不需要保留终端窗口。

## Windows 后台常驻

需要让项目工作台、会话监控、远程 PWA 和 FRP 隧道在登录后始终可用时，双击：

```text
install-system-startup.bat
```

脚本会为当前 Windows 用户注册 `Local Project Console` 计划任务：

- 用户登录后隐藏启动，不需要保留终端窗口。
- 计划任务直接启动 Python，不经过常驻 PowerShell 包装循环；项目控制台先响应，辅助进程随后在后台启动。
- 辅助进程异常退出后由核心进程在 3 秒后重启；核心进程异常退出时由计划任务恢复。
- 允许电池供电时运行，错过登录触发后会尽快补启动。
- 使用当前用户身份，因此能够读取同一用户的 Codex 配置、会话和 DPAPI 密钥。
- 核心日志写入 `.runtime/system-startup/console-service.log`，辅助进程日志写入同目录的 `auxiliary-runtime.log`，均不会提交到 Git。

计划任务不使用 `SYSTEM` 身份。远程会话依赖当前登录用户的 Codex Desktop/CLI 数据，改成 `SYSTEM` 会导致识别不到原有会话。电脑尚未登录时 Codex Desktop 本身也不可用，因此采用“登录即启动”比 Windows Service 更符合实际依赖关系。

管理命令：

```powershell
# 查看任务与健康状态
powershell -ExecutionPolicy Bypass -File scripts/manage-system-startup.ps1 -Action Status

# 重启后台控制台
powershell -ExecutionPolicy Bypass -File scripts/manage-system-startup.ps1 -Action Restart

# 停止并移除计划任务
uninstall-system-startup.bat
```

重新拉取或移动仓库后，需要在新路径重新运行安装脚本。计划任务不会写死任何仓库作者的设备路径，而是在安装时记录当前副本的绝对路径。

## 手机远程访问

1. 启动控制台，打开 <http://127.0.0.1:8765/remote>。
2. 确认“公网 HTTPS 地址”。同一局域网也可以临时使用页面自动识别的 `http://<电脑局域网 IP>:8766`。
3. 点击“生成配对二维码”，用手机扫码。
4. 在手机上填写设备名称并确认配对。
5. 核对手机和电脑显示的六位验证码，一致后进入工作台。

二维码有效期为 3 分钟且只能使用一次。每台手机会获得独立设备令牌，控制台只保存令牌的 SHA-256 哈希；在“配对设备”中撤销后，该设备会立即失去访问权限。

先在电脑端的“远程会话 > 会话”中点击“添加同步会话”，读取本机 Codex 会话并把需要远程访问的会话加入同步目录。远程同步目录与会话监控目录相互独立：加入同步不会启用 API 监控或自动续跑，移除同步也不会删除原 Codex 会话或监控配置。手机端只显示用户明确加入远程同步目录的会话。

发送消息时：

- 目标 turn 正在运行：调用 `turn/steer`，把消息加入同一个运行中 turn。
- 目标会话空闲：调用 `thread/resume` 后再调用 `turn/start`。
- App Server 无法确认发送结果：不盲目重试，避免同一消息被重复提交。

消息会写入相同的 Codex `threadId`。同步页面采用 App Server 事件长轮询与主动读取组合：事件到达时立即读取，任务运行中约每 1.2 秒读取一次，空闲时约每 5 秒读取一次，页面进入后台后自动降频。界面会持续显示当前活动、工具状态、本轮活动数、文件变更数和最近同步时间；新内容默认跟随到底部，向上滚动后暂停跟随。由于控制台和 Codex Desktop GUI 使用的是不同 App Server 进程，桌面 GUI 有时需要切换会话、重新打开或刷新后才显示手机发送的新内容。

如果由 PWA 启动或续跑的任务请求命令执行、文件修改或权限扩展，手机端会显示待审批区域，可选择“允许一次”“本会话允许”或“拒绝”。两分钟无人处理、移除同步会话或关闭控制台时会自动拒绝。该能力不能接管 Codex Desktop 独立连接中已经弹出的审批请求。

局域网 HTTP 可以直接在手机浏览器中使用。PWA 安装和 Service Worker 在非 `localhost` 地址上要求 HTTPS。跨网络访问推荐使用自建 VPS、FRP 和 Nginx：电脑主动建立加密隧道，手机只访问稳定的 HTTPS 域名。不要直接把本机 `8766` 映射到公网，更不能暴露管理端 `8765`。

当 `.runtime/frp/frpc-lpc.exe` 与 `.runtime/frp/frpc.toml` 同时存在时，控制台会自动管理 FRP 客户端。隧道随控制台启动和关闭，状态显示在“远程会话 > 配对设备”。完整部署步骤见 [使用自建 VPS 和 FRP 远程访问](docs/frp-remote-access.md)。

远程监听地址和端口可以在启动前通过环境变量调整：

```powershell
$env:LPC_REMOTE_HOST = '0.0.0.0'
$env:LPC_REMOTE_PORT = '8766'
python app.py
```

详细说明见 [手机远程访问与会话同步](docs/remote-access.md)。

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

会话监控页面包含四个视图：

- **监控会话**：用户明确添加的本地 Codex 会话、状态、检查周期、绑定渠道和手动检查入口。
- **执行记录**：记录每次检查为什么保持静默、触发续跑、执行失败或需要人工处理。
- **添加监控渠道**：配置用于可用性探测的 OpenAI 兼容 API 渠道。
- **错误类型**：查看内置的 429、502、503、504、超时和连接重置规则，并维护用户自定义的错误文本。

自定义错误类型采用“文本包含”匹配。粘贴报错中稳定出现的文字即可，匹配时忽略大小写和多余空白。自定义规则可以编辑、停用或删除；系统内置规则保持只读。命中自定义文本只代表错误进入恢复候选，仍需同时满足渠道健康、会话异常、续跑开关和事件去重等安全条件。

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

- **本机 App Server · 推荐**：控制台直接使用常驻 App Server 的 `thread/resume` 和 `turn/start` 恢复任务，并与远程 PWA 共用事件流；无需额外桥接会话或定时任务。Codex Desktop 可能需要重新打开会话才能看到外部实例写入的新内容。
- **Codex Desktop · 兼容桥接**：仅在必须让恢复过程立即显示在 Codex Desktop 时使用。控制台创建持久化 bridge job，由暂停状态之外的 Desktop Runner 领取并向目标会话发送提示词。

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
- [手机远程访问与会话同步](docs/remote-access.md)
- [使用自建 VPS 和 FRP 远程访问](docs/frp-remote-access.md)

常用只读诊断接口：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/watchdog/status
Invoke-RestMethod http://127.0.0.1:8765/api/watchdog/settings
Invoke-RestMethod 'http://127.0.0.1:8765/api/watchdog/runs?limit=20'
```

所有会话监控接口都位于 `/api/watchdog/` 命名空间，不会读写项目控制台的 `/api/projects` 数据。
