# 使用自建 VPS 和 FRP 远程访问

这套方案让电脑主动连接 VPS，手机只访问 VPS 上的 HTTPS 域名。管理端 `127.0.0.1:8767` 永远不进入隧道，只有经过设备认证的远程端 `127.0.0.1:8766` 被转发。

```text
手机 -> HTTPS 443 -> VPS Nginx -> 127.0.0.1:18766
                                      |
电脑 127.0.0.1:8766 <- frpc <-> frps 7000
```

## 准备条件

- 一台带公网 IPv4 的 Linux VPS
- 一个指向 VPS 的域名或子域名
- VPS 防火墙和云安全组允许 TCP `80`、`443`、`7000`
- Windows 电脑可以访问 VPS 的 TCP `7000`
- `frps` 与 `frpc` 使用相同版本

## VPS 端

建议以独立系统用户运行 `frps`。以下是 `/etc/frp/frps.toml` 的核心配置：

```toml
bindAddr = "0.0.0.0"
bindPort = 7000
proxyBindAddr = "127.0.0.1"

[auth]
method = "token"
token = "使用密码生成器创建的长随机值"

[transport.tls]
force = true
```

`proxyBindAddr = "127.0.0.1"` 很重要：FRP 创建的 `18766` 只允许 VPS 本机 Nginx 访问，不直接暴露到公网。

systemd 服务示例：

```ini
[Unit]
Description=FRP Server for Codex Session Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=frp
Group=frp
ExecStart=/opt/frp/frps -c /etc/frp/frps.toml
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/frp

[Install]
WantedBy=multi-user.target
```

Oracle Cloud 的 Ubuntu 镜像即使 UFW 未启用，也可能在 `iptables` 末尾存在拒绝规则。云安全列表放行后仍连接失败时，检查：

```bash
sudo iptables -S INPUT
```

允许规则必须位于最终的 `REJECT` 之前，并通过 `netfilter-persistent` 保存。

## Nginx 和 HTTPS

Nginx 只把手机域名转发到 FRP 在 VPS 本机创建的端口：

```nginx
upstream lpc_remote {
    server 127.0.0.1:18766;
    keepalive 16;
}

server {
    listen 80;
    listen [::]:80;
    server_name console.example.com;

    # 截图以 base64 JSON 上传，Nginx 默认 1m 会在到达 PWA 后端前先返回 413。
    client_max_body_size 8m;

    # 事件流是一次最长 30 秒的长轮询。Nginx 默认缓冲代理响应，会把事件
    # 压在缓冲区里，手机端看起来就是慢好几秒。
    location /api/remote/events {
        proxy_pass http://lpc_remote;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    location / {
        proxy_pass http://lpc_remote;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
    }
}
```

使用 Certbot 或其他 ACME 客户端为域名签发证书，并强制 HTTP 跳转 HTTPS。

`scripts/setup-relay-server.sh` 会按上面的内容生成站点配置，手工部署时以脚本为准。

## Windows 端

1. 下载与服务端相同版本的 `frpc`，或者从对应 Git 标签自行构建。
2. 创建 `.runtime/frp/`。
3. 把客户端保存为 `.runtime/frp/frpc-lpc.exe`。
4. 复制 [`scripts/frpc.example.toml`](../scripts/frpc.example.toml) 为 `.runtime/frp/frpc.toml`。
5. 修改服务器地址和 Token。
6. 启动 Codex 会话管理。

`.runtime/` 已被 Git 忽略。Token、客户端二进制和日志都不会提交到仓库。

会话管理检测到客户端和配置后，会自动启动 `frpc`，并在“远程会话 > 配对设备”显示状态。状态会分别检查本地 8766、VPS relay 和公网 HTTPS；本地正常但公网连续失败达到阈值时才会受控重启 frpc，瞬时网络中断仍由 frpc 自身重连。无敏感信息的本地健康入口为 `/api/remote/health`。

也可以通过环境变量改变默认路径：

```powershell
$env:CSM_FRPC_EXECUTABLE = 'D:\tools\frpc.exe'
$env:CSM_FRPC_CONFIG = 'D:\secrets\frpc.toml'
$env:CSM_FRPC_LOG = 'D:\logs\frpc.log'
```

## Windows Defender

反向代理工具可能被 Defender 识别为 `PUA:Win32/FRProxy`。只允许来源和哈希已经核验的具体文件，不要关闭实时防护，也不要给整个项目目录添加全局排除项。若出现其他木马名称，先停止并重新核验下载来源与 SHA-256。

## 验证顺序

1. `frps` 日志显示客户端登录成功。
2. `frpc` 日志显示 `start proxy success`。
3. 公网域名返回远程会话页面，而不是 `502`。
4. 未配对请求返回 `401`，而不是暴露管理数据。
5. 在桌面端生成新二维码，用手机完成一次性配对。
