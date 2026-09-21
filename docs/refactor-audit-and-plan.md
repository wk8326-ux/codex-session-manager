# Codex 会话管理：架构审计与整改方案

本文基于 CodeGraph 索引（65 文件 / 1775 节点 / 5152 边）和本地实测取证，
回答用户提出的四条痛点：远程链路不稳定、本地进程偏重、服务器与代理路径未优化、
同步实时性无法保证。每条结论都附实测证据，每条改法都说明「为什么这样改能提升体验或稳定性」。

术语沿用 `CONTEXT.md`：监控渠道、监控会话、同步会话、会话快照、恢复规则、恢复事件、
CodexRuntime、SessionProjection、TunnelSupervisor、桌面桥接。

---

## 一、四条痛点与实测根因对照

| 用户痛点 | 实测根因 | 关键证据 |
| --- | --- | --- |
| 远程 FRP / 组网不稳定 | 公网链路串了两台 VPS、两次跨公网、两次 TLS 终结 | 固定往返 2001/2000/2001ms，非抖动；DNS 仍指向旧 Oracle |
| 本地进程有点重 | `thread/list` 未用状态库，为凑行数扫描 2GB 会话文件，10s 超时后拖垮整个 Runtime | `limit=50` 实测 16.11s、仅 7 条唯一；加 `useStateDbOnly` 后 0.01s、50 条唯一 |
| 服务器 / 代理路径欠缺优化 | 渠道探针与隧道探针继承系统代理，把「代理故障」混同为「渠道故障」 | 同渠道走代理 2002ms vs 直连 231ms，慢 8.7 倍 |
| 同步响应与实时性难以保证 | 列表与详情用两套状态判定，列表兜底 `stopped`；轮询全量刷 45s | 列表显示「已停止」，点开显示「已完成/运行中」 |

---

## 二、架构映射

当前分层与越界点（越界即本方案的整改对象）：

```
Presentation
  remote.html / remote.js (2852 行)     ← 越界：内嵌状态判定规则
  watchdog.html (1549 行)               ← 越界：内嵌会话选择与轮询逻辑
  session-manager.html / session-manager-nav.js

Application
  remote/application.py (405 行)        ← 越界：捕获适配器异常后统一转 502
  remote/router.py                      ← 越界：所有 RemoteApplicationError → BAD_GATEWAY
  watchdog/application.py (582 行)
  watchdog/service.py (598 行)

Domain
  watchdog/decision.py (104 行)         ← 决策链清晰，作为参照基线
  remote/projection.py (308 行)         ← SessionProjection，缓存与失效策略集中
  watchdog/models.py / remote/models.py

Infrastructure
  watchdog/codex_adapter.py (905 行)    ← 越界：list_threads 未用 useStateDbOnly
  watchdog/codex_runtime.py             ← 越界：UncertainSendFailure 即断连
  watchdog/channels.py                  ← 越界：代理策略隐含在 _default_opener
  remote/tunnel.py (538 行)             ← 越界：_http_probe 继承系统代理
  watchdog/store.py (1219 行)           ← 越界：8 类职责混在一个模块
```

目标模式：把「网络可达性」与「上游语义」解耦，把「状态判定」收敛为单一定义源，
把「写请求不确定性」与「连接健康」分离。这三件事分别对应下文的 P0-a/b/c 与 P1-a。

---

## 三、分级问题清单

### P0-a：`thread/list` 改用状态库读取，并按唯一会话数取数

**现象**：远程会话「选择本机 Codex 会话」加载不出来，或长时间转圈后报 502；
会话列表出现同一会话重复多行。

**实测证据**（同一 `StdioJsonRpcClient`，本机复验）：

```
{'limit': 50}                           -> 16.11s  rows=50  uniq=7
{'limit': 50,  'useStateDbOnly': True}  ->  0.01s  rows=50  uniq=50
{'limit': 100, 'useStateDbOnly': True}  ->  0.06s  rows=59  uniq=59
```

重复来源已定位：`limit` 是「扫描行数」而非「唯一会话数」。`watchdog/codex_adapter.py`
的 `list_threads()` 在取数之后才用 `seen_thread_ids` 去重，为了凑够 50 行必须扫描
`~/.codex/sessions`（232 个 jsonl、合计 2060.8MB、单文件最大 330MB）。

**改法**：`thread/list` 请求增加 `useStateDbOnly=True`，并保持现有去重逻辑作为兜底。

**为什么能提升体验与稳定性**：
1. 请求从 16s 级降到 10ms 级，列表能即时渲染，用户不再面对「加载不出来」。
2. 超时消失后，`UncertainSendFailure` 不再触发，连带消除了「一次列表失败导致整个
   Codex 连接被判为断开」的连锁故障。
