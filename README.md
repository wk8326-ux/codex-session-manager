# Codex 会话管理

独立的 Windows 本地工具，用于保障 Codex 会话可用性：

- 监控用户指定的 OpenAI 兼容 API 渠道。
- 监控用户明确添加的 Codex 会话，并在匹配恢复规则时自动续跑。
- 通过 PWA 在手机上查看会话、发送消息、上传截图和处理审批。
- 管理手机配对、FRP/WireGuard 远程入口和运行记录。

本项目不包含本地项目启动器，也不读取 `projects.json`。它和 Local Project Console 是两个独立仓库、两个进程、两套数据职责。

## 启动

要求 Windows 10/11、Python 3.10+，并已安装且登录 Codex CLI 或 Codex Desktop。

```powershell
python app.py
```

本机管理端：<http://127.0.0.1:8767/>

手机 PWA：`http://<电脑局域网 IP>:8766/`，跨网络访问建议按配置向导部署专用隧道。

双击 `start-session-manager.bat` 也可以启动；关闭对应终端或在 Local Project Console 中点击“关闭”即可停止。

从拆分前的 Local Project Console 迁移时，先关闭旧的会话管理进程，再运行：

```text
migrate-legacy-data.bat "D:\path\to\localhost-project-console"
```

迁移只复制 `watchdog.db` 和稳定的 FRP 配置，不复制 `projects.json`、PID、锁或日志。源目录不会被删除；目标已有数据库时默认拒绝覆盖。

## 添加到项目控制台

先启动 Local Project Console，再双击：

```text
register-with-console.bat
```

脚本只通过项目控制台公开的 `/api/projects` 接口登记一个普通项目：

```text
名称：Codex 会话管理
工作目录：当前仓库目录
启动命令：start-session-manager.bat
关闭命令：stop-session-manager.bat
监控端口：8767
访问地址：http://127.0.0.1:8767/
```

项目控制台不需要知道 Codex、PWA、数据库或隧道实现。删除这条项目记录也不会删除会话管理数据。

## 数据

源码运行默认保存在当前仓库：

```text
watchdog.db          监控、配对、远程同步和执行记录
.runtime/            PID、FRP/WireGuard 等运行配置
logs/                服务日志
```

这些路径均在 `.gitignore` 中，不会上传 GitHub。API Key 使用当前 Windows 用户的 DPAPI 加密；同一用户迁移后仍可解密，换设备或换 Windows 用户后需要重新填写。为兼容已配对手机，浏览器存储键和 FRP 配置包格式继续沿用旧标识。

## 端口职责

| 端口 | 监听 | 用途 |
|---|---|---|
| 8767 | `127.0.0.1` | 管理页面、监控配置、本机远程配置 |
| 8766 | `0.0.0.0` | 经过设备令牌认证的手机 PWA |

8767 不应暴露到公网。8766 也不应直接做裸端口映射，推荐使用仓库内的远程配置向导。

使用 Codex Desktop 兼容桥接时，runner 在收到 `/started` 后立即结束，不等待目标会话完成，也不循环调用会话等待接口。详见 [桌面桥接说明](docs/codex-desktop-bridge.md)。

## 安全边界

- 只监控用户明确添加的会话，不读取全部 Codex 会话作为监控目录。
- 远程同步目录和自动续跑监控目录相互独立。
- 手机端看不到项目启动命令、API 密钥或本机任意文件。
- 敏感数据库、服务器凭据、隧道 token 和设备令牌不进入 Git。

详细说明见：

- [会话监控配置与运维](docs/watchdog-configuration.md)
- [远程访问架构](docs/remote-access.md)
- [FRP 配置](docs/frp-remote-access.md)
