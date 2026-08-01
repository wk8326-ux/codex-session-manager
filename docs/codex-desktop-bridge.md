# Codex Desktop 实时桥接

## 为什么需要桥接

控制台通过 `codex app-server --listen stdio://` 启动的 App Server 与 Codex Desktop 自己使用的 App Server 不是同一个进程。独立 App Server 可以读取已经持久化的会话历史，也可以创建新的 turn，但它创建的 turn 不会把运行中的 commentary、工具调用和审批状态实时推送到 Codex Desktop。

`desktop_bridge` 模式把“是否应该恢复”与“由谁发送恢复消息”拆开：

1. 控制台继续探测 API、读取会话、匹配恢复规则并去重。
2. 命中恢复条件后，控制台原子写入恢复事件、执行记录和桌面桥接任务。
3. Codex Desktop 中的桥接 runner 领取一个任务，并通过桌面原生 `send_message_to_thread` 启动目标会话。
4. runner 回传新的 desktop turn ID，再等待目标任务完成或需要关注。
5. runner 回传最终状态，控制台更新原执行记录。

同一恢复事件只选择一个发送模式，不会同时调用桌面桥接和独立 App Server。

## 启用步骤

1. 启动控制台，并确认 `http://127.0.0.1:8765/api/watchdog/status` 可访问。
2. 在会话监控页把“恢复通道”切换为 `Codex Desktop · 实时同步`。
3. 在 Codex Desktop 中选择一个专用的本地桥接任务，为它创建 heartbeat 自动化。runner 可以绑定任意本地任务，不要把设备名或个人路径写进项目代码。
4. 使用一次性测试会话完成领取、启动、实时显示和完成回执测试。
5. 测试通过后，再开启全局自动续跑并启用实际监控会话。

控制台关闭后，HTTP 领取接口不可访问，因此 heartbeat 会静默结束，不会恢复任何会话。这样监控的实际生命周期仍由控制台终端控制。删除 heartbeat 则会完全移除桌面桥接 runner。

## Heartbeat 提示词模板

将下面内容作为专用 Codex Desktop 任务的 heartbeat 提示词。端口改变时只需要修改 `baseUrl`，无需修改项目源码。

```text
你是 Local Project Console 的 Codex Desktop 桥接 runner。

baseUrl: http://127.0.0.1:8765
runnerId: codex-desktop-local

每次运行只处理一个任务，并严格执行：

1. POST /api/watchdog/bridge/jobs/claim，JSON 为
   {"runnerId":"codex-desktop-local","leaseSeconds":120}。
2. 如果控制台不可访问，或响应为空，静默结束；不要创建任务，不要重试其他地址。
3. 如果领取到任务，保存 id、leaseToken、threadId、prompt。
4. 先用 read_thread(threadId, turnLimit=1) 记录发送前的最新 turn ID，再通过 Codex Desktop 的 send_message_to_thread 向 threadId 发送 prompt。
5. 发送被明确拒绝时，POST /api/watchdog/bridge/jobs/{id}/finish，JSON 为
   {"leaseToken":"...","outcome":"dispatch_failed","detail":"简短错误"}，然后结束。
6. send_message_to_thread 被接受后，继续用 read_thread(threadId, turnLimit=1) 读取最新 turn，直到它与发送前的 turn ID 不同；这个新 ID 才是 resumedTurnId。随后 POST /api/watchdog/bridge/jobs/{id}/started，JSON 为
   {"leaseToken":"...","resumedTurnId":"..."}。
7. 通过 wait_threads 等待该 threadId。completed 回传 outcome=completed；failed 回传 failed；interrupted 回传 interrupted；需要审批或用户输入时回传 manual_attention。
8. 最终 POST /api/watchdog/bridge/jobs/{id}/finish，并附带 leaseToken、outcome 和不超过 500 字的 detail。
9. 一旦 send_message_to_thread 已被接受，绝不对同一个 job 再次发送。每轮绝不领取第二个任务。
10. 不修改项目文件，不扫描其他会话，不处理领取结果之外的 threadId。
```

## Bridge API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/watchdog/bridge/status` | 查看模式、待领取、已领取、运行中和已结束数量 |
| `POST` | `/api/watchdog/bridge/jobs/claim` | 原子领取最早的待处理任务并获得租约 |
| `POST` | `/api/watchdog/bridge/jobs/{id}/started` | 登记桌面 turn ID，将执行记录改为 `resume_started` |
| `POST` | `/api/watchdog/bridge/jobs/{id}/finish` | 回传完成、失败、中断、人工关注或明确发送失败 |

领取租约默认 90 秒，可设置为 30 到 300 秒。租约到期且尚未登记 turn ID 的任务可被再次领取；一旦登记为 started，就不会再次出现在领取队列。启动和结束回调均支持相同参数的幂等重复提交。

明确发送失败继续使用原有重试策略：第一次约 30 秒后重试，第二次约 120 秒后重试，总计最多 3 次。API 已可用但桌面发送仍连续失败，通常说明桥接逻辑、桌面状态或 thread ID 有问题，第三次后会转为人工关注。

## 安全边界

- 接口只由监听 `127.0.0.1` 的本地控制台提供。
- bridge job 仅包含用户已加入监控目录的 thread ID 和该会话自己的续跑提示词。
- API Key 不进入 bridge job，也不会返回给 runner。
- runner 不扫描全部 Codex 会话。
- 执行记录只保存脱敏原因，不保存完整模型输出。
- 开源仓库不保存设备名、用户目录、真实 thread ID、heartbeat 自动化 ID 或本地数据库。
