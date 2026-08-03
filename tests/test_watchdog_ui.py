import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WatchdogUiTests(unittest.TestCase):
    def test_both_workspaces_support_persistent_system_aware_theming(self) -> None:
        for filename in ("index.html", "watchdog.html"):
            with self.subTest(filename=filename):
                html = (ROOT / filename).read_text(encoding="utf-8")

                self.assertEqual(html.count('id="theme-toggle"'), 1)
                self.assertIn(':root[data-theme="dark"]', html)
                self.assertIn("prefers-color-scheme: dark", html)
                self.assertIn("localhost-project-console.theme", html)
                self.assertIn("localStorage.setItem(key, next)", html)
                self.assertIn("window.addEventListener('storage'", html)
                self.assertIn('aria-label="切换至深色模式"', html)
                self.assertIn('title="切换至深色模式"', html)
                self.assertIn('aria-pressed="false"', html)
                self.assertLess(
                    html.index("document.documentElement.dataset.theme"),
                    html.index("<style>"),
                    "theme must resolve before styles are parsed to avoid a light-mode flash",
                )

    def test_project_page_adds_only_one_watchdog_navigation_link(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")

        self.assertEqual(html.count('href="/watchdog"'), 1)
        self.assertNotIn("执行记录", html)
        self.assertNotIn("添加监控渠道", html)

    def test_project_navigation_stays_expanded_across_both_workspaces(self) -> None:
        labels = ["全部项目", "运行中", "已关闭", "待配置", "网页入口"]

        for filename in ("index.html", "watchdog.html"):
            with self.subTest(filename=filename):
                html = (ROOT / filename).read_text(encoding="utf-8")
                self.assertIn('class="sidebar-summary"', html)
                self.assertIn('id="project-filter-list"', html)
                self.assertIn('aria-label="项目查看范围"', html)
                for label in labels:
                    self.assertIn(label, html)

        watchdog_html = (ROOT / "watchdog.html").read_text(encoding="utf-8")
        self.assertIn('href="/watchdog" aria-current="page"', watchdog_html)
        self.assertIn('href="/?filter=running"', watchdog_html)

    def test_watchdog_page_has_three_horizontal_tabs(self) -> None:
        html = (ROOT / "watchdog.html").read_text(encoding="utf-8")
        labels = ["监控会话", "执行记录", "添加监控渠道"]
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