3. 唯一会话数从 7 提升到 50，重复行消失，用户不会误以为存在多个同名会话。

**影响面**：`watchdog/codex_adapter.py` 单方法 + 对应单测。

**验收**：新增单测断言请求参数含 `useStateDbOnly`；实测 `limit=50` 响应 < 200ms 且
`rows == uniq`。

---

### P0-b：超时与上游故障语义分离

**现象**：一次读取超时会让后续状态查询一起失灵；HTTP 层统一返回 502，无法区分
「超时」「上游故障」「内部错误」。

**实测证据**：

```
11.38s  /api/remote/local-sessions?limit=50         -> HTTP 502
10.81s  /api/watchdog/local-codex-sessions?limit=50 -> HTTP 503
```

第二条是 503 而非 502，说明超时后 Runtime 被判为 disconnected，错误已经扩散到
整个连接状态。链路为：`codex_adapter.py` 抛 `UncertainSendFailure`
→ `codex_runtime.py:145 _mark_disconnected()` → `remote/application.py:235`
转 `RemoteApplicationError` → `remote/router.py:148` 映射 502。

**改法**：
1. 只读方法（`list_threads` / `read_status` / `read_thread_detail`）的超时不触发
   `_mark_disconnected()`；连接健康仍由 `_client_alive()` 与心跳判定。
2. 新增 `CodexReadTimeout` 语义，HTTP 层映射为 504，与 502（上游故障）区分。

**为什么能提升体验与稳定性**：
1. 单次慢请求不再降级整个 Codex 连接，用户不会因为一个列表接口超时而看到
   「会话监控整体不可用」。
2. 504/502 可区分后，前端能给出「本次读取超时，请重试」而不是「后端不可用」，
   用户知道该重试还是该排查上游。
3. 连接不再频繁重建，App Server 子进程的启停次数下降，本地进程更轻。

**影响面**：`codex_runtime.py`、`codex_adapter.py`、`remote/router.py`、
`watchdog/router.py`。属于错误契约变更（refactor skill Red Line），
因此需要单独提交、单独验收。

**验收**：注入超时后 Runtime 仍报告 connected；HTTP 返回 504 且 message 明确为超时。

---

### P0-c：探针代理策略显式化

**现象**：渠道探测显示「网络异常」，但同一渠道在浏览器中正常。

**实测证据**：

```
urllib.request.getproxies() -> {'http': 'http://127.0.0.1:10808', ...}
HKCU ProxyEnable=1, ProxyServer=127.0.0.1:10808
同渠道：经代理 2002ms / 直连 231ms
```

`watchdog/channels.py` 的 `_default_opener` 只对 loopback 绕过代理，
非 loopback 走 `build_opener()` 继承系统代理；`remote/tunnel.py` 的 `_http_probe`
使用裸 `urlopen`，同样继承。后果是代理返回的 502 与渠道自身 502 的 detail
完全一致（`channel upstream was unavailable`），无法区分。

**改法**：
1. 抽出统一的 `build_probe_opener(url, *, use_system_proxy)`，两处共用。
2. 渠道探针与隧道探针默认直连，只有显式配置时才走系统代理。
3. 探针结果区分 `proxy_error` 与 `upstream_error`，写入 detail。

**为什么能提升体验与稳定性**：
1. 直连比经代理快 8.7 倍，探测周期缩短，调度器不会因探测本身占用而顺延。
2. 区分代理故障与渠道故障后，用户不会再被误导去排查一个其实健康的渠道。
3. 隧道探针不受本机代理状态影响，公网可达性判定反映真实链路，而不是本机代理是否开启。

**影响面**：`watchdog/channels.py`、`remote/tunnel.py` + 单测。

**验收**：在系统代理开启的环境下，loopback 与非 loopback 探针均不走代理；
`proxy_error` 与 `upstream_error` 可被单测区分。

---

### P0-d：续跑开关可见化

**现象**：用户反复反馈「明明命中了恢复规则，却没有续跑」。

**实测证据**：

```
GET /api/watchdog/status -> resumeActionsEnabled: false, schedulerEnabled: true
monitor_runs 10030 条：resume_candidate_observed 41 / resume_completed 4
```

41 次命中恢复规则但被开关拦下，真正续跑仅 8 次。安全默认值本身合理，
问题在于「命中但被拦下」在 UI 上表现为「静默」，用户无法得知原因。

**改法**：
1. `resume_candidate_observed` 在列表中显式标注为「已命中恢复规则，续跑开关未开启」。
2. 首次出现该状态时给一次性引导，说明开关位置与影响。
3. 不修改默认值。

**为什么能提升体验与稳定性**：
1. 用户能立刻区分「没有命中规则」与「命中了但被开关拦下」，排查时间从「反复提问」
   降到「看一眼列表」。
