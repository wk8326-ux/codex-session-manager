mod runtime;

use runtime::{BootstrapResult, ImportResult, RuntimeHealth, RuntimeManager};
use std::time::Duration;
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, State, Theme, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_notification::NotificationExt;
use url::Url;

const CONSOLE_ORIGIN: &str = "http://127.0.0.1:8765";
const RELEASES_URL: &str = "https://github.com/wk8326-ux/localhost-project-console/releases/latest";
const EXTERNAL_NAVIGATION_SHIM: &str = r#"
(() => {
  const navigateInPlace = (value) => {
    if (value === undefined || value === null || value === '') return null;
    window.location.assign(new URL(String(value), window.location.href).href);
    return null;
  };
  window.open = (url) => navigateInPlace(url);
  document.addEventListener('click', (event) => {
    const origin = event.target;
    const anchor = origin instanceof Element
      ? origin.closest('a[target="_blank"]')
      : null;
    if (!anchor || !anchor.href) return;
    event.preventDefault();
    navigateInPlace(anchor.href);
  }, true);
})();
"#;

#[tauri::command]
fn runtime_status(manager: State<'_, RuntimeManager>) -> RuntimeHealth {
    manager.status()
}

#[tauri::command]
fn ensure_runtime(manager: State<'_, RuntimeManager>) -> Result<BootstrapResult, String> {
    let health = manager.ensure_running()?;
    Ok(BootstrapResult {
        health,
        last_route: manager.last_route(),
    })
}

#[tauri::command]
fn open_logs(manager: State<'_, RuntimeManager>) -> Result<(), String> {
    manager.open_logs()
}

#[tauri::command]
fn repair_runtime(manager: State<'_, RuntimeManager>) -> Result<BootstrapResult, String> {
    let health = manager.repair()?;
    Ok(BootstrapResult {
        health,
        last_route: manager.last_route(),
    })
}

#[tauri::command]
fn import_legacy_data(manager: State<'_, RuntimeManager>) -> Result<ImportResult, String> {
    manager.import_legacy_data()
}

fn show_route(app: &AppHandle, route: &str) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let target = format!("{CONSOLE_ORIGIN}{route}");
    if let Ok(url) = Url::parse(&target) {
        let _ = window.navigate(url);
    }
    let _ = window.show();
    let _ = window.unminimize();
    let _ = window.set_focus();
}

fn show_main_window(app: &AppHandle) {
    let route = app.state::<RuntimeManager>().last_route();
    show_route(app, &route);
}

fn is_bundled_app_url(url: &Url) -> bool {
    url.scheme() == "tauri" || (url.scheme() == "http" && url.host_str() == Some("tauri.localhost"))
}

fn is_console_url(url: &Url) -> bool {
    url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port_or_known_default() == Some(8765)
}

fn create_main_window(app: &tauri::App) -> tauri::Result<()> {
    let navigation_app = app.handle().clone();
    WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("本地项目控制台")
        .theme(Some(Theme::Dark))
        .inner_size(1280.0, 820.0)
        .min_inner_size(1024.0, 680.0)
        .center()
        .initialization_script(EXTERNAL_NAVIGATION_SHIM)
        .on_navigation(move |url| {
            // Tauri 2 serves bundled assets from http://tauri.localhost.
            // Keep this origin inside the WebView; opening it in the system
            // browser produces a misleading ERR_CONNECTION_REFUSED page.
            if is_bundled_app_url(url) {
                return true;
            }
            if is_console_url(url) {
                navigation_app
                    .state::<RuntimeManager>()
                    .remember_route(url.path());
                true
            } else {
                let _ = open::that(url.as_str());
                false
            }
        })
        .build()?;
    Ok(())
}

