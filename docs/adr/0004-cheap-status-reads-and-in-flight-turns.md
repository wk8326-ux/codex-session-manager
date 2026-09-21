# ADR-0004：用廉价状态查询与在飞判定替代全量 transcript 轮询

状态：已接受

## 背景

会话列表和监控调度都需要频繁判断「这个会话是在运行、已完成，还是失败中断」。
原实现每次调用 `thread/read(includeTurns=True)`，在长会话上实测 1.6–6.9 秒，
超过投影层 3 秒的等待预算，导致每次刷新都超时并丢弃结果。前端因此把刚测量过的
会话显示成「状态未知」，抽屉把它渲染成「已停止」。

同时 App Server 会把一个仍在写入的 turn 报告为 `interrupted`，`error` 为空。
原判定只看状态字符串，于是运行中的会话被当成中断，运行中的续跑 turn 被提前
`finalize`，事件指纹也可能落到没有错误证据的最新 turn 上。

## 决策

1. 新增 `CodexAppServerAdapter.read_status()`：`thread/read(includeTurns=False)`
   取元数据，再配 `thread/turns/list` 取最近若干 turn，合计约 30ms。它是会话
   生命周期的事实源；`read_thread` 仍负责对话渲染，不参与状态轮询。
2. `TurnSnapshot.in_flight` 判定为 `status == "inProgress"`，或
   `startedAt` 有值而 `completedAt` 为空。实测数据中所有真实终态都带
   `completedAt`，只有在飞的 turn 为空。
3. `SessionSnapshot.evidence_turn` 在最新 turn 没有错误证据时，回退到最近一个
   仍能解释这次中断的失败 turn；向前遇到正常完成的 turn 即停止，避免用早已
   翻篇的旧错误触发续跑。
4. `SessionProjection` 为状态单独设置 60 秒新鲜窗口（对话快照仍是 1.5 秒），
   并把等待预算放宽到 6 秒以覆盖一次真实读取。超时或读失败时回退到缓存状态，
   不再返回 `None`。

## 结果

状态刷新从秒级降到毫秒级，本地进程不再因为轮询长会话而显得沉重。运行中的会话
稳定显示为运行中，运行中的续跑不会被误判为结束。最新 turn 无错误证据、但更早
turn 有 503 的会话现在可以命中恢复规则并续跑。

代价是状态最多可能滞后 60 秒。PWA 的事件覆盖和会话打开时的实时投影仍然提供
强实时，状态缓存只影响列表上的生命周期标签。
