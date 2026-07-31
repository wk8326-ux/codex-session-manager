import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WatchdogUiTests(unittest.TestCase):
    def test_project_page_adds_only_one_watchdog_navigation_link(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")

        self.assertEqual(html.count('href="/watchdog"'), 1)
        self.assertNotIn("执行记录", html)
        self.assertNotIn("添加监控渠道", html)

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


if __name__ == "__main__":
    unittest.main()
