[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter()]
    [string]$Bundle = $env:LPC_SETUP_BUNDLE,

    [Parameter()]
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),

    [Parameter()]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Fail([string]$Message) {
    throw "[CSM] $Message"
}

function Decode-Bundle([string]$Encoded) {
    $value = ($Encoded -replace '^LPC_CONFIG_BUNDLE=', '').Trim()
    if ([string]::IsNullOrWhiteSpace($value) -or $value.Length -gt 16384) {
        Fail '配置包为空或长度异常。'
    }
    $standard = $value.Replace('-', '+').Replace('_', '/')
    switch ($standard.Length % 4) {
        2 { $standard += '==' }
        3 { $standard += '=' }
        1 { Fail '配置包格式无效。' }
    }
    try {
        $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($standard))
        $config = $json | ConvertFrom-Json
    }
    catch {
        Fail '配置包无法解析。'
    }
    if ($config.format -ne 'lpc-frp-v1') { Fail '配置包版本不受支持。' }
    if ($config.serverAddr -notmatch '^[A-Za-z0-9:.-]+$') { Fail '服务器地址无效。' }
    if ([int]$config.serverPort -lt 1 -or [int]$config.serverPort -gt 65535) { Fail 'FRP 端口无效。' }
    if ([int]$config.remotePort -lt 1 -or [int]$config.remotePort -gt 65535) { Fail '转发端口无效。' }
    if ($config.frpVersion -notmatch '^\d+\.\d+\.\d+([+-][A-Za-z0-9.-]+)?$') { Fail 'FRP 版本无效。' }
    if ($config.publicUrl -notmatch '^https://[A-Za-z0-9.-]+$') { Fail '公网入口无效。' }
    if ([string]::IsNullOrWhiteSpace($config.token) -or $config.token.Length -lt 32 -or $config.token.Length -gt 256) {
        Fail '连接凭据无效。'
    }
    return $config
}

if ([string]::IsNullOrWhiteSpace($Bundle)) {
    $Bundle = Read-Host '粘贴 VPS 输出的 LPC_CONFIG_BUNDLE'
}
$config = Decode-Bundle $Bundle
$runtimeDirectory = Join-Path $ProjectRoot '.runtime\frp'
$executablePath = Join-Path $runtimeDirectory 'frpc-lpc.exe'
$configPath = Join-Path $runtimeDirectory 'frpc.toml'
$versionPath = Join-Path $runtimeDirectory 'frpc.version'
$logPath = Join-Path $runtimeDirectory 'frpc.log'

Write-Host "[CSM] 服务器: $($config.serverAddr):$($config.serverPort)"
Write-Host "[CSM] 公网入口: $($config.publicUrl)"
Write-Host "[CSM] FRP 版本: $($config.frpVersion)"

if ($DryRun) {
    Write-Host '[CSM] Dry run passed. No files were changed.'
    exit 0
}

New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
$installedVersion = if (Test-Path -LiteralPath $versionPath) {
    (Get-Content -Raw -LiteralPath $versionPath).Trim()
} else { '' }

if (-not (Test-Path -LiteralPath $executablePath) -or $installedVersion -ne $config.frpVersion) {
    $architecture = switch ($env:PROCESSOR_ARCHITECTURE) {
        'AMD64' { 'amd64' }
        'ARM64' { 'arm64' }
        default { Fail "暂不支持的 Windows 架构：$env:PROCESSOR_ARCHITECTURE" }
    }
    $assetName = "frp_$($config.frpVersion)_windows_$architecture.zip"
    $releaseUri = "https://api.github.com/repos/fatedier/frp/releases/tags/v$($config.frpVersion)"
    Write-Host '[CSM] 正在读取 FRP 官方发布信息...'
    $release = Invoke-RestMethod -Uri $releaseUri -Headers @{ 'User-Agent' = 'LocalProjectConsole/1.0' }
    $asset = $release.assets | Where-Object { $_.name -eq $assetName } | Select-Object -First 1
    if ($null -eq $asset -or $asset.browser_download_url -notlike 'https://github.com/*') {
        Fail "没有找到官方发布文件 $assetName。"
    }
    if ($asset.digest -notmatch '^sha256:([A-Fa-f0-9]{64})$') {
        Fail '官方发布信息没有提供 SHA-256 摘要，已停止下载。'
    }
    $expectedHash = $Matches[1].ToLowerInvariant()
    $downloadDirectory = Join-Path $runtimeDirectory 'download'
    $archivePath = Join-Path $downloadDirectory $assetName
    if (Test-Path -LiteralPath $downloadDirectory) {
        Remove-Item -LiteralPath $downloadDirectory -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $downloadDirectory | Out-Null
    try {
        Write-Host '[CSM] 正在下载并校验 frpc...'
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archivePath -Headers @{ 'User-Agent' = 'LocalProjectConsole/1.0' }
        $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
        if ($actualHash -ne $expectedHash) { Fail 'frpc 下载文件的 SHA-256 不匹配。' }
        Expand-Archive -LiteralPath $archivePath -DestinationPath $downloadDirectory -Force
        $downloadedExecutable = Get-ChildItem -LiteralPath $downloadDirectory -Recurse -Filter 'frpc.exe' | Select-Object -First 1
        if ($null -eq $downloadedExecutable) { Fail '下载包中没有找到 frpc.exe。' }
        Copy-Item -LiteralPath $downloadedExecutable.FullName -Destination $executablePath -Force
        Set-Content -LiteralPath $versionPath -Value $config.frpVersion -Encoding ASCII
    }
    finally {
        if (Test-Path -LiteralPath $downloadDirectory) {
            Remove-Item -LiteralPath $downloadDirectory -Recurse -Force
        }
    }
}

if (Test-Path -LiteralPath $configPath) {
    $backupPath = "$configPath.$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')).bak"
    Copy-Item -LiteralPath $configPath -Destination $backupPath
}

$toml = @"
serverAddr = "$($config.serverAddr)"
serverPort = $([int]$config.serverPort)
loginFailExit = false

[auth]
method = "token"
token = "$($config.token)"

[transport]
protocol = "tcp"
poolCount = 8
dialServerTimeout = 10
dialServerKeepalive = 7200
heartbeatInterval = 30
heartbeatTimeout = 90

[transport.tls]
enable = true

[[proxies]]
name = "local-project-console-remote"
type = "tcp"
localIP = "127.0.0.1"
localPort = 8766
remotePort = $([int]$config.remotePort)

[proxies.healthCheck]
type = "tcp"
timeoutSeconds = 2
maxFailed = 2
intervalSeconds = 3
"@
Set-Content -LiteralPath $configPath -Value $toml -Encoding UTF8
if (-not (Test-Path -LiteralPath $logPath)) {
    New-Item -ItemType File -Path $logPath | Out-Null
}

Write-Host '[CSM] 本机 FRP 配置已写入 .runtime\frp。'
try {
    $result = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8767/api/relay-setup/tunnel/start' -ContentType 'application/json' -Body '{}'
    if ($result.tunnel.running) {
        Write-Host '[CSM] FRP 隧道已经启动。'
    }
    else {
        Write-Host '[CSM] 配置已完成，请重启会话管理以启动 FRP 隧道。'
    }
}
catch {
    Write-Host '[CSM] 配置已完成。会话管理未响应，请重启后启动 FRP 隧道。'
}

Write-Host '[CSM] Windows Defender 可能将反向代理工具标记为 PUA。只允许本脚本从官方发布页下载且 SHA-256 校验通过的具体文件。'