2. 保持默认关闭，避免在用户未确认时自动发送消息，稳定性不受影响。

**影响面**：`watchdog.html` 渲染 + `watchdog/service.py` 状态码文案。不改决策逻辑。

---

### P0-e：公网链路扁平化

**现象**：远程访问慢且不稳定，手机端偶发 502。

**实测证据**：

```
Resolve-DnsName console.holdzywoo.top -> 161.33.78.216   （旧 Oracle，非阿里云）
Test-NetConnection 39.96.202.121:18766 -> False          （阿里云未放行）
Test-NetConnection 39.96.202.121:7000  -> True, 22ms
https://console.holdzywoo.top/api/remote/health -> 200
https://39.96.202.121/... -> CERTIFICATE_VERIFY_FAILED (IP mismatch)
往返延迟：2001 / 2000 / 2001 ms
```

当前链路为：浏览器 → CF DNS（指向旧 Oracle）→ 旧 Oracle nginx（TLS 终结）→
`proxy_pass https://39.96.202.121` → 阿里云 nginx → `127.0.0.1:18766` →
frpc 隧道 → 本地 8766。即两次跨公网、两次 TLS、两台 VPS 串联。
固定 ~2s 往返说明这是链路层数问题，不是带宽或抖动。

**改法**：
1. DNS `console.holdzywoo.top` 直接指向阿里云 `39.96.202.121`。
2. 阿里云放行 18766，nginx 直接 `proxy_pass http://127.0.0.1:18766`。
3. 保留旧 Oracle 配置作为回滚路径，不立即删除。

**为什么能提升体验与稳定性**：
1. 去掉一次跨公网与一次 TLS 握手，往返延迟从 ~2s 降到亚秒级，同步实时性直接改善。
2. 链路节点从 6 个减到 4 个，任一台机器抖动导致失败的概率显著下降。
3. 证书与 IP 匹配，消除 `CERTIFICATE_VERIFY_FAILED` 引发的失败重试。

**影响面**：DNS、阿里云安全组、nginx 配置。属于外部状态变更，需要用户确认后执行。

---

### P1-a：统一状态判定

**现象**：列表显示「已停止」，点开后显示「已完成」或「运行中」。

**实测证据**：

- 列表未选中走 `remote.js:819 sessionSnapshotStatus()`，兜底 `return 'stopped'`。
- 已选中走 `remote.js:1410 conversationStatus()`，另有 180s 活动宽限与
  `latestTurnError` 检查。
- 后端 8 条数据状态正确：`completed` / `interrupted` / `inProgress` 各就各位。

两套判定链不一致，列表侧缺少活动宽限与错误检查，且兜底值把「未知」渲染成「已停止」。

**改法**：抽出单一 `resolveSessionStatus(session, detail?, activity?)`，
列表与详情共用；兜底值改为 `unknown`。

**为什么能提升体验与稳定性**：
1. 同一会话在列表与详情中状态一致，用户不再怀疑「到底哪个是真的」。
2. 「未知」与「已停止」区分后，用户不会误以为一个仍在运行的会话已经结束，
   也就不会误触发续跑。
3. 判定逻辑只有一份，后续新增状态只需改一处，降低回归风险。

**影响面**：`remote.js`。属于前端行为变更，需单独提交。

**验收**：同一会话在列表与详情渲染同一状态；无数据时显示「未知」而非「已停止」。

---

### P1-b：本机会话列表复用廉价读取路径并补测试

**现象**：`list_local_sessions` 当前无测试覆盖，且直接依赖 `list_threads`。

**改法**：复用 `read_status` 已验证的廉价读取策略；补充覆盖
`remote/application.py:233` 的成功与异常分支。

**为什么能提升体验与稳定性**：
1. 无覆盖的路径是回归高发区，补测后改动 P0-a 时能立即发现破坏。
2. 与 P0-a 配合，列表接口端到端稳定在百毫秒级。

---

### P1-c：轮询降频与增量刷新

**实测证据**：`hydrateSessionStatuses` 每 45s 全量刷 `/api/remote/sessions`，
实测耗时 2.85s；`ACTIVE_REFRESH_MS=2500`。

**改法**：状态刷新改为按会话增量请求，仅在选中会话活跃时保持 2.5s 频率。

**为什么能提升体验与稳定性**：
1. 全量刷 2.85s 期间主线程竞争，用户会感觉界面卡顿；增量后单次请求更小。
2. 非活跃会话不再高频请求，App Server 负载下降，本地进程更轻。

---

### P1-d：记录保留策略收紧

