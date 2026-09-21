# 领域上下文

Codex 会话管理是独立于 Local Project Console 的 Windows 本地应用。它只负责
Codex 会话的自动恢复和远程人工控制，不负责启动普通项目。

## 领域词汇

- **监控渠道**：用户登记的 OpenAI 兼容 API 入口。调度器只用它判断上游是否可用。
- **监控会话**：用户明确加入自动检查目录的 Codex 会话。只有它可能触发自动续跑。
- **同步会话**：用户明确允许手机 PWA 读取和发送消息的会话。它与监控会话是两份独立目录。
- **会话快照**：一次 `thread/read` 得到并脱敏后的有限 turn 视图。
- **恢复规则**：只匹配结构化失败证据或 `error` item 的规则。普通用户或助手正文不是证据。
- **恢复事件**：由会话、失败 turn 和错误签名形成稳定指纹的一次可恢复故障。
- **CodexRuntime**：拥有 App Server 子进程、连接 generation、事件订阅和退避重连的 Module。
- **SessionProjection**：合并并发读取、缓存会话快照并按事件或 generation 失效的 Module。
- **TunnelSupervisor**：分别检查本地 PWA、FRP relay 和公网 HTTPS 的 Module。
- **桌面桥接**：为了让恢复过程尽快出现在 Codex Desktop 中保留的可选兼容 Adapter。

## 核心不变量

1. 渠道不可用时保持静默，绝不发送续跑消息。
2. `completed` 和在飞 turn 在错误规则匹配前短路；在飞以 `completedAt` 缺失为准，
   因为 App Server 会把仍在写入的 turn 报告为 `interrupted`。
3. 结果不确定的写请求不自动重发，避免同一会话收到重复消息。
4. App Server 断开不会终止 HTTP Host；Runtime 会重连并重新订阅事件。
5. 管理端先于 Codex、FRP 和调度器就绪，重功能不能阻塞故障页面。
6. 管理器自己启动的 turn 使用事件驱动强实时；Codex Desktop 独立启动的 turn 只能最终一致。
7. 8767 只监听本机；公网隧道只转发需要设备令牌的 8766。

## 运行结构

一个 Python Host 监听 8767 和 8766。Codex App Server 与可选 frpc 是它管理的必要
子进程，不再拆出其他常驻 Python 进程。SQLite 保存监控、同步、配对和审计数据；
浏览器 IndexedDB 只保存最近会话快照，用于快速恢复显示。