fn create_tray(app: &tauri::App) -> tauri::Result<()> {
    let project = MenuItem::with_id(app, "project", "打开项目控制台", true, None::<&str>)?;
    let session_manager = MenuItem::with_id(app, "session-manager", "Codex 会话管理", true, None::<&str>)?;
    let status = MenuItem::with_id(app, "status", "后台状态：检测中", false, None::<&str>)?;
    let restart = MenuItem::with_id(app, "restart", "重启后台服务", true, None::<&str>)?;
    let stop = MenuItem::with_id(app, "stop", "停止后台服务", true, None::<&str>)?;
    let releases = MenuItem::with_id(app, "releases", "检查更新", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出桌面应用", true, None::<&str>)?;
    let separator_a = PredefinedMenuItem::separator(app)?;
    let separator_b = PredefinedMenuItem::separator(app)?;
    let separator_c = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(
        app,
        &[
            &project,
            &session_manager,
            &separator_a,
            &status,
            &restart,
            &stop,
            &separator_b,
            &releases,
            &separator_c,
            &quit,
        ],
    )?;

    TrayIconBuilder::new()
        .icon(
            app.default_window_icon()
                .cloned()
                .expect("the desktop icon must be bundled"),
        )
        .tooltip("本地项目控制台")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "project" => show_route(app, "/"),
            "session-manager" => show_route(app, "/session-manager"),
            "restart" => {
                let app = app.clone();
                std::thread::spawn(move || {
                    let _ = app.state::<RuntimeManager>().restart();
                    show_main_window(&app);
                });
            }
            "stop" => {
                let confirmed = rfd::MessageDialog::new()
                    .set_title("停止后台服务")
                    .set_description("这会停止项目控制台核心，但不会自动关闭独立的 Codex 会话管理。确定继续吗？")
                    .set_buttons(rfd::MessageButtons::YesNo)
                    .set_level(rfd::MessageLevel::Warning)
                    .show();
                if confirmed == rfd::MessageDialogResult::Yes {
                    let _ = app.state::<RuntimeManager>().stop();
                }
            }
            "releases" => {
                let _ = open::that(RELEASES_URL);
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        })
        .build(app)?;

    let status_app = app.handle().clone();
    std::thread::spawn(move || loop {
        let health = status_app.state::<RuntimeManager>().status();
        let label = if health.ready {
            if health.auxiliary_ready {
                format!("后台状态：运行中 · {}", health.version)
            } else {
                "后台状态：核心已就绪 · 辅助启动中".to_string()
            }
        } else {
            "后台状态：未连接".to_string()
        };
        let item = status.clone();
        let _ = status_app.run_on_main_thread(move || {
            let _ = item.set_text(label);
        });
        std::thread::sleep(Duration::from_secs(5));
    });
    Ok(())
}

fn start_update_check(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(2));
        let Some(release) = app.state::<RuntimeManager>().check_for_update_if_due() else {
            return;
        };
        let _ = app
            .notification()
            .builder()
            .title("Local Project Console 有新版本")
            .body(format!(
                "版本 {} 已发布，可通过托盘的“检查更新”打开下载页面。",
                release.version
            ))
            .show();
    });
}

fn start_runtime_bootstrap(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        let _ = app.state::<RuntimeManager>().ensure_running();
    });
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(
            |app, _arguments, _cwd| {
                show_main_window(app);
            },
        ))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .invoke_handler(tauri::generate_handler![
            runtime_status,
            ensure_runtime,
            open_logs,
            repair_runtime,
            import_legacy_data
        ])
        .setup(|app| {
            let manager = RuntimeManager::new(app.handle())?;
            app.manage(manager);
            start_runtime_bootstrap(app.handle());
            create_main_window(app)?;
            create_tray(app)?;
            start_update_check(app.handle());
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("Local Project Console desktop runtime failed");
}

#[cfg(test)]
mod tests {
    use super::{is_bundled_app_url, is_console_url};
    use url::Url;

    #[test]
    fn keeps_tauri_two_bundled_origin_inside_webview() {
        let bundled = Url::parse("http://tauri.localhost/index.html").unwrap();

        assert!(is_bundled_app_url(&bundled));
    }

    #[test]
    fn keeps_only_console_origin_inside_webview() {
        let console = Url::parse("http://127.0.0.1:8765/watchdog").unwrap();
        let wrong_port = Url::parse("http://127.0.0.1:8766/remote").unwrap();
        let external = Url::parse("https://github.com/wk8326-ux").unwrap();

        assert!(is_console_url(&console));
        assert!(!is_console_url(&wrong_port));
        assert!(!is_console_url(&external));
    }
}
