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
const ADMIN_PORT: u16 = 8765;
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
    pub role: String,
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
    pub migrated: bool,
    pub source: String,
    pub copied: Vec<String>,
    pub reason: String,
    pub backup: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct MigrationPayload {
    migrated: bool,
    #[serde(default)]
    copied: Vec<String>,
    #[serde(default)]
    reason: String,
    #[serde(default)]
    backup: String,
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

#[derive(Debug, PartialEq, Eq)]
enum ScheduledStartAction {
    None,
    Install,
    Start,
}

fn scheduled_start_action(
    ready: bool,
    registration_is_current: impl FnOnce() -> bool,
) -> ScheduledStartAction {
    if !registration_is_current() {
        ScheduledStartAction::Install
    } else if ready {
        ScheduledStartAction::None
    } else {
        ScheduledStartAction::Start
    }
}

fn runtime_health_is_compatible(health: &RuntimeHealth, mode: RuntimeMode) -> bool {
    health.service == "local-project-console"
        && health.ready
        && (health.role.is_empty() || health.role == "core")
        && health.version == CURRENT_VERSION
        && health.mode == mode.as_str()
        && health.pid > 0
        && health.ports.admin == ADMIN_PORT
}

struct ScheduledTaskAdapter {
    paths: RuntimePaths,
}

impl ScheduledTaskAdapter {
    fn registration_is_current(&self) -> bool {
        let marker_matches = fs::read_to_string(&self.paths.registration_marker)
            .map(|version| version.trim() == CURRENT_VERSION)
            .unwrap_or(false);
        marker_matches
            && run_powershell(
                &self.paths.management_script,
                &[
                    "-Action",
                    "Validate",
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
                    "-ExpectedVersion",
                    CURRENT_VERSION,
                ],
            )
            .is_ok()
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
                "-ExpectedVersion",
                CURRENT_VERSION,
                "-StartNow",
            ],
        )?;
        fs::write(&self.paths.registration_marker, CURRENT_VERSION.as_bytes())
            .map_err(|error| error.to_string())
    }

    fn run_action(&self, action: &str) -> Result<(), String> {
        run_powershell(
            &self.paths.management_script,
            &[
                "-Action",
                action,
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
                "-ExpectedVersion",
                CURRENT_VERSION,
            ],
        )
    }
}

impl RuntimeAdapter for ScheduledTaskAdapter {
    fn ensure_running(&self, current: &RuntimeHealth) -> Result<(), String> {
        match scheduled_start_action(current.ready, || self.registration_is_current()) {
            ScheduledStartAction::None => Ok(()),
            ScheduledStartAction::Install => self.install(),
            ScheduledStartAction::Start => self.run_action("Start"),
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
            "--core",
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
        if current.pid == 0 {
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
        } else if current.pid > 0 {
            self.restart(current)
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
        let mut managed = current.clone();
        managed.ready = runtime_health_is_compatible(&current, self.mode);
        self.adapter.ensure_running(&managed)?;
        self.wait_for_health(self.startup_timeout())
    }

    pub fn restart(&self) -> Result<RuntimeHealth, String> {
        let _operation = self
            .operation
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let current = self.status();
        self.adapter.restart(&current)?;
        self.wait_for_health(self.startup_timeout())
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
        self.wait_for_health(self.startup_timeout())
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
                    migrated: false,
                    source: String::new(),
                    copied: Vec::new(),
                    reason: "cancelled".to_string(),
                    backup: String::new(),
                })
            }
        };
        let replacing_existing = self.paths.data.join("projects.json").is_file()
            || self.paths.data.join("watchdog.db").is_file();
        if replacing_existing
            && rfd::MessageDialog::new()
                .set_title("导入源码版数据")
                .set_description(
                    "当前安装目录已有数据。继续后会先创建完整备份，再用所选源码版数据替换。",
                )
                .set_buttons(rfd::MessageButtons::YesNo)
                .set_level(rfd::MessageLevel::Warning)
                .show()
                != rfd::MessageDialogResult::Yes
        {
            return Ok(ImportResult {
                cancelled: true,
                migrated: false,
                source: source.display().to_string(),
                copied: Vec::new(),
                reason: "cancelled".to_string(),
                backup: String::new(),
            });
        }
        self.import_legacy_from(&source)
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
            let state = self.state.lock().unwrap_or_else(|error| error.into_inner());
            if !update_check_is_due(state.last_update_check, now) {
                return None;
            }
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
        {
            let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
            state.last_update_check = now;
            let _ = write_state(&self.paths.state_file, &state);
        }
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
        command_output_with_timeout(command, Duration::from_secs(120), "后台数据迁移")
    }

    fn import_legacy_from(&self, source: &Path) -> Result<ImportResult, String> {
        let _operation = self
            .operation
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let current = self.status();
        let was_running = current.ready;
        let payload = with_runtime_paused(
            was_running,
            || {
                self.adapter.stop(&current)?;
                self.wait_for_stopped(Duration::from_secs(10))
            },
            || {
                self.run_backend(&[
                    "--migrate-from",
                    &source.to_string_lossy(),
                    "--replace-existing",
                    "--data-dir",
                    &self.paths.data.to_string_lossy(),
                    "--runtime-dir",
                    &self.paths.runtime.to_string_lossy(),
                    "--log-dir",
                    &self.paths.logs.to_string_lossy(),
                    "--mode",
                    self.mode.as_str(),
                ])
                .and_then(parse_migration_output)
            },
            || {
                self.adapter
                    .ensure_running(&RuntimeHealth::default())
                    .and_then(|()| self.wait_for_health(self.startup_timeout()).map(|_| ()))
            },
        )?;
        Ok(ImportResult {
            cancelled: false,
            migrated: payload.migrated,
            source: source.display().to_string(),
            copied: payload.copied,
            reason: payload.reason,
            backup: payload.backup,
        })
    }

    fn startup_timeout(&self) -> Duration {
        startup_timeout_for(self.mode)
    }

    fn wait_for_health(&self, timeout: Duration) -> Result<RuntimeHealth, String> {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            let health = self.status();
            if runtime_health_is_compatible(&health, self.mode) {
                return Ok(health);
            }
            thread::sleep(Duration::from_millis(100));
        }
        Err(format!(
            "后台服务在 {} 秒内没有响应。请打开日志查看启动错误。",
            timeout.as_secs()
        ))
    }

    fn wait_for_stopped(&self, timeout: Duration) -> Result<(), String> {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if !self.status().ready {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(100));
        }
        Err("后台服务未能停止，旧数据未被修改。".to_string())
    }
}

