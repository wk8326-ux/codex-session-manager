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
        self.assertIn('id="session-select"', html)
        self.assertIn('id="manage-synced-sessions"', html)
        self.assertIn('id="sync-session-dialog"', html)
        self.assertIn('id="local-session-select"', html)
        self.assertIn('id="synced-session-list"', html)
        self.assertIn('id="runtime-strip"', html)
        self.assertIn('id="runtime-activity"', html)
        self.assertIn('id="runtime-metrics"', html)
        self.assertIn('id="jump-latest"', html)
        self.assertNotIn('id="session-list"', html)
        self.assertNotIn("只显示会话监控中登记的任务", html)
        self.assertLess(html.index('id="create-pairing"'), html.index('id="device-list"'))

    def test_sync_manager_uses_the_independent_remote_catalog_api(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        self.assertIn("/api/remote/local-sessions?limit=50", script)
        self.assertIn("/api/remote/synced-sessions", script)
        self.assertIn("method: 'DELETE'", script)

    def test_transcript_is_a_fixed_scrollable_latest_message_viewport(self) -> None:
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        self.assertIn("height: clamp(", stylesheet)
        self.assertIn("overflow-y: auto", stylesheet)
        self.assertIn("ACTIVE_REFRESH_MS = 1200", script)
        self.assertIn("IDLE_REFRESH_MS = 5000", script)
        self.assertIn("document.visibilityState", script)
        self.assertIn("followTail", script)
        self.assertIn("scrollToLatest", script)
        self.assertIn("aria-busy", (ROOT / "remote.html").read_text(encoding="utf-8"))

    def test_runtime_activity_uses_structured_tool_and_reasoning_rows(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        self.assertIn("reasoningNode", script)
        self.assertIn("activityNode", script)
        self.assertIn("conversationActivity", script)
        self.assertIn("stream-tail", script)
        self.assertIn("runtime-strip running", script)
        self.assertIn("runtime-rotor", stylesheet)
        self.assertIn("status-badge.running::before", stylesheet)

    def test_conversation_text_uses_safe_readable_markdown_structure(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        self.assertIn("appendRichText", script)
        self.assertIn("appendInlineMarkup", script)
        self.assertIn("message-heading", script)
        self.assertIn("message-code", script)
        self.assertIn("rich-text", stylesheet)
        self.assertIn("max-width: 76ch", stylesheet)

    def test_service_worker_never_caches_api_responses(self) -> None:
        script = (ROOT / "service-worker.js").read_text(encoding="utf-8")

        self.assertIn("url.pathname.startsWith('/api/')", script)
        self.assertNotIn("/api/remote", (ROOT / "manifest.webmanifest").read_text(encoding="utf-8"))

    def test_browser_renders_conversation_text_without_html_injection(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        self.assertIn("element.textContent =", script)
        self.assertIn("code.textContent =", script)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()
