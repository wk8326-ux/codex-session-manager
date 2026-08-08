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
        self.assertNotIn('data-console-sidebar', html)
        self.assertNotIn('/assets/console-sidebar.css', html)
        self.assertNotIn('/assets/console-sidebar.js', html)
        self.assertIn('id="composer"', html)
        self.assertIn('id="session-drawer"', html)
        self.assertIn('id="session-drawer-backdrop"', html)
        self.assertIn('id="open-session-drawer"', html)
        self.assertIn('id="close-session-drawer"', html)
        self.assertIn('id="session-drawer-list"', html)
        self.assertIn('href="/"', html)
        self.assertNotIn('class="mobile-nav"', html)
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

    def test_mobile_client_uses_a_swipeable_session_drawer_and_fixed_chat_shell(self) -> None:
        html = (ROOT / "remote.html").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        self.assertIn('aria-controls="session-drawer"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('id="conversation-title"', html)
        self.assertIn('id="remote-theme-toggle"', html)
        self.assertIn("function openSessionDrawer()", script)
        self.assertIn("function closeSessionDrawer", script)
        self.assertIn("touchstart", script)
        self.assertIn("touchend", script)
        self.assertIn("deltaX < -60", script)
        self.assertIn("body.drawer-open .session-drawer", stylesheet)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr);", stylesheet)
        self.assertIn("padding-bottom: max(10px, env(safe-area-inset-bottom));", stylesheet)

    def test_desktop_session_drawer_is_persistent_and_keeps_chat_interactive(self) -> None:
        html = (ROOT / "remote.html").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        self.assertIn('role="complementary"', html)
        self.assertNotIn('aria-modal="true"', html)
        self.assertIn("DRAWER_STATE_KEY", script)
        self.assertIn("readDrawerOpenPreference", script)
        self.assertIn("storeDrawerOpenPreference", script)
        self.assertIn("drawerUsesModalOverlay", script)
        self.assertIn("@media (min-width: 761px)", stylesheet)
        self.assertIn("body.drawer-open .app-shell", stylesheet)
        self.assertIn("grid-template-columns: var(--drawer-width) minmax(0, 1fr)", stylesheet)

    def test_session_drawer_uses_visible_icon_and_text_statuses(self) -> None:
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        self.assertIn("sessionStatusOverrides: new Map()", script)
        self.assertIn("function sessionSnapshotStatus", script)
        self.assertIn("function updateSessionStatusesFromEvents", script)
        self.assertIn("drawer-session-indicator", script)
        self.assertIn("drawer-session-status", script)
        self.assertIn(".drawer-session-indicator.running", stylesheet)
        self.assertIn(".drawer-session-indicator.loading", stylesheet)
        self.assertIn(".drawer-session-indicator.failed", stylesheet)
        self.assertIn(".drawer-session-indicator.completed", stylesheet)

    def test_session_summary_never_reports_an_unknown_state_as_stopped(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        start = script.index("  function stateLabel(value) {")
        end = script.index("\n  function updateSessionStatusesFromEvents", start)
        function_source = script[start:end]
        node_script = r"""
const vm = require('vm');
const source = process.argv[1];
const context = {};
vm.createContext(context);
vm.runInContext(`${source}; this.stateLabel = stateLabel; this.sessionSnapshotStatus = sessionSnapshotStatus;`, context);
const cases = [
  {
    expected: 'unknown',
    session: { statusKnown: false, threadStatus: 'idle', latestTurnStatus: '' },
  },
  {
    expected: 'completed',
    session: { statusKnown: true, threadStatus: 'idle', latestTurnStatus: 'completed' },
  },
  {
    expected: 'interrupted',
    session: { statusKnown: true, threadStatus: 'notLoaded', latestTurnStatus: 'interrupted' },
  },
  {
    expected: 'inProgress',
    session: { statusKnown: true, threadStatus: 'active', latestTurnStatus: 'inProgress' },
  },
];
for (const item of cases) {
  const actual = context.sessionSnapshotStatus(item.session);
  if (actual !== item.expected) {
    console.error(JSON.stringify({ expected: item.expected, actual, session: item.session }));
    process.exit(1);
  }
}
if (context.stateLabel('unknown') !== '状态未知') process.exit(2);
"""
        result = subprocess.run(
            ["node", "-e", node_script, function_source],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_chat_surface_uses_a_central_reading_column_and_integrated_composer(self) -> None:
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        self.assertIn("--chat-column: 760px", stylesheet)
        self.assertIn("width: min(var(--chat-column), 100%)", stylesheet)
        self.assertIn(".conversation { width: 100%; height: 100%;", stylesheet)
        self.assertIn(".composer-row { width: min(var(--chat-column), 100%);", stylesheet)
        self.assertIn(".composer-row textarea", stylesheet)

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

    def test_explicit_turn_errors_override_stale_active_thread_state(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        start = script.index("  function conversationStatus(detail) {")
        end = script.index("\n  function uuidV7Timestamp", start)
        function_source = script[start:end]
        node_script = r"""
const vm = require('vm');
const source = process.argv[1];
const context = { Date, console, state: { lastConversationActivityAt: 0, lastEventAt: 0 } };
vm.createContext(context);
vm.runInContext(`const LIVE_ACTIVITY_GRACE_MS = 180000;\n${source}; this.conversationStatus = conversationStatus;`, context);
const cases = [
  {
    expected: 'failed',
    detail: { status: 'active', turns: [{ status: 'failed', error: 'exceeded retry limit, last status: 429 Too Many Requests' }] },
  },
  {
    expected: 'failed',
    detail: { status: 'active', turns: [{ status: 'inProgress', error: 'unexpected status 503 Service Unavailable' }] },
  },
  {
    expected: 'inProgress',
    detail: { status: 'active', turns: [{ status: 'inProgress', items: [{ status: 'inProgress' }] }] },
  },
  {
    expected: 'interrupted',
    detail: { status: 'active', activeFlags: ['active'], turns: [{ status: 'interrupted' }] },
  },
];
for (const item of cases) {
  const actual = context.conversationStatus(item.detail);
  if (actual !== item.expected) {
    console.error(JSON.stringify({ expected: item.expected, actual, detail: item.detail }));
    process.exit(1);
  }
}
"""
        result = subprocess.run(
            ["node", "-e", node_script, function_source],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

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
        html = (ROOT / "remote.html").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        self.assertIn("interactive-widget=resizes-content", html)
        self.assertIn("--app-viewport-height: 100dvh", stylesheet)
        self.assertNotIn(".mobile-nav", stylesheet)
        self.assertIn(".main { height: var(--app-viewport-height);", stylesheet)
        self.assertIn(".sessions-workspace { min-height: 0; display: grid;", stylesheet)
        self.assertNotIn(".sessions-workspace { height: 100%;", stylesheet)
        self.assertIn(".conversation { width: 100%; height: 100%; min-height: 0;", stylesheet)
        self.assertIn(".transcript { height: 100%; min-height: 0;", stylesheet)
        self.assertIn("function syncVisualViewport()", script)
        self.assertIn("visualViewport.addEventListener('resize', syncVisualViewport", script)
        self.assertIn("--app-viewport-height", script)
        self.assertIn("@media (max-width: 480px)", stylesheet)
        self.assertIn(".chat-header", stylesheet)
        self.assertNotIn("calc(100dvh - 421px", stylesheet)

    def test_composer_supports_one_screenshot_with_preview_and_removal(self) -> None:
        html = (ROOT / "remote.html").read_text(encoding="utf-8")
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        for element_id in (
            "attach-image",
            "image-input",
            "attachment-preview",
            "attachment-thumbnail",
            "remove-attachment",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('accept="image/png,image/jpeg,image/webp"', html)
        self.assertIn("function prepareScreenshot(file)", script)
        self.assertIn("image: state.pendingImage?.dataUrl", script)
        self.assertIn(".composer-row", stylesheet)
        self.assertIn(".attachment-preview", stylesheet)

    def test_composer_accepts_capture_paste_and_a_configurable_page_shortcut(self) -> None:
        html = (ROOT / "remote.html").read_text(encoding="utf-8")
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        for element_id in (
            "capture-screen",
            "screenshot-shortcut-settings",
            "screenshot-shortcut-dialog",
            "screenshot-shortcut-input",
            "save-screenshot-shortcut",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("SCREENSHOT_SHORTCUT_KEY", script)
        self.assertIn("function captureScreenScreenshot()", script)
        self.assertIn("navigator.mediaDevices?.getDisplayMedia", script)
        self.assertIn("function clipboardImageFile(event)", script)
        self.assertIn("function handleComposerPaste(event)", script)
        self.assertIn("await prepareScreenshot(file)", script)
        self.assertIn(".capture-screen", stylesheet)
        self.assertIn(".shortcut-recorder", stylesheet)

        start = script.index("  function clipboardImageFile(event) {")
        end = script.index("\n  async function handleComposerPaste", start)
        function_source = script[start:end]
        node_script = r"""
const vm = require('vm');
const source = process.argv[1];
const context = {};
vm.createContext(context);
vm.runInContext(`${source}; this.clipboardImageFile = clipboardImageFile;`, context);
const expected = { name: 'pasted.png' };
const event = { clipboardData: { items: [
  { kind: 'string', type: 'text/plain', getAsFile: () => null },
  { kind: 'file', type: 'image/png', getAsFile: () => expected },
] } };
if (context.clipboardImageFile(event) !== expected) process.exit(1);
if (context.clipboardImageFile({ clipboardData: { items: [] } }) !== null) process.exit(2);
"""
        result = subprocess.run(
            ["node", "-e", node_script, function_source],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_screen_capture_stops_media_tracks_before_image_processing(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        start = script.index("  async function captureScreenScreenshot() {")
        end = script.index("\n  async function sendMessage", start)
        function_source = script[start:end]
        node_script = r"""
const vm = require('vm');
const source = process.argv[1];
const calls = [];
const tracks = [
  { stop: () => calls.push('stop-video') },
  { stop: () => calls.push('stop-audio') },
];
const stream = { getTracks: () => tracks };
const classList = { add: () => {}, remove: () => {} };
const controls = {
  '#capture-screen': { disabled: false, classList, setAttribute: () => {} },
  '#attach-image': { disabled: false },
};
const document = {
  createElement: tagName => {
    if (tagName === 'video') {
      return {
        readyState: 1,
        videoWidth: 120,
        videoHeight: 80,
        muted: false,
        playsInline: false,
        srcObject: null,
        play: async () => {},
      };
    }
    if (tagName === 'canvas') {
      return {
        width: 0,
        height: 0,
        getContext: () => ({ drawImage: () => calls.push('draw') }),
        toBlob: callback => callback({ size: 10, type: 'image/png' }),
      };
    }
    throw new Error('unexpected element ' + tagName);
  },
};
class FakeFile {
  constructor(parts, name, options) {
    this.parts = parts;
    this.name = name;
    this.type = options.type;
  }
}
const context = {
  state: { capturingScreen: false, selectedSessionId: 'session-1' },
  navigator: { mediaDevices: { getDisplayMedia: async () => stream } },
  document,
  File: FakeFile,
  requestAnimationFrame: callback => callback(),
  prepareScreenshot: async () => calls.push('prepare'),
  showToast: () => {},
  $: selector => controls[selector],
  Date,
};
vm.createContext(context);
vm.runInContext(source + '; this.captureScreenScreenshot = captureScreenScreenshot;', context);

(async () => {
  await context.captureScreenScreenshot();
  const firstStop = calls.findIndex(value => value.startsWith('stop-'));
  const prepare = calls.indexOf('prepare');
  if (firstStop < 0 || prepare < 0 || firstStop > prepare) process.exit(1);
  if (calls.filter(value => value.startsWith('stop-')).length !== 2) process.exit(2);
})().catch(error => {
  console.error(error);
  process.exit(3);
});
"""
        result = subprocess.run(
            ["node", "-e", node_script, function_source],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_markdown_pipe_tables_render_as_safe_semantic_tables(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        start = script.index("  function appendInlineMarkup(container, text) {")
        end = script.index("\n  function appendStreamCaret", start)
        function_source = script[start:end]
        node_script = r"""
const vm = require('vm');
const source = process.argv[1];

class FakeNode {
  constructor(tagName, text = '') {
    this.tagName = tagName;
    this.childNodes = [];
    this.attributes = {};
    this.dataset = {};
    this.className = '';
    this._text = text;
    this.classList = {
      add: (...names) => {
        const values = new Set(this.className.split(/\s+/).filter(Boolean));
        names.forEach(name => values.add(name));
        this.className = [...values].join(' ');
      },
    };
  }
  append(...children) { this.childNodes.push(...children); }
  replaceChildren(...children) { this.childNodes = [...children]; this._text = ''; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  set textContent(value) { this._text = String(value); this.childNodes = []; }
  get textContent() { return this._text + this.childNodes.map(node => node.textContent).join(''); }
}

const document = {
  createElement: tagName => new FakeNode(tagName.toUpperCase()),
  createTextNode: text => new FakeNode('#text', String(text)),
};
const context = { document };
vm.createContext(context);
vm.runInContext(`${source}; this.appendRichText = appendRichText;`, context);

const container = new FakeNode('DIV');
context.appendRichText(container, `| 优先级 | 增强项 | 具体效果 |
| :--- | :---: | ---: |
| P0 | 状态缓存 | 页面立即显示 |
| P1 | 支持转义 \\| 竖线 | 保持可读 |`);

const wrapper = container.childNodes[0];
const table = wrapper?.childNodes[0];
if (wrapper?.className !== 'message-table-scroll') process.exit(1);
if (table?.tagName !== 'TABLE' || table.className !== 'message-table') process.exit(2);
if (table.childNodes[0]?.tagName !== 'THEAD') process.exit(3);
if (table.childNodes[1]?.tagName !== 'TBODY') process.exit(4);
if (table.childNodes[0].childNodes[0].childNodes.length !== 3) process.exit(5);
if (table.childNodes[1].childNodes.length !== 2) process.exit(6);
if (table.childNodes[1].textContent.includes('\\|')) process.exit(7);
if (!table.childNodes[1].textContent.includes('支持转义 | 竖线')) process.exit(8);
if (table.childNodes[0].childNodes[0].childNodes[0].attributes.scope !== 'col') process.exit(9);

const malformed = new FakeNode('DIV');
context.appendRichText(
  malformed,
  '| 列一 | 列二 |\n| -- | 不是分隔符 |\n| 内容 | <img src=x onerror=alert(1)> |'
);
if (malformed.childNodes.some(node => node.tagName === 'TABLE')) process.exit(10);
if (!malformed.textContent.includes('| 列一 | 列二 |')) process.exit(11);
if (!malformed.textContent.includes('<img src=x onerror=alert(1)>')) process.exit(12);
"""
        result = subprocess.run(
            ["node", "-e", node_script, function_source],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_pending_approval_is_visible_and_actionable_on_mobile(self) -> None:
        html = (ROOT / "remote.html").read_text(encoding="utf-8")
        script = (ROOT / "remote.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote.css").read_text(encoding="utf-8")

        for element_id in (
            "approval-tray",
            "approval-title",
            "approval-view-session",
            "approval-decline",
            "approval-accept-session",
            "approval-accept",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('aria-live="assertive"', html)
        self.assertIn("/api/remote/approvals", script)
        self.assertIn("function renderApprovals", script)
        self.assertIn("function resolveApproval", script)
        self.assertIn("waitingOnApproval", script)
        self.assertIn("remote/approval", script)
        self.assertIn("acceptForTurn", script)
        self.assertIn("本轮全部允许", html)
        self.assertIn(".approval-tray", stylesheet)
        self.assertIn(".status-badge.waiting", stylesheet)
        self.assertIn("min-height: 44px", stylesheet)
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(88px, 1fr))", stylesheet)

    def test_session_switch_restores_cached_conversation_before_refreshing(self) -> None:
        script = (ROOT / "remote.js").read_text(encoding="utf-8")

        self.assertIn("conversationCache: new Map()", script)
        self.assertIn("conversationRequests: new Set()", script)
        self.assertIn("function restoreCachedConversation(session)", script)
        self.assertIn("state.conversationCache.set(sessionId", script)
        self.assertNotIn("conversationRefreshInFlight: false", script)

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
        self.assertIn('/remote.css?v=21', script)
        self.assertIn('href="/remote.css?v=21"', html)
        self.assertIn('/remote.js?v=19', script)
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