fn parse_migration_output(output: std::process::Output) -> Result<MigrationPayload, String> {
    if !output.status.success() {
        return Err(command_failure("导入旧数据失败", &output));
    }
    parse_migration_payload(&output.stdout)
}

fn parse_migration_payload(payload: &[u8]) -> Result<MigrationPayload, String> {
    serde_json::from_slice(payload).map_err(|error| format!("后台返回了无效的迁移结果：{error}"))
}

fn startup_timeout_for(mode: RuntimeMode) -> Duration {
    match mode {
        RuntimeMode::Installed => Duration::from_secs(30),
        RuntimeMode::Portable => Duration::from_secs(30),
    }
}

fn update_check_is_due(last_check: u64, now: u64) -> bool {
    now.saturating_sub(last_check) >= 24 * 60 * 60
}

fn with_runtime_paused<T>(
    was_running: bool,
    stop: impl FnOnce() -> Result<(), String>,
    operation: impl FnOnce() -> Result<T, String>,
    restore: impl FnOnce() -> Result<(), String>,
) -> Result<T, String> {
    if was_running {
        stop()?;
    }
    let result = operation();
    let restored = if was_running { restore() } else { Ok(()) };
    match (result, restored) {
        (Ok(value), Ok(())) => Ok(value),
        (Err(error), Ok(())) => Err(error),
        (Ok(_), Err(error)) => Err(format!("旧数据处理完成，但后台恢复失败：{error}")),
        (Err(operation_error), Err(restore_error)) => Err(format!(
            "{operation_error}；同时后台恢复失败：{restore_error}"
        )),
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
    let output = command_output_with_timeout(command, Duration::from_secs(180), "后台管理命令")?;
    if output.status.success() {
        Ok(())
    } else {
        Err(command_failure("后台管理命令失败", &output))
    }
}

fn command_output_with_timeout(
    mut command: Command,
    timeout: Duration,
    label: &str,
) -> Result<std::process::Output, String> {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn().map_err(|error| error.to_string())?;
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return child.wait_with_output().map_err(|error| error.to_string()),
            Ok(None) if Instant::now() < deadline => {
                thread::sleep(Duration::from_millis(25));
            }
            Ok(None) => {
                terminate_process_tree(&mut child);
                return Err(format!("{label}在 {} 秒内没有结束。", timeout.as_secs()));
            }
            Err(error) => return Err(error.to_string()),
        }
    }
}

