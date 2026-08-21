import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WatchdogUiTests(unittest.TestCase):
    def test_admin_pages_share_session_manager_navigation(self) -> None:
        navigation = (ROOT / "assets" / "session-manager-nav.js").read_text(
            encoding="utf-8"
        )
        expected = {
            "session-manager.html": "overview",
            "watchdog.html": "monitor",
        }
        for filename, active in expected.items():
            with self.subTest(filename=filename):
                html = (ROOT / filename).read_text(encoding="utf-8")
                self.assertIn(
                    f'<aside class="sidebar" data-session-nav data-active="{active}"',
                    html,
                )
                self.assertEqual(html.count("data-session-nav"), 1)
                self.assertIn('/assets/session-manager-nav.js?v=1', html)

        for label in ("功能总览", "监控与续跑", "远程会话"):
            self.assertIn(label, navigation)
        self.assertNotIn("项目控制台", navigation)
        self.assertNotIn("工作区", navigation)

    def test_admin_theme_is_persistent_and_remote_pwa_stays_independent(self) -> None:
        navigation = (ROOT / "assets" / "session-manager-nav.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("codex-session-manager.theme", navigation)
        self.assertIn("localStorage.setItem(THEME_KEY, next)", navigation)

        remote_html = (ROOT / "remote.html").read_text(encoding="utf-8")
        remote_script = (ROOT / "remote.js").read_text(encoding="utf-8")
        self.assertNotIn("data-session-nav", remote_html)
        self.assertIn('id="remote-theme-toggle"', remote_html)
        self.assertIn("localhost-project-console.theme", remote_script)

    def test_watchdog_page_has_four_horizontal_tabs(self) -> None:
        html = (ROOT / "watchdog.html").read_text(encoding="utf-8")
        labels = ["监控会话", "执行记录", "添加监控渠道", "错误类型"]
        positions = [html.index(label) for label in labels]

        self.assertEqual(positions, sorted(positions))
        self.assertIn('class="watchdog-tabs"', html)
        self.assertIn('role="tablist"', html)

    def test_watchdog_shell_includes_accessible_and_responsive_states(self) -> None:
        html = (ROOT / "watchdog.html").read_text(encoding="utf-8")

        self.assertIn('href="#watchdog-main"', html)
        self.assertIn('prefers-reduced-motion: reduce', html)
        self.assertIn('@media (max-width: 820px)', html)
        self.assertIn('@media (max-width: 520px)', html)
        for state in ("loading", "empty", "unavailable", "disabled"):
            self.assertIn(f'data-view-state="{state}"', html)

    def test_recovery_controls_share_the_same_labeled_grid(self) -> None:
        html = (ROOT / "watchdog.html").read_text(encoding="utf-8")

        self.assertIn('class="dispatch-field resume-field"', html)
        self.assertIn(".dispatch-field > select,", html)
        self.assertIn(".dispatch-field > .resume-control", html)
        self.assertIn("所有已启用会话共用", html)

    def test_watchdog_ui_has_no_project_console_dependency(self) -> None:
        html = (ROOT / "watchdog.html").read_text(encoding="utf-8")

        self.assertNotIn("/api/projects", html)
        self.assertNotIn("/api/shell/project-summary", html)
        self.assertNotIn("projectSummary", html)


if __name__ == "__main__":
    unittest.main()
