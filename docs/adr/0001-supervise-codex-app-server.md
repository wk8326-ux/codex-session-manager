# ADR-0001：由 CodexRuntime 监管 App Server

状态：已接受

## 背景

会话监控和远程 PWA 共用一个 stdio App Server。一次性启动会让子进程退出后两个功能
同时失效，静态 `codexConnected` 还会继续报告已连接。

## 决策

由一个深的 `CodexRuntime` Module 拥有子进程、动态健康状态、连接 generation、事件订阅
和带上限的退避重连。重连成功后重新注册事件处理器。管理端 HTTP Host 不依赖 App Server
是否连接。

写请求已经发送但结果不确定时，不由 Runtime 自动重发；调用方必须将其转为人工确认。

## 结果

App Server 崩溃不会要求重启整个应用。健康接口能展示 connecting、lastError 和 retryInMs。
代价是调用方必须接受暂时不可用和 generation 变化，并通过 SessionProjection 失效旧快照。
