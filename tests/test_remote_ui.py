from __future__ import annotations

import unittest
import subprocess
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

    def test_pairing_keeps_the_current_origin_and_survives_confirmation_reload(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        accept_pairing = script[
            script.index("function acceptPairingUrl"):
            script.index("function parsePairingLink")
        ]
        claim_pairing = script[
            script.index("async function claimPairing"):
            script.index("async function enterAfterPairing")
        ]
        self.assertNotIn("location.assign", accept_pairing)
        self.assertIn("stopQrScanner({ keepFeedback: scannerActive })", accept_pairing)
        self.assertIn("storePendingPairing", claim_pairing)
        self.assertIn("readPendingPairing", script)
        self.assertIn("restorePendingPairing", script)

    def test_transient_remote_failures_do_not_immediately_report_disconnected(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        self.assertIn("CONNECTION_FAILURE_THRESHOLD = 3", script)
        self.assertIn("CONNECTION_FAILURE_GRACE_MS = 8000", script)
        self.assertIn("connectionFailureCount", script)
        self.assertIn("连接波动，正在复检", script)
        self.assertIn("error.authorizationFailed", script)
        self.assertIn("scheduleWorkspaceRetry", script)

    def test_camera_scanner_falls_back_when_barcode_detector_is_missing(self) -> None:
        html = (ROOT / "remote.html").read_text(encoding="utf-8")
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        server = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn('/assets/vendor/jsQR.js?v=1', html)
        self.assertIn("async function createQrDetector", script)
        self.assertIn("typeof window.jsQR === 'function'", script)
        self.assertIn("window.jsQR(image.data", script)
        self.assertNotIn("|| !('BarcodeDetector' in window)", script)
        self.assertGreaterEqual(server.count('"/assets/vendor/jsQR.js"'), 2)

    def test_vendored_decoder_reads_the_generated_pairing_qr(self) -> None:
        generator = ROOT / "assets" / "vendor" / "qrcode.min.js"
        decoder = ROOT / "assets" / "vendor" / "jsQR.js"
        node_script = r"""
const fs = require('fs');
const vm = require('vm');
const generatorPath = process.argv[1];
const decoderPath = process.argv[2];
const context = {};
vm.createContext(context);
vm.runInContext(fs.readFileSync(generatorPath, 'utf8'), context);
const jsQR = require(decoderPath);
const value = 'https://console.example.com/pair?pairing=12345678-1234-1234-1234-123456789012#secret=test-secret';
const qr = context.qrcode(0, 'M');
qr.addData(value);
qr.make();
const modules = qr.getModuleCount();
const quiet = 4;
const scale = 5;
const width = (modules + quiet * 2) * scale;
const pixels = new Uint8ClampedArray(width * width * 4);
pixels.fill(255);
for (let row = 0; row < modules; row += 1) {
  for (let column = 0; column < modules; column += 1) {
    if (!qr.isDark(row, column)) continue;
    for (let y = 0; y < scale; y += 1) {
      for (let x = 0; x < scale; x += 1) {
        const pixelX = (column + quiet) * scale + x;
        const pixelY = (row + quiet) * scale + y;
        const offset = (pixelY * width + pixelX) * 4;
        pixels[offset] = 0;
        pixels[offset + 1] = 0;
        pixels[offset + 2] = 0;
      }
    }
  }
}
const result = jsQR(pixels, width, width, { inversionAttempts: 'dontInvert' });
if (!result || result.data !== value) process.exit(1);
"""
        result = subprocess.run(
            ["node", "-e", node_script, str(generator), str(decoder)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_mobile_remote_mode_fills_the_viewport_without_a_single_item_nav(self) -> None:
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        self.assertIn("body.remote-mode .mobile-nav { display: none; }", stylesheet)
        self.assertIn("body.remote-mode .main { height: 100dvh; }", stylesheet)
        self.assertIn(".sessions-workspace { height: 100%; min-height: 0;", stylesheet)
        self.assertIn(".conversation { width: 100%; height: 100%; min-height: 0;", stylesheet)
        self.assertIn(".transcript { height: 100%; min-height: 0;", stylesheet)
        self.assertIn("@media (max-width: 480px)", stylesheet)
        self.assertIn(".conversation-head { min-height: 0; display: grid; grid-template-columns: minmax(0, 1fr);", stylesheet)
        self.assertNotIn("calc(100dvh - 421px", stylesheet)

    def test_mobile_pairing_flow_starts_near_the_top_on_short_screens(self) -> None:
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        self.assertIn(".pair-screen { grid-template-rows: auto auto; align-content: start;", stylesheet)
        self.assertIn(".pair-panel { align-self: start; }", stylesheet)

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
        html = (ROOT / "remote.html").read_text(encoding="utf-8")

        self.assertIn("url.pathname.startsWith('/api/')", script)
        self.assertNotIn("/api/remote", (ROOT / "manifest.webmanifest").read_text(encoding="utf-8"))
        server = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('/remote.css?v=14', script)
        self.assertIn('href="/remote.css?v=14"', html)
        self.assertIn('/remote.js?v=11', script)
        self.assertIn("fetch(event.request, { cache: 'no-store' })", script)
        self.assertIn('"/remote.css": ("remote.css", "text/css; charset=utf-8", "no-cache")', server)
        self.assertIn('"/remote.js": ("remote.js", "text/javascript; charset=utf-8", "no-cache")', server)
        self.assertIn('/assets/vendor/jsQR.js?v=1', script)

    def test_browser_renders_conversation_text_without_html_injection(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        self.assertIn("element.textContent =", script)
        self.assertIn("code.textContent =", script)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()