**实测证据**：`monitor_runs` 10030 条，默认 `record_retention_days=90`、
`record_limit=10000`。绝大多数是 `silent_channel_unavailable`（4678 条）。

**改法**：默认保留 14 天 / 2000 条，保留设置项可调。

**为什么能提升体验与稳定性**：
1. 静默记录占据绝大多数，压缩后查询更快、数据库更小。
2. 真正需要排查的 `resume_*` 记录保留比例反而更高。

---

### P1-e：拆分 `watchdog/store.py`

**现状**：1219 行，混合 settings / recovery rules / channels / sessions /
monitor runs / desktop bridge / incidents / pruning 共 8 类职责。

**改法**：按职责拆为 8 个模块，保留 `WatchdogStore` 作为门面。

**为什么能提升体验与稳定性**：
1. 单模块职责单一后，改动一处不会意外影响其他表。
2. 拆分后可针对每个职责单独测试，回归定位更快。

**前置条件**：先给出收益论证，不为拆而拆。本项排在 P1 最后。

---

### P2-a：FRP 连接池与健康检查调优

**实测证据**：`.runtime/frp/frpc.log` 中 `work connection pool is full, discarding`
出现 633 次，集中在同一秒；伴随 `connect to local service [127.0.0.1:8766] error:
actively refused it`。现行 `frpc.toml` 无 `poolCount`、无 `healthCheck`。

**改法**：配置 `transport.poolCount` 与 `healthCheck`，本地端口不可用时不再无限重试。

**为什么能提升体验与稳定性**：
1. 连接池耗尽会让公网请求整体排队，配置池大小后消除该故障模式。
2. 健康检查让 frpc 在本地服务未就绪时快速失败，而不是堆积重试。

---

### P2-b：PWA 移动端适配

**现状**：`remote.html` viewport 已含 `viewport-fit=cover` 与
`interactive-widget=resizes-content`；`remote.css` 已有 1120/761/760/480/360 断点
与 `prefers-reduced-motion`。

**改法**：需要真机验证后再改。此前两次修复方向出错，本次不盲改。

---

### P2-c：8767「打开即关闭」

**实测**：`/`、`/watchdog`、`/remote`、`/session-manager`、`/remote-setup`、
`/api/health` 全部 200，未复现。`window.close` / `beforeunload` 检索无相关逻辑。

**改法**：暂不改，需要用户提供复现条件（设备、浏览器、操作步骤、时间点）。

---

## 四、实施顺序

每一步单独提交、可回滚，遵循 refactor skill 的「一次一个操作、每步跑测试」。

| 顺序 | 内容 | 风险 | 测试范围 |
| --- | --- | --- | --- |
| 1 | P0-a `useStateDbOnly` | 低 | `tests.test_codex_adapter` |
| 2 | P0-c 探针代理策略 | 低 | `tests.test_watchdog_channels`、`tests.test_remote_tunnel` |
| 3 | P1-a 统一状态判定 | 中 | `tests.test_remote_ui` |
| 4 | P1-b 列表补测 | 低 | `tests.test_remote_application` |
| 5 | P0-b 超时语义分离 | 中高 | `tests.test_codex_runtime`、`tests.test_remote_router` |
| 6 | P0-d 续跑开关可见化 | 低 | `tests.test_watchdog_ui` |
| 7 | P1-c 轮询降频 | 中 | `tests.test_remote_ui` |
| 8 | P1-d 保留策略 | 低 | `tests.test_watchdog_store` |
| 9 | P2-a FRP 调优 | 低 | 手工验证 |
| 10 | P1-e store 拆分 | 高 | 全量 |

第 1、2 步完成后跑一次全量（314 tests）确认基线。

---

## 五、本次不改

1. `resume_actions_enabled` 默认值保持 0（安全默认合理）。
2. 决策链 `watchdog/decision.py` 不改（逻辑清晰，已验证正确）。
3. `remote/projection.py` 缓存策略不改（1.5s / 60s / 6s 已实测有效）。
4. 8767 打开即关闭、PWA 移动端适配：需复现条件，不盲改。
5. 公网 DNS 与 nginx 变更：需用户确认后执行。

---

## 六、预期效果对照

| 指标 | 当前 | 目标 |
| --- | --- | --- |
| 本机会话列表响应 | 16.11s 或超时 502 | < 200ms |
| 列表唯一会话数 | 7 / 50 行 | 50 / 50 行 |
| 单次超时对连接的影响 | 整个 Runtime 断开 | 仅本次请求返回 504 |
| 渠道探测耗时 | 经代理 2002ms | 直连 231ms |
| 公网往返 | ~2000ms | 目标亚秒级 |
| 状态一致性 | 列表与详情不一致 | 单一定义源 |
| 监控记录量 | 10030 条 | ≤ 2000 条 |
