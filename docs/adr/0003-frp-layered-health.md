# ADR-0003：FRP 使用分层健康而非进程存活判断

状态：已接受

## 背景

frpc 进程存在只说明客户端尚未退出，不能证明本地 PWA、VPS relay 或公网 HTTPS 可用。
瞬时网络抖动又不应该触发反复重启。

## 决策

`TunnelSupervisor` 分别观测本地 8766、FRP relay 和公网 HTTPS，状态使用
`starting/running/degraded/failed/stopped`。保留 frpc 的实际退出码。只有本地正常而公网连续
失败达到阈值时，才受控重启 frpc；一般网络重连继续交给 frpc 自身处理。

`/api/remote/health` 只返回无敏感信息的健康数据。WireGuard、Tailscale 等外部组网不与 FRP
同时自动管理。

## 结果

502 可以定位到具体层级，短暂丢包不会形成重启风暴。公网探测会产生少量低频流量，并需要
正确配置公开根地址。
