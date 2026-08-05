from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RemoteUiContractTests(unittest.TestCase):
    def test_remote_page_has_stable_workspaces_and_mobile_controls(self) -> None:
        html = (ROOT / "remote.html").read_text(encoding="utf-8")

        for label in ("项目控制台", "会话监控", "远程会话"):
            self.assertIn(label, html)
        self.assertIn('id="composer"', html)
        self.assertIn('class="mobile-nav"', html)
        self.assertIn('id="create-pairing"', html)
        self.assertIn('id="desktop-comparison-code"', html)

    def test_service_worker_never_caches_api_responses(self) -> None:
        script = (ROOT / "service-worker.js").read_text(encoding="utf-8")

        self.assertIn("url.pathname.startsWith('/api/')", script)
        self.assertNotIn("/api/remote", (ROOT / "manifest.webmanifest").read_text(encoding="utf-8"))

    def test_browser_renders_conversation_text_without_html_injection(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        self.assertIn("body.textContent = text", script)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()
