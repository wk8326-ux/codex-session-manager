from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RemoteUiContractTests(unittest.TestCase):
    def test_remote_page_has_stable_workspaces_and_mobile_controls(self) -> None:
        html = (ROOT / "remote.html").read_text(encoding="utf-8")
        sidebar = (ROOT / "assets" / "console-sidebar.js").read_text(encoding="utf-8")

        for label in ("项目控制台", "会话监控", "远程会话"):
            self.assertIn(label, sidebar)
        self.assertIn('data-console-sidebar data-active="remote"', html)
        self.assertIn('id="composer"', html)
        self.assertIn('class="mobile-nav"', html)
        self.assertIn('id="create-pairing"', html)
        self.assertIn('id="tunnel-state-label"', html)
        self.assertIn('id="remote-public-url"', html)
        self.assertIn('id="desktop-comparison-code"', html)
        self.assertIn('id="start-qr-scan"', html)
        self.assertIn('id="qr-scanner"', html)
        self.assertIn('id="pair-link-input"', html)
        self.assertIn('id="pair-details"', html)
        self.assertIn('id="device-identity"', html)
        self.assertIn('id="session-select"', html)
        self.assertIn('id="manage-synced-sessions"', html)
        self.assertIn("添加同步会话", html)
        self.assertNotIn("管理同步", html)
        self.assertIn('id="sync-session-dialog"', html)
        self.assertIn('id="local-session-select"', html)
        self.assertIn('id="synced-session-list"', html)
        self.assertIn('id="runtime-strip"', html)
        self.assertIn('id="runtime-activity"', html)
        self.assertIn('id="runtime-metrics"', html)
        self.assertIn('id="jump-latest"', html)
        self.assertNotIn('id="session-list"', html)
        self.assertNotIn('data-view="projects"', html)
        self.assertNotIn('data-view-panel="projects"', html)
        self.assertNotIn('data-mobile-view="projects"', html)
        self.assertNotIn("项目概览", html)
        self.assertNotIn("只显示会话监控中登记的任务", html)
        self.assertLess(html.index('id="create-pairing"'), html.index('id="device-list"'))

    def test_sync_manager_uses_the_independent_remote_catalog_api(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        self.assertIn("/api/remote/local-sessions?limit=50", script)
        self.assertIn("/api/remote/synced-sessions", script)
        self.assertIn("method: 'DELETE'", script)
        self.assertNotIn("/api/remote/projects", script)
        self.assertNotIn("renderProjects", script)

    def test_pwa_pairing_remembers_and_revalidates_the_device(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        self.assertIn("/api/remote/device", script)
        self.assertIn("BarcodeDetector", script)
        self.assertIn("getUserMedia", script)
        self.assertIn("acceptPairingUrl", script)
        self.assertIn("requestPersistentStorage", script)
        self.assertIn("remote-device", script)
        self.assertIn(".qr-scanner", stylesheet)
        self.assertIn(".pair-memory", stylesheet)

    def test_live_activity_can_override_a_stale_interrupted_snapshot(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        self.assertIn("LIVE_ACTIVITY_GRACE_MS", script)
        self.assertIn("INITIAL_LIVE_TURN_MAX_AGE_MS = 7200000", script)
        self.assertIn("lastConversationActivityAt", script)
        self.assertIn("conversationLooksRecentlyActive", script)
        self.assertIn("signature !== state.conversationSignature", script)
        self.assertIn("activeStates.includes(latestItem?.status)", script)
        self.assertIn("terminalStates.includes(lastItem?.status)", script)
        self.assertIn("runtime-rotor.running", stylesheet)
        self.assertIn(".scan-button { min-height: var(--control-height)", stylesheet)

    def test_admin_view_reports_local_frp_lifecycle(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        self.assertIn("function renderRemoteAccess", script)
        self.assertIn("function refreshAdminStatus", script)
        self.assertIn("tunnel-status-dot", stylesheet)
        self.assertIn("prefers-reduced-motion", stylesheet)

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

    def test_conversation_switch_has_stable_loading_and_error_feedback(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        self.assertIn("conversationLoadingTimer", script)
        self.assertIn("renderConversationLoading", script)
        self.assertIn("renderConversationReadError", script)
        self.assertIn("conversation-skeleton", script)
        self.assertIn("aria-busy", script)
        self.assertIn(".conversation-skeleton", stylesheet)
        self.assertIn("@keyframes skeleton-sweep", stylesheet)

    def test_running_stopped_and_failed_sessions_are_visually_distinct(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        self.assertIn("conversationStatusClass", script)
        self.assertIn("stream-caret", script)
        self.assertIn("status-badge.stopped::before", stylesheet)
        self.assertIn("status-badge.failed::before", stylesheet)
        self.assertIn(".stream-caret", stylesheet)
        self.assertIn("--conversation-inline", stylesheet)
        self.assertIn("prefers-reduced-motion: reduce", stylesheet)

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