#[cfg(windows)]
fn terminate_process_tree(child: &mut std::process::Child) {
    let mut command = Command::new("taskkill.exe");
    command
        .args(["/PID", &child.id().to_string(), "/T", "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    hide_console(&mut command);
    let terminated = command
        .status()
        .map(|status| status.success())
        .unwrap_or(false);
    if !terminated {
        let _ = child.kill();
    }
    let _ = child.wait();
}

#[cfg(not(windows))]
fn terminate_process_tree(child: &mut std::process::Child) {
    let _ = child.kill();
    let _ = child.wait();
}

fn first_existing(candidates: &[PathBuf]) -> Option<PathBuf> {
    candidates.iter().find(|path| path.is_file()).cloned()
}

fn normalized_route(route: &str) -> String {
    match route.trim() {
        "/session-manager" | "/session-manager/" => "/session-manager".to_string(),
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

#[cfg(test)]
mod tests {
    use super::{
        parse_migration_payload, runtime_health_is_compatible, startup_timeout_for,
        update_check_is_due, RuntimeHealth, RuntimeMode, RuntimePorts,
    };
    use super::{scheduled_start_action, with_runtime_paused, ScheduledStartAction};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    #[test]
    fn migration_payload_preserves_skip_and_backup_metadata() {
        let payload = parse_migration_payload(
            br#"{"migrated":true,"copied":["projects.json"],"reason":"migrated","backup":"D:/data.pre-import"}"#,
        )
        .unwrap();

        assert!(payload.migrated);
        assert_eq!(payload.copied, ["projects.json"]);
        assert_eq!(payload.reason, "migrated");
        assert_eq!(payload.backup, "D:/data.pre-import");
    }

    #[test]
    fn malformed_migration_payload_is_rejected() {
        assert!(parse_migration_payload(b"not-json").is_err());
    }

    #[test]
    fn packaged_runtimes_allow_for_defender_cold_start_scanning() {
        assert_eq!(
            startup_timeout_for(RuntimeMode::Installed),
            Duration::from_secs(30)
        );
        assert_eq!(
            startup_timeout_for(RuntimeMode::Portable),
            Duration::from_secs(30)
        );
    }

    #[test]
    fn failed_update_checks_remain_due_until_a_success_is_recorded() {
        let now = 200_000;
        assert!(update_check_is_due(0, now));
        assert!(!update_check_is_due(now - 60, now));
        assert!(update_check_is_due(now - 86_400, now));
    }

    #[test]
    fn healthy_runtime_still_validates_scheduled_task_registration() {
        let validation_called = Arc::new(Mutex::new(false));
        let observed = validation_called.clone();

        let action = scheduled_start_action(true, move || {
            *observed.lock().unwrap() = true;
            true
        });

        assert_eq!(action, ScheduledStartAction::None);
        assert!(*validation_called.lock().unwrap());
        assert_eq!(
            scheduled_start_action(true, || false),
            ScheduledStartAction::Install
        );
        assert_eq!(
            scheduled_start_action(false, || false),
            ScheduledStartAction::Install
        );
        assert_eq!(
            scheduled_start_action(false, || true),
            ScheduledStartAction::Start
        );
    }

    #[test]
    fn runtime_health_requires_the_expected_mode_version_and_ports() {
        let health = RuntimeHealth {
            service: "local-project-console".to_string(),
            version: super::CURRENT_VERSION.to_string(),
            ready: true,
            mode: "installed".to_string(),
            pid: 42,
            ports: RuntimePorts {
                admin: 8765,
                remote: 8766,
                auxiliary: 8767,
            },
            ..RuntimeHealth::default()
        };

        assert!(runtime_health_is_compatible(
            &health,
            RuntimeMode::Installed
        ));
        assert!(!runtime_health_is_compatible(
            &health,
            RuntimeMode::Portable
        ));

        let mut foreign = health.clone();
        foreign.service = "another-local-service".to_string();
        assert!(!runtime_health_is_compatible(
            &foreign,
            RuntimeMode::Installed
        ));

        let mut stale = health;
        stale.version = "0.0.0".to_string();
        assert!(!runtime_health_is_compatible(
            &stale,
            RuntimeMode::Installed
        ));
    }

    #[test]
    fn migration_restores_runtime_after_success_or_failure() {
        for fail in [false, true] {
            let events = Arc::new(Mutex::new(Vec::new()));
            let stop_events = events.clone();
            let operation_events = events.clone();
            let restore_events = events.clone();
            let result = with_runtime_paused(
                true,
                move || {
                    stop_events.lock().unwrap().push("stop");
                    Ok(())
                },
                move || {
                    operation_events.lock().unwrap().push("migrate");
                    if fail {
                        Err("migration failed".to_string())
                    } else {
                        Ok("migrated")
                    }
                },
                move || {
                    restore_events.lock().unwrap().push("restore");
                    Ok(())
                },
            );

            assert_eq!(*events.lock().unwrap(), ["stop", "migrate", "restore"]);
            assert_eq!(result.is_err(), fail);
        }
    }
}
