# 手机远程访问与会话同步

## 运行结构

控制台启动后同时维护两个 HTTP 入口和一个 Codex App Server 子进程：

```text
127.0.0.1:8765
  本机管理：项目、会话监控、配对与设备撤销

0.0.0.0:8766
  远程访问：PWA、配对领取、已登记会话、脱敏项目状态

Codex App Server / stdio
  thread/read、thread/resume、turn/start、turn/steer、事件通知
```

关闭 `start-console.bat` 所在终端后，两个 HTTP 服务、监控调度器和该控制台创建的 App Server 子进程会一起关闭。

## 网络选择

### 同一局域网

电脑和手机连接同一可信网络时，可使用：

```text
http://<电脑局域网 IP>:8766
```

如果手机无法打开，请确认 Windows 防火墙允许 Python 在专用网络监听 TCP 8766，并确认无线路由器没有启用客户端隔离。

### 跨网络

建议先建立私有网络，再通过 HTTPS 访问，例如 Tailscale HTTPS。也可以使用带 TLS 的可信反向代理。把手机实际能够访问的根地址填入配对页面，例如：

```text
https://console.example.ts.net
```

二维码中的一次性密钥位于 URL fragment，即 `#secret=...`，浏览器不会在 HTTP 请求中把 fragment 发送给服务器。不要在聊天、截图或仓库中公开完整配对链接。

## 权限边界

8766 不提供以下能力：

- 新增、编辑、启动或关闭项目
- 查看项目路径、启动命令、停止命令或完整 URL
- 新增、编辑或删除监控渠道
- 读取兼容 API Key
- 修改会话监控与桌面桥接设置
- 访问未登记在监控目录中的 Codex 会话

远程项目概览只返回项目 ID、名称、类型和状态。会话内容会过滤本地命令、工作目录、命令输出、文件路径、工具参数和隐藏推理正文；用户消息、助手消息和公开的过程摘要可以显示。

## 同会话消息发送

手机发送消息时，服务端先读取指定 `threadId` 的最新状态：

```text
最新 turn = inProgress
  -> turn/steer(threadId, expectedTurnId, input)

没有运行中的 turn
  -> thread/resume(threadId)
  -> turn/start(threadId, input)
```

服务端不会创建替代 thread，也不会自动 fork。`turn/steer` 使用 `expectedTurnId` 作为并发前置条件；如果状态在读取和发送之间发生变化，发送会明确失败并交给用户重试。超时或连接中断造成的“不确定结果”不会自动重发。

## 更新机制

控制台把 App Server 通知转换为只包含以下字段的事件：

- 递增序号
- 事件方法
- 已登记的 `threadId`
- `turnId`
- 安全状态字段
- UTC 时间

手机使用最长 25 秒的长轮询读取事件。收到相关事件后重新调用 `thread/read` 获取脱敏后的最新会话内容。事件流不会直接传输消息 delta、命令输出或工具参数。

## PWA

Service Worker 只缓存 HTML、CSS、JavaScript、图标和二维码库，不拦截或缓存 `/api/` 响应。认证令牌保存在配对设备浏览器的本地存储中。

除 `localhost` 外，浏览器通常只允许在 HTTPS 安全上下文安装 PWA 和启用 Service Worker。局域网 HTTP 仍可作为普通网页使用，但不保证出现“安装到主屏幕”入口。

## 设备丢失或授权异常

在电脑端打开“远程会话 > 配对设备”，撤销对应设备。撤销立即使该设备令牌失效，不影响其他设备。

如果浏览器清除了站点数据，需要重新生成二维码配对。二维码过期、重复领取或密钥错误时，服务端只返回统一失败信息，不泄露具体失败原因。
