# Windows 桌面应用

Local Project Console 的桌面版是现有 Python 工作台的轻量外壳。它负责原生窗口、系统托盘、后台运行和安装升级，不重写项目控制台、会话监控或手机 PWA。

## 运行结构

```text
LocalProjectConsole.exe
  -> 检查 http://127.0.0.1:8765/api/health
  -> 安装版：管理当前用户的 Windows 计划任务
  -> 便携版：隐藏启动随包携带的 lpc-service.exe

lpc-service.exe
  -> 127.0.0.1:8765 项目控制台
  -> 127.0.0.1:8767 会话监控与本机管理
  -> 0.0.0.0:8766 手机 PWA
```

关闭桌面窗口时应用隐藏到托盘。托盘中的“退出桌面应用”不会停止后台；只有“停止后台服务”会停止监控、远程访问和三个监听端口。

## 数据目录

安装版使用：

```text
%LOCALAPPDATA%\LocalProjectConsole\data
%LOCALAPPDATA%\LocalProjectConsole\runtime
%LOCALAPPDATA%\LocalProjectConsole\logs
```

核心服务与辅助运行时日志位于 `logs\system-startup`；项目启动日志继续位于 `logs`。托盘中的“打开日志”会直接打开这个统一日志根目录。源码版为了兼容现有脚本，系统启动日志仍保留在仓库的 `.runtime\system-startup`。

便携版在程序目录使用 `data`、`runtime` 和 `logs`。便携版移动目录前，应先从托盘停止后台。

首次安装会检查已有的 `Local Project Console` 计划任务。如果旧任务指向源码目录，安装版会停止旧后台、在同盘临时目录校验并复制 `projects.json`、`watchdog.db` 和稳定的 FRP 文件，然后切换计划任务。旧文件不会删除；提交或新后台启动失败时，会清理本次未完成的数据并恢复旧计划任务。自动识别失败时，可在启动故障页选择“导入源码版数据”。后台超过 5 秒未就绪时，启动页会提供重试、打开日志和重新注册后台操作。

同一 Windows 用户下，SQLite 中的手机配对信息和 DPAPI 加密渠道密钥可以继续使用。导入其他 Windows 用户的数据后，需要重新填写渠道密钥。

## 构建

需要 Windows 10/11 x64、Python 3.13、Node.js 22、Rust stable 和 WebView2。先安装构建依赖：

```powershell
python -m pip install -r packaging/requirements-build.txt
```

完整构建：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-desktop.ps1
```

生成文件：

```text
release\LocalProjectConsole-Setup-x64.exe
release\LocalProjectConsole-Portable-x64.zip
release\*.sha256
```

只构建 PyInstaller 后台：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-desktop.ps1 -Action BackendOnly
```

## 发布和安全

推送 `v*` 标签会运行 `.github/workflows/windows-release.yml`，执行测试并把安装包、便携包和 SHA-256 上传到 GitHub Release。

发行包不会包含 `projects.json`、`watchdog.db`、FRP Token、服务器地址、日志或手机设备令牌。桌面 WebView 只允许 `127.0.0.1:8765` 留在应用窗口内，其他链接交给系统浏览器；localhost 网页不获得 Tauri 原生 IPC 权限。

当前版本未使用商业代码签名证书，Windows SmartScreen 可能显示“未知发布者”。请从项目 GitHub Releases 下载并核对同名 `.sha256` 文件。

## 卸载

卸载程序会先停止并移除计划任务，然后询问是否删除本地数据。默认选择保留，后续重新安装可以继续使用原有配置和配对设备。
