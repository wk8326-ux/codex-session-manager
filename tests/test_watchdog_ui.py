import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WatchdogUiTests(unittest.TestCase):
    def test_all_workspaces_reuse_one_persistent_system_aware_theme_control(self) -> None:
        sidebar_script = (ROOT / "assets" / "console-sidebar.js").read_text(encoding="utf-8")
        for filename in ("index.html", "watchdog.html"):
            with self.subTest(filename=filename):
                html = (ROOT / filename).read_text(encoding="utf-8")

                self.assertNotIn('id="theme-toggle"', html)
                self.assertEqual(html.count('src="/assets/console-sidebar.js?v=9"'), 1)
                self.assertIn("prefers-color-scheme: dark", html)
                self.assertIn("localhost-project-console.theme", html)
                self.assertLess(
                    html.index("document.documentElement.dataset.theme"),
                    html.index("</head>"),
                    "theme must resolve before styles are parsed to avoid a light-mode flash",
                )

        remote_html = (ROOT / "remote.html").read_text(encoding="utf-8")
        remote_script = (ROOT / "remote.js").read_text(encoding="utf-8")
        self.assertNotIn('src="/assets/console-sidebar.js?v=9"', remote_html)
        self.assertIn('id="remote-theme-toggle"', remote_html)
        self.assertIn("localhost-project-console.theme", remote_script)

        self.assertIn("localStorage.setItem(THEME_KEY, next)", sidebar_script)
        self.assertIn("addEventListener('storage'", sidebar_script)
        self.assertIn('aria-label="切换至深色模式"', sidebar_script)
        self.assertIn('aria-pressed="false"', sidebar_script)

    def test_project_page_adds_only_one_watchdog_navigation_link(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        sidebar_script = (ROOT / "assets" / "console-sidebar.js").read_text(encoding="utf-8")

        self.assertEqual(html.count('href="/watchdog"'), 0)
        self.assertEqual(
            sidebar_script.count("navLink('watchdog', '/watchdog'"), 1
        )
        self.assertNotIn("执行记录", html)
        self.assertNotIn("添加监控渠道", html)

    def test_sidebar_navigation_has_only_three_primary_entries(self) -> None:
        removed_labels = ["查看范围", "全部项目", "工作区", "自动化工具"]
        sidebar_script = (ROOT / "assets" / "console-sidebar.js").read_text(encoding="utf-8")
        expected_active = {
            "index.html": "projects",
            "watchdog.html": "watchdog",
        }

        for filename, active in expected_active.items():
            with self.subTest(filename=filename):
                html = (ROOT / filename).read_text(encoding="utf-8")
                self.assertIn(
                    f'<aside class="sidebar" data-console-sidebar data-active="{active}"',
                    html,
                )
                self.assertEqual(html.count('data-console-sidebar'), 1)
                for label in removed_labels:
                    self.assertNotIn(f">{label}<", sidebar_script)

        for route in (
            "navLink('projects', '/', 'layout', '项目控制台')",
            "navLink('watchdog', '/watchdog', 'activity', '会话监控')",
            "navLink('remote', '/remote', 'globe', '远程会话')",
        ):
            self.assertEqual(sidebar_script.count(route), 1)

        remote_html = (ROOT / "remote.html").read_text(encoding="utf-8")
        self.assertNotIn("data-console-sidebar", remote_html)
        self.assertIn('href="/"', remote_html)

    def test_shared_sidebar_owns_all_fixed_layout_and_copy(self) -> None:
        sidebar_script = (ROOT / "assets" / "console-sidebar.js").read_text(encoding="utf-8")
        sidebar_css = (ROOT / "assets" / "console-sidebar.css").read_text(encoding="utf-8")
        remote_stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")
        server = (ROOT / "app.py").read_text(encoding="utf-8")

        for text in (
            "本地项目控制台",
            "LOCALHOST / 8765",
            "本地服务运行中",
            "控制台在线",
            "127.0.0.1:8765",
            "每 5 秒同步一次状态",
        ):
            self.assertIn(text, sidebar_script)
        self.assertIn(".sidebar[data-console-sidebar]", sidebar_css)
        self.assertIn("/api/shell/project-summary", sidebar_script)
        self.assertIn("setInterval(refreshProjectSummary, 5000)", sidebar_script)
        self.assertIn("sessionStorage.setItem(SUMMARY_KEY", sidebar_script)
        self.assertIn("top: 50%", sidebar_css)
        self.assertIn("place-items: center", sidebar_css)
        self.assertIn("translateX(22px)", sidebar_css)
        self.assertNotIn(".theme-switch span", remote_stylesheet)
        self.assertIn("grid-template-rows: auto auto auto 1fr auto auto", sidebar_css)
        self.assertIn("font-size: 13px", sidebar_css)
        self.assertEqual(server.count('"/assets/console-sidebar.css"'), 2)
        self.assertEqual(server.count('"/assets/console-sidebar.js"'), 2)

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
        self.assertIn('aria-current="page"', html)
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


if __name__ == "__main__":
    unittest.main()
