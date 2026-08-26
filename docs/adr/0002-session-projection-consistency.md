# ADR-0002：用 SessionProjection 提供分级一致性

状态：已接受

## 背景

为每个浏览器轮询直接执行 `thread/read` 会放大 RPC、CPU 和 DOM 更新。Codex Desktop 又使用
独立 App Server，本项目无法订阅它的全部运行时事件。

## 决策

`SessionProjection` 合并同一会话的并发读取，缓存脱敏快照，并在 App Server 事件或 generation
变化时失效。PWA 以事件驱动更新为主，保留 2.5 秒运行态、8 秒空闲态、30 秒后台态的低频
校准；会话目录状态每 45 秒校准。

本项目 App Server 启动的 turn 提供事件驱动强实时。Codex Desktop 独立启动的 turn 只承诺
最终一致。浏览器 IndexedDB 保存最近 6 个 turn 的快照，仅用于先显示后校准，不作为事实源。

## 结果

切换会话和重新打开 PWA 可以立即显示上次内容，重复读取被合并。Desktop 侧的运行变化仍可能
在下一次校准才出现，这是明确的产品限制，不通过高频全量轮询伪装成强实时。
