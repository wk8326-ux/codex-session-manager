use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::{AppHandle, Manager};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

const HEALTH_URL: &str = "http://127.0.0.1:8765/api/health";
const TASK_NAME: &str = "Local Project Console";
const CURRENT_VERSION: &str = "0.1.0";
const RELEASE_API: &str =
    "https://api.github.com/repos/wk8326-ux/localhost-project-console/releases/latest";
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const DETACHED_PROCESS: u32 = 0x0000_0008;

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimeHealth {
    #[serde(default)]
    pub service: String,
    #[serde(default)]
    pub version: String,
    #[serde(default)]
    pub ready: bool,
    #[serde(default)]
    pub auxiliary_ready: bool,
    #[serde(default)]
    pub mode: String,
    #[serde(default)]
    pub pid: u32,
    #[serde(default)]
    pub started_at: String,
    #[serde(default)]
    pub ports: RuntimePorts,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimePorts {
    #[serde(default)]
    pub admin: u16,
    #[serde(default)]
    pub remote: u16,
    #[serde(default)]
    pub auxiliary: u16,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BootstrapResult {
    #[serde(flatten)]
    pub health: RuntimeHealth,
    pub last_route: String,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportResult {
    pub cancelled: bool,
    pub source: String,
}

#[derive(Clone, Debug)]
pub struct ReleaseInfo {
    pub version: String,
}

#[derive(Deserialize)]
struct GithubRelease {
    tag_name: String,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopState {
    last_route: String,
    last_update_check: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RuntimeMode {
    Installed,
    Portable,
}

impl RuntimeMode {
    fn as_str(self) -> &'static str {
        match self {
            Self::Installed => "installed",
            Self::Portable => "portable",
        }
    }
}

#[derive(Clone, Debug)]
struct RuntimePaths {
    backend_executable: PathBuf,
    management_script: PathBuf,
    data: PathBuf,
    runtime: PathBuf,
    logs: PathBuf,
    state_file: PathBuf,
    registration_marker: PathBuf,
}

trait RuntimeAdapter: Send + Sync {
    fn ensure_running(&self, current: &RuntimeHealth) -> Result<(), String>;
    fn restart(&self, current: &RuntimeHealth) -> Result<(), String>;
    fn stop(&self, current: &RuntimeHealth) -> Result<(), String>;
    fn repair(&self, current: &RuntimeHealth) -> Result<(), String>;
}

struct ScheduledTaskAdapter {
    paths: RuntimePaths,
}

impl ScheduledTaskAdapter {
    fn registration_is_current(&self) -> bool {
        fs::read_to_string(&self.paths.registration_marker)
            .map(|version| version.trim() == CURRENT_VERSION)
            .unwrap_or(false)
    }

    fn install(&self) -> Result<(), String> {
        run_powershell(
            &self.paths.management_script,
            &[
                "-Action",
                "Install",
                "-TaskName",
                TASK_NAME,
                "-ServiceExecutable",
                &self.paths.backend_executable.to_string_lossy(),
                "-DataDirectory",
                &self.paths.data.to_string_lossy(),
                "-RuntimeDirectory",
                &self.paths.runtime.to_string_lossy(),
                "-LogDirectory",
                &self.paths.logs.to_string_lossy(),
                "-RuntimeMode",
                "installed",
                "-StartNow",
            ],
        )?;
        fs::write(&self.paths.registration_marker, CURRENT_VERSION.as_bytes())
            .map_err(|error| error.to_string())
    }

    fn run_action(&self, action: &str) -> Result<(), String> {
        run_powershell(
            &self.paths.management_script,
            &["-Action", action, "-TaskName", TASK_NAME],
        )
    }
}

impl RuntimeAdapter for ScheduledTaskAdapter {
    fn ensure_running(&self, current: &RuntimeHealth) -> Result<(), String> {
        if !self.registration_is_current() {
            self.install()
        } else if !current.ready {
            self.run_action("Start")
        } else {
            Ok(())
        }
    }

    fn restart(&self, _current: &RuntimeHealth) -> Result<(), String> {
        self.run_action("Restart")
    }

    fn stop(&self, _current: &RuntimeHealth) -> Result<(), String> {
        self.run_action("Stop")
    }

    fn repair(&self, _current: &RuntimeHealth) -> Result<(), String> {
        self.install()
    }
}

struct PortableProcessAdapter {
    paths: RuntimePaths,
}

impl PortableProcessAdapter {
    fn spawn(&self) -> Result<(), String> {
        let mut command = Command::new(&self.paths.backend_executable);
        command.args([
            "--service",
            "--data-dir",
            &self.paths.data.to_string_lossy(),
            "--runtime-dir",
            &self.paths.runtime.to_string_lossy(),
            "--log-dir",
            &self.paths.logs.to_string_lossy(),
            "--mode",
            "portable",
        ]);
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        detach_console(&mut command);
        command.spawn().map_err(|error| error.to_string())?;
        Ok(())
    }

    fn stop_process(&self, current: &RuntimeHealth) -> Result<(), String> {
        if !current.ready || current.pid == 0 {
            return Ok(());
        }
        let mut command = Command::new("taskkill.exe");
        command.args(["/PID", &current.pid.to_string(), "/T", "/F"]);
        hide_console(&mut command);
        let output = command.output().map_err(|error| error.to_string())?;
        if output.status.success() {
            Ok(())
        } else {
            Err(command_failure("停止便携版后台失败", &output))
        }
    }
}

impl RuntimeAdapter for PortableProcessAdapter {
    fn ensure_running(&self, current: &RuntimeHealth) -> Result<(), String> {
        if current.ready {
            Ok(())
        } else {
            self.spawn()
        }
    }

    fn restart(&self, current: &RuntimeHealth) -> Result<(), String> {
        self.stop_process(current)?;
        self.spawn()
    }

    fn stop(&self, current: &RuntimeHealth) -> Result<(), String> {
        self.stop_process(current)
    }

    fn repair(&self, current: &RuntimeHealth) -> Result<(), String> {
        self.restart(current)
    }
}

pub struct RuntimeManager {
    paths: RuntimePaths,
    mode: RuntimeMode,
    adapter: Box<dyn RuntimeAdapter>,
    operation: Mutex<()>,
    state: Mutex<DesktopState>,
}

impl RuntimeManager {
    pub fn new(app: &AppHandle) -> Result<Self, String> {
        let executable = std::env::current_exe().map_err(|error| error.to_string())?;
        let executable_dir = executable
            .parent()
            .ok_or_else(|| "无法确定桌面应用目录。".to_string())?
            .to_path_buf();
        let portable = cfg!(debug_assertions)
            || executable_dir.join("portable.flag").is_file()
            || std::env::var("LPC_DESKTOP_PORTABLE").as_deref() == Ok("1");
        let mode = if portable {
            RuntimeMode::Portable
        } else {
            RuntimeMode::Installed
        };

        let resource_dir = app
            .path()
            .resource_dir()
            .map_err(|error| error.to_string())?;
        let manifest_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let backend_executable = first_existing(&[
            resource_dir.join("backend").join("lpc-service.exe"),
            manifest_root
                .join("resources")
                .join("backend")
                .join("lpc-service.exe"),
        ])
        .unwrap_or_else(|| resource_dir.join("backend").join("lpc-service.exe"));
        let management_script = first_existing(&[
            resource_dir
                .join("scripts")
                .join("manage-system-startup.ps1"),
            manifest_root
                .join("resources")
                .join("scripts")
                .join("manage-system-startup.ps1"),
            manifest_root
                .parent()
                .and_then(Path::parent)
                .unwrap_or(&manifest_root)
                .join("scripts")
                .join("manage-system-startup.ps1"),
        ])
        .unwrap_or_else(|| {
            resource_dir
                .join("scripts")
                .join("manage-system-startup.ps1")
        });

        let home = if mode == RuntimeMode::Portable {
            executable_dir
        } else {
            let local_app_data = std::env::var_os("LOCALAPPDATA")
                .map(PathBuf::from)
                .ok_or_else(|| "Windows LOCALAPPDATA 不可用。".to_string())?;
            local_app_data.join("LocalProjectConsole")
        };
        let paths = RuntimePaths {
            backend_executable,
            management_script,
            data: home.join("data"),
            runtime: home.join("runtime"),
            logs: home.join("logs"),
            state_file: home.join("desktop-state.json"),
            registration_marker: home.join("desktop-runtime-registered"),
        };
        for directory in [&paths.data, &paths.runtime, &paths.logs] {
            fs::create_dir_all(directory).map_err(|error| error.to_string())?;
        }
        let state = read_state(&paths.state_file);
        let adapter: Box<dyn RuntimeAdapter> = match mode {
            RuntimeMode::Installed => Box::new(ScheduledTaskAdapter {
                paths: paths.clone(),
            }),
            RuntimeMode::Portable => Box::new(PortableProcessAdapter {
                paths: paths.clone(),
            }),
        };
        Ok(Self {
            paths,
            mode,
            adapter,
            operation: Mutex::new(()),
            state: Mutex::new(state),
        })
    }

    pub fn status(&self) -> RuntimeHealth {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_millis(180))
            .timeout_read(Duration::from_millis(300))
            .build();
        let response = match agent.get(HEALTH_URL).call() {
            Ok(response) => response,
            Err(_) => return RuntimeHealth::default(),
        };
        let health: RuntimeHealth = match response.into_json() {
            Ok(payload) => payload,
            Err(_) => return RuntimeHealth::default(),
        };
        if health.service != "local-project-console" {
            return RuntimeHealth::default();
        }
        health
    }

    pub fn ensure_running(&self) -> Result<RuntimeHealth, String> {
        let _operation = self
            .operation
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if !self.paths.backend_executable.is_file() {
            let current = self.status();
            if current.ready {
                return Ok(current);
            }
            return Err(format!(
                "后台程序不存在：{}。请先构建 Python runtime。",
                self.paths.backend_executable.display()
            ));
        }

        let current = self.status();
        self.adapter.ensure_running(&current)?;
        self.wait_for_health(Duration::from_secs(8))
    }

    pub fn restart(&self) -> Result<RuntimeHealth, String> {
        let _operation = self
            .operation
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let current = self.status();
        self.adapter.restart(&current)?;
        self.wait_for_health(Duration::from_secs(8))
    }

    pub fn stop(&self) -> Result<(), String> {
        let _operation = self
            .operation
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let current = self.status();
        self.adapter.stop(&current)
    }

    pub fn repair(&self) -> Result<RuntimeHealth, String> {
        let _operation = self
            .operation
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let current = self.status();
        self.adapter.repair(&current)?;
        self.wait_for_health(Duration::from_secs(8))
    }

    pub fn open_logs(&self) -> Result<(), String> {
        fs::create_dir_all(&self.paths.logs).map_err(|error| error.to_string())?;
        open::that(&self.paths.logs).map_err(|error| error.to_string())
    }

    pub fn import_legacy_data(&self) -> Result<ImportResult, String> {
        let source = match rfd::FileDialog::new()
            .set_title("选择 Local Project Console 源码目录")
            .pick_folder()
        {
            Some(path) => path,
            None => {
                return Ok(ImportResult {
                    cancelled: true,
                    source: String::new(),
                })
            }
        };
        let output = self.run_backend(&[
            "--migrate-from",
            &source.to_string_lossy(),
            "--data-dir",
            &self.paths.data.to_string_lossy(),
            "--runtime-dir",
            &self.paths.runtime.to_string_lossy(),
            "--log-dir",
            &self.paths.logs.to_string_lossy(),
            "--mode",
            self.mode.as_str(),
        ])?;
        if !output.status.success() {
            return Err(command_failure("导入旧数据失败", &output));
        }
        Ok(ImportResult {
            cancelled: false,
            source: source.display().to_string(),
        })
    }

    pub fn last_route(&self) -> String {
        let state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        normalized_route(&state.last_route)
    }

    pub fn remember_route(&self, route: &str) {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        state.last_route = normalized_route(route);
        let _ = write_state(&self.paths.state_file, &state);
    }

    pub fn check_for_update_if_due(&self) -> Option<ReleaseInfo> {
        let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
        {
            let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
            if now.saturating_sub(state.last_update_check) < 24 * 60 * 60 {
                return None;
            }
            state.last_update_check = now;
            let _ = write_state(&self.paths.state_file, &state);
        }

        let agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(2))
            .timeout_read(Duration::from_secs(3))
            .build();
        let release: GithubRelease = agent
            .get(RELEASE_API)
            .set("Accept", "application/vnd.github+json")
            .set("User-Agent", "LocalProjectConsole/0.1.0")
            .call()
            .ok()?
            .into_json()
            .ok()?;
        let latest_text = release.tag_name.trim_start_matches('v');
        let current = semver::Version::parse(CURRENT_VERSION).ok()?;
        let latest = semver::Version::parse(latest_text).ok()?;
        if latest <= current {
            return None;
        }
        Some(ReleaseInfo {
            version: latest.to_string(),
        })
    }

    fn run_backend(&self, arguments: &[&str]) -> Result<std::process::Output, String> {
        let mut command = Command::new(&self.paths.backend_executable);
        command.args(arguments);
        hide_console(&mut command);
        command.output().map_err(|error| error.to_string())
    }

    fn wait_for_health(&self, timeout: Duration) -> Result<RuntimeHealth, String> {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            let health = self.status();
            if health.ready {
                return Ok(health);
            }
            thread::sleep(Duration::from_millis(100));
        }
        Err("后台服务在 8 秒内没有响应。请打开日志查看启动错误。".to_string())
    }
}

fn run_powershell(script: &Path, arguments: &[&str]) -> Result<(), String> {
    if !script.is_file() {
        return Err(format!("后台管理脚本不存在：{}", script.display()));
    }
    let mut command = Command::new("powershell.exe");
    command.args([
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
    ]);
    command.arg(script).args(arguments);
    hide_console(&mut command);
    let output = command.output().map_err(|error| error.to_string())?;
    if output.status.success() {
        Ok(())
    } else {
        Err(command_failure("后台管理命令失败", &output))
    }
}

fn first_existing(candidates: &[PathBuf]) -> Option<PathBuf> {
    candidates.iter().find(|path| path.is_file()).cloned()
}

fn normalized_route(route: &str) -> String {
    match route.trim() {
        "/watchdog" | "/watchdog/" => "/watchdog".to_string(),
        "/remote" | "/remote/" => "/remote".to_string(),
        _ => "/".to_string(),
    }
}

fn read_state(path: &Path) -> DesktopState {
    fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_default()
}

fn write_state(path: &Path, state: &DesktopState) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    let temporary = path.with_extension("tmp");
    fs::write(
        &temporary,
        serde_json::to_vec_pretty(state).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    fs::rename(temporary, path).map_err(|error| error.to_string())
}

fn command_failure(label: &str, output: &std::process::Output) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let detail = if !stderr.is_empty() { stderr } else { stdout };
    if detail.is_empty() {
        format!("{}，退出码 {:?}。", label, output.status.code())
    } else {
        format!("{}：{}", label, detail)
    }
}

#[cfg(windows)]
fn hide_console(command: &mut Command) {
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_console(_command: &mut Command) {}

#[cfg(windows)]
fn detach_console(command: &mut Command) {
    command.creation_flags(CREATE_NO_WINDOW | DETACHED_PROCESS);
}

#[cfg(not(windows))]
fn detach_console(_command: &mut Command) {}
