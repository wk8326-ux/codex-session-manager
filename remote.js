(() => {
  const TOKEN_KEY = 'localhost-project-console.remote-token';
  const DEVICE_KEY = 'localhost-project-console.remote-device';
  const PENDING_PAIRING_KEY = 'localhost-project-console.remote-pending-pairing';
  const THEME_KEY = 'localhost-project-console.theme';
  const DRAWER_STATE_KEY = 'localhost-project-console.remote-drawer-open';
  const SCREENSHOT_SHORTCUT_KEY = 'localhost-project-console.remote-screenshot-shortcut';
  const DEFAULT_SCREENSHOT_SHORTCUT = Object.freeze({
    code: 'KeyS',
    ctrlKey: false,
    altKey: true,
    shiftKey: true,
    metaKey: false,
  });
  const ACTIVE_REFRESH_MS = 1200;
  const IDLE_REFRESH_MS = 5000;
  const HIDDEN_REFRESH_MS = 12000;
  const LIVE_ACTIVITY_GRACE_MS = 180000;
  const INITIAL_LIVE_TURN_MAX_AGE_MS = 7200000;
  const CONNECTION_FAILURE_THRESHOLD = 3;
  const CONNECTION_FAILURE_GRACE_MS = 8000;
  const WORKSPACE_RETRY_MS = 2500;
  const SCREENSHOT_MAX_DIMENSION = 1600;
  const SCREENSHOT_MAX_SOURCE_BYTES = 12_000_000;
  const SCREENSHOT_MAX_DATA_URL_LENGTH = 1_550_000;
  const SCREENSHOT_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const state = {
    admin: false,
    token: '',
    pendingToken: '',
    device: null,
    pendingDevice: null,
    pendingComparisonCode: '',
    sessions: [],
    localSessions: [],
    devices: [],
    approvals: [],
    activeApprovalId: '',
    approvalResolvingId: '',
    approvalRefreshInFlight: false,
    approvalRefreshQueued: false,
    approvalExpiryTimer: 0,
    selectedSessionId: '',
    cursor: 0,
    polling: false,
    pairingSecret: '',
    pairingId: '',
    pairingUrl: '',
    qrStream: null,
    qrScanFrame: 0,
    qrScanGeneration: 0,
    conversation: null,
    conversationSignature: '',
    conversationRefreshTimer: 0,
    conversationLoadingTimer: 0,
    conversationSelectionVersion: 0,
    conversationCache: new Map(),
    conversationRequests: new Set(),
    sessionStatusOverrides: new Map(),
    pendingImage: null,
    screenshotShortcut: readScreenshotShortcut(),
    screenshotShortcutDraft: null,
    capturingScreen: false,
    followTail: true,
    lastUpdatedAt: 0,
    lastEvent: null,
    lastEventAt: 0,
    lastConversationActivityAt: 0,
    adminStatus: null,
    connectionFailureCount: 0,
    connectionFailureStartedAt: 0,
    workspaceRetryTimer: 0,
    drawerOpen: false,
    currentView: 'sessions',
    drawerTouchStart: null,
    drawerReturnFocus: null,
  };

  function syncVisualViewport() {
    const viewportHeight = Math.max(240, Math.round(
      window.visualViewport?.height || window.innerHeight || document.documentElement.clientHeight
    ));
    document.documentElement.style.setProperty('--app-viewport-height', `${viewportHeight}px`);
    document.body.classList.toggle('compact-viewport', viewportHeight < 520);
  }

  function setupVisualViewport() {
    syncVisualViewport();
    addEventListener('resize', syncVisualViewport, { passive: true });
    if (window.visualViewport) {
      visualViewport.addEventListener('resize', syncVisualViewport, { passive: true });
      visualViewport.addEventListener('scroll', syncVisualViewport, { passive: true });
    }
  }

  function normalizedScreenshotShortcut(value) {
    if (!value || typeof value !== 'object' || typeof value.code !== 'string') {
      return { ...DEFAULT_SCREENSHOT_SHORTCUT };
    }
    const shortcut = {
      code: value.code,
      ctrlKey: Boolean(value.ctrlKey),
      altKey: Boolean(value.altKey),
      shiftKey: Boolean(value.shiftKey),
      metaKey: Boolean(value.metaKey),
    };
    if (!shortcut.code || !(shortcut.ctrlKey || shortcut.altKey || shortcut.metaKey)) {
      return { ...DEFAULT_SCREENSHOT_SHORTCUT };
    }
    return shortcut;
  }

  function readScreenshotShortcut() {
    try {
      return normalizedScreenshotShortcut(
        JSON.parse(localStorage.getItem(SCREENSHOT_SHORTCUT_KEY) || 'null')
      );
    } catch {
      return { ...DEFAULT_SCREENSHOT_SHORTCUT };
    }
  }

  function storeScreenshotShortcut(value) {
    const shortcut = normalizedScreenshotShortcut(value);
    try { localStorage.setItem(SCREENSHOT_SHORTCUT_KEY, JSON.stringify(shortcut)); } catch {}
    state.screenshotShortcut = shortcut;
  }

  function screenshotShortcutKeyLabel(code) {
    if (/^Key[A-Z]$/.test(code)) return code.slice(3);
    if (/^Digit\d$/.test(code)) return code.slice(5);
    if (/^F(?:[1-9]|1[0-2])$/.test(code)) return code;
    return {
      Backquote: '`',
      Minus: '-',
      Equal: '=',
      BracketLeft: '[',
      BracketRight: ']',
      Backslash: '\\',
      Semicolon: ';',
      Quote: "'",
      Comma: ',',
      Period: '.',
      Slash: '/',
      Space: 'Space',
    }[code] || code.replace(/^(Arrow|Numpad)/, '');
  }

  function formatScreenshotShortcut(shortcut) {
    const parts = [];
    if (shortcut.ctrlKey) parts.push('Ctrl');
    if (shortcut.altKey) parts.push('Alt');
    if (shortcut.shiftKey) parts.push('Shift');
    if (shortcut.metaKey) parts.push('Command');
    parts.push(screenshotShortcutKeyLabel(shortcut.code));
    return parts.join(' + ');
  }

  function screenshotShortcutFromEvent(event) {
    if (['Control', 'Alt', 'Shift', 'Meta'].includes(event.key)) return null;
    if (!(event.ctrlKey || event.altKey || event.metaKey)) return false;
    return normalizedScreenshotShortcut({
      code: event.code,
      ctrlKey: event.ctrlKey,
      altKey: event.altKey,
      shiftKey: event.shiftKey,
      metaKey: event.metaKey,
    });
  }

  function matchesScreenshotShortcut(event) {
    const shortcut = state.screenshotShortcut;
    return event.code === shortcut.code
      && event.ctrlKey === shortcut.ctrlKey
      && event.altKey === shortcut.altKey
      && event.shiftKey === shortcut.shiftKey
      && event.metaKey === shortcut.metaKey;
  }

  function readToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
  }

  function storeToken(value) {
    try {
      if (value) localStorage.setItem(TOKEN_KEY, value);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {}
    state.token = value;
  }

  function readStoredDevice() {
    try { return JSON.parse(localStorage.getItem(DEVICE_KEY) || 'null'); } catch { return null; }
  }

  function storeRememberedDevice(device) {
    try {
      if (device) localStorage.setItem(DEVICE_KEY, JSON.stringify(device));
      else localStorage.removeItem(DEVICE_KEY);
    } catch {}
    state.device = device;
  }

  function readPendingPairing() {
    try { return JSON.parse(localStorage.getItem(PENDING_PAIRING_KEY) || 'null'); } catch { return null; }
  }

  function storePendingPairing(value) {
    try {
      if (value) localStorage.setItem(PENDING_PAIRING_KEY, JSON.stringify(value));
      else localStorage.removeItem(PENDING_PAIRING_KEY);
    } catch {}
  }

  function restorePendingPairing() {
    const pending = readPendingPairing();
    if (!pending?.token || !pending?.device?.id || !pending?.comparisonCode) return false;
    state.pendingToken = pending.token;
    state.pendingDevice = pending.device;
    state.pendingComparisonCode = pending.comparisonCode;
    return true;
  }

  function forgetRememberedDevice() {
    storeToken('');
    storeRememberedDevice(null);
    storePendingPairing(null);
  }

  async function requestPersistentStorage() {
    try {
      if (navigator.storage?.persist) await navigator.storage.persist();
    } catch {}
  }

  function renderDeviceIdentity() {
    const identity = $('#device-identity');
    const device = state.device || readStoredDevice();
    identity.hidden = false;
    identity.textContent = state.admin
      ? '本机管理端'
      : (device?.name ? `已配对设备 · ${device.name}` : '移动设备');
  }

  function applyTheme(theme) {
    const resolved = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.theme = resolved;
    document.documentElement.style.colorScheme = resolved;
    renderRemoteTheme();
  }

  function renderRemoteTheme() {
    const dark = document.documentElement.dataset.theme === 'dark';
    const toggle = $('#remote-theme-toggle');
    if (!toggle) return;
    toggle.setAttribute('aria-pressed', String(dark));
    toggle.setAttribute('aria-label', dark ? '切换至浅色模式' : '切换至深色模式');
    $('#remote-theme-label').textContent = dark ? '浅色模式' : '深色模式';
  }

  function toggleRemoteTheme() {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem(THEME_KEY, next); } catch {}
    applyTheme(next);
  }

  function showToast(message) {
    const toast = $('#toast');
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => { toast.hidden = true; }, 3200);
  }

  function setConnected(connected, options = {}) {
    const connectionDot = $('#drawer-connection-dot');
    if (connected) {
      state.connectionFailureCount = 0;
      state.connectionFailureStartedAt = 0;
      $('#offline-banner').hidden = true;
      $('#sync-state').textContent = '已同步';
      connectionDot.className = 'connection-dot online';
      return true;
    }

    const now = Date.now();
    state.connectionFailureCount += 1;
    if (!state.connectionFailureStartedAt) state.connectionFailureStartedAt = now;
    const forceOffline = options.immediate || navigator.onLine === false;
    const confirmedOffline = forceOffline
      || state.connectionFailureCount >= CONNECTION_FAILURE_THRESHOLD
      || now - state.connectionFailureStartedAt >= CONNECTION_FAILURE_GRACE_MS;
    $('#offline-banner').hidden = !confirmedOffline;
    $('#sync-state').textContent = confirmedOffline ? '重新连接中' : '连接波动，正在复检';
    connectionDot.className = `connection-dot ${confirmedOffline ? 'offline' : 'checking'}`;
    return confirmedOffline;
  }

  async function api(path, options = {}) {
    const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
    if (!state.admin && state.token) headers.Authorization = `Bearer ${state.token}`;
    const response = await fetch(path, { ...options, headers, cache: 'no-store' });
    const payload = response.status === 204
      ? null
      : await response.json().catch(() => ({ message: '服务返回了无法读取的响应。' }));
    if (response.status === 401 && !state.admin) {
      forgetRememberedDevice();
      const error = new Error('设备授权已失效，请重新扫码配对。');
      error.authorizationFailed = true;
      error.status = response.status;
      throw error;
    }
    if (!response.ok) {
      const error = new Error(payload?.message || `请求失败 (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function stopQrScanner(options = {}) {
    const keepFeedback = options.keepFeedback === true;
    state.qrScanGeneration += 1;
    cancelAnimationFrame(state.qrScanFrame);
    state.qrScanFrame = 0;
    state.qrStream?.getTracks().forEach(track => track.stop());
    state.qrStream = null;
    $('#qr-video').srcObject = null;
    $('#qr-scanner').classList.toggle('scan-success', keepFeedback);
    $('#qr-scanner').hidden = !keepFeedback;
    if (!keepFeedback) $('#scan-status').textContent = '将二维码完整放入取景框';
  }

  function showPairingStep() {
    const ready = Boolean(state.pairingId && state.pairingSecret);
    $('#pair-start').hidden = ready;
    $('#pair-details').hidden = !ready;
    $('#mobile-comparison').hidden = true;
    $('#claim-pairing').hidden = false;
    $('#claim-pairing').disabled = !ready;
    $('#pair-device-name').disabled = false;
    if (ready) $('#pair-device-name').focus();
  }

  function showPendingPairingStep() {
    $('#pair-start').hidden = true;
    $('#pair-details').hidden = true;
    $('#claim-pairing').hidden = true;
    $('#pair-device-name').disabled = true;
    $('#mobile-comparison-code').textContent = state.pendingComparisonCode || '------';
    $('#mobile-comparison').hidden = false;
  }

  function acceptPairingUrl(value) {
    try {
      const url = new URL(String(value || '').trim(), location.href);
      const pairingId = url.searchParams.get('pairing') || '';
      const secret = new URLSearchParams(url.hash.slice(1)).get('secret') || '';
      if (!pairingId || !secret) throw new Error('没有识别到有效的配对信息。');
      state.pairingId = pairingId;
      state.pairingSecret = secret;
      const scannerActive = Boolean(state.qrStream);
      $('#scan-status').textContent = '二维码已识别，正在准备配对';
      stopQrScanner({ keepFeedback: scannerActive });
      history.replaceState(null, '', `${location.pathname}?pairing=${encodeURIComponent(pairingId)}`);
      $('#pair-error').textContent = '';
      if (scannerActive) {
        setTimeout(() => {
          $('#qr-scanner').hidden = true;
          $('#qr-scanner').classList.remove('scan-success');
          showPairingStep();
        }, 320);
      } else showPairingStep();
      return true;
    } catch (error) {
      $('#pair-error').textContent = error.message || '配对链接无法读取。';
      return false;
    }
  }

  function parsePairingLink() {
    const query = new URLSearchParams(location.search);
    const fragment = new URLSearchParams(location.hash.slice(1));
    const pairingId = query.get('pairing') || '';
    const secret = fragment.get('secret') || '';
    if (pairingId && secret) acceptPairingUrl(location.href);
  }

  function resetPairing() {
    stopQrScanner();
    state.pairingId = '';
    state.pairingSecret = '';
    state.pendingToken = '';
    state.pendingDevice = null;
    state.pendingComparisonCode = '';
    $('#pair-link-input').value = '';
    $('#pair-error').textContent = '';
    history.replaceState(null, '', location.pathname);
    showPairingStep();
  }

  async function scanQrFrame(detector) {
    if (!state.qrStream) return;
    const generation = state.qrScanGeneration;
    try {
      const codes = await detector.detect($('#qr-video'));
      if (generation !== state.qrScanGeneration || !state.qrStream) return;
      const value = codes.find(code => code.rawValue)?.rawValue;
      if (value && acceptPairingUrl(value)) return;
    } catch {}
    if (generation === state.qrScanGeneration && state.qrStream) {
      state.qrScanFrame = requestAnimationFrame(() => scanQrFrame(detector));
    }
  }

  async function createQrDetector() {
    if ('BarcodeDetector' in window) {
      try {
        const supported = BarcodeDetector.getSupportedFormats
          ? await BarcodeDetector.getSupportedFormats()
          : ['qr_code'];
        if (supported.includes('qr_code')) {
          return new BarcodeDetector({ formats: ['qr_code'] });
        }
      } catch {}
    }

    if (typeof window.jsQR === 'function') {
      const canvas = document.createElement('canvas');
      const context = canvas.getContext('2d', { willReadFrequently: true });
      if (!context) throw new Error('当前浏览器无法初始化二维码识别。');
      return {
        detect(video) {
          if (video.readyState < 2 || !video.videoWidth || !video.videoHeight) return [];
          const scale = Math.min(1, 720 / Math.max(video.videoWidth, video.videoHeight));
          const width = Math.max(1, Math.round(video.videoWidth * scale));
          const height = Math.max(1, Math.round(video.videoHeight * scale));
          if (canvas.width !== width || canvas.height !== height) {
            canvas.width = width;
            canvas.height = height;
          }
          context.drawImage(video, 0, 0, width, height);
          const image = context.getImageData(0, 0, width, height);
          const code = window.jsQR(image.data, width, height, {
            inversionAttempts: 'attemptBoth',
          });
          return code?.data ? [{ rawValue: code.data }] : [];
        },
      };
    }

    throw new Error('当前浏览器缺少二维码识别组件，请粘贴电脑端的配对链接。');
  }

  async function startQrScanner() {
    $('#pair-error').textContent = '';
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      $('#pair-error').textContent = '当前浏览器无法直接调用摄像头扫码，请粘贴电脑端的配对链接。';
      $('#pair-link-input').focus();
      return;
    }
    try {
      stopQrScanner();
      $('#scan-status').textContent = '正在打开相机';
      const detector = await createQrDetector();
      state.qrStream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: { ideal: 'environment' },
          width: { ideal: 1280 },
          height: { ideal: 720 },
        },
        audio: false,
      });
      $('#qr-video').srcObject = state.qrStream;
      $('#qr-scanner').hidden = false;
      await $('#qr-video').play();
      $('#scan-status').textContent = '将二维码完整放入取景框';
      scanQrFrame(detector);
    } catch (error) {
      stopQrScanner();
      $('#pair-error').textContent = error?.name === 'NotAllowedError'
        ? '相机权限未开启。允许使用相机后重新点击扫描，或粘贴配对链接。'
        : (error.message || '摄像头暂时无法使用，请粘贴配对链接。');
    }
  }

  function showPairScreen(message = '') {
    closeSessionDrawer({ restoreFocus: false });
    $('#app-shell').hidden = true;
    $('#pair-screen').hidden = false;
    $('#pair-error').textContent = message;
    if (state.pendingToken && state.pendingDevice) showPendingPairingStep();
    else showPairingStep();
  }

  async function claimPairing() {
    const button = $('#claim-pairing');
    button.disabled = true;
    $('#pair-error').textContent = '';
    try {
      const result = await api(`/api/remote/pairings/${encodeURIComponent(state.pairingId)}/claim`, {
        method: 'POST',
        body: JSON.stringify({
          secret: state.pairingSecret,
          deviceName: $('#pair-device-name').value.trim() || '我的手机',
        }),
      });
      state.pendingToken = result.deviceToken;
      state.pendingDevice = { id: result.deviceId, name: result.deviceName };
      state.pendingComparisonCode = result.comparisonCode;
      storePendingPairing({
        token: state.pendingToken,
        device: state.pendingDevice,
        comparisonCode: state.pendingComparisonCode,
      });
      await requestPersistentStorage();
      state.pairingSecret = '';
      showPendingPairingStep();
    } catch (error) {
      $('#pair-error').textContent = error.message;
      button.disabled = false;
    }
  }

  async function enterAfterPairing() {
    storeToken(state.pendingToken);
    storeRememberedDevice(state.pendingDevice);
    await requestPersistentStorage();
    storePendingPairing(null);
    state.pendingToken = '';
    state.pendingDevice = null;
    state.pendingComparisonCode = '';
    history.replaceState(null, '', location.pathname);
    $('#pair-screen').hidden = true;
    $('#app-shell').hidden = false;
    renderDeviceIdentity();
    await startWorkspace();
    showToast('这台设备已记住，后续打开将自动连接。');
  }

  function stateLabel(value) {
    const labels = {
      active: '正在运行', inProgress: '正在运行', idle: '已停止', completed: '已完成',
      failed: '运行失败', interrupted: '已中断', systemError: '系统错误', notLoaded: '已停止',
      waitingOnApproval: '等待授权', unknown: '状态未知',
      running: '正在运行', started: '正在运行', online: '在线', stopped: '已停止', offline: '离线',
    };
    return labels[value] || value || '未知';
  }

  function sessionSnapshotStatus(session) {
    if (session?.statusKnown === false) return 'unknown';
    const latestStatus = session?.latestTurnStatus || '';
    const threadStatus = session?.threadStatus || '';
    const activeStates = ['active', 'inProgress', 'running', 'started'];
    const failedStates = ['failed', 'interrupted', 'systemError', 'cancelled'];
    if (session?.latestTurnHasError || failedStates.includes(latestStatus)) {
      return latestStatus || 'failed';
    }
    if (activeStates.includes(latestStatus)) return 'inProgress';
    if (latestStatus === 'completed') return 'completed';
    if ((session?.activeFlags || []).some(flag => activeStates.includes(flag))) return 'inProgress';
    if (failedStates.includes(threadStatus)) return threadStatus;
    if (activeStates.includes(threadStatus)) return 'inProgress';
    if (threadStatus === 'completed') return 'completed';
    return 'stopped';
  }

  function updateSessionStatusesFromEvents(events = []) {
    const activeStates = ['active', 'inProgress', 'running', 'started'];
    const failedStates = ['failed', 'interrupted', 'systemError', 'cancelled'];
    for (const event of events) {
      if (!event?.threadId) continue;
      if (event.method === 'remote/approvalRequested') {
        state.sessionStatusOverrides.set(event.threadId, 'waitingOnApproval');
        continue;
      }
      if (event.method === 'remote/approvalResolved') {
        state.sessionStatusOverrides.set(event.threadId, 'inProgress');
        continue;
      }
      if (failedStates.includes(event.status)) {
        state.sessionStatusOverrides.set(event.threadId, event.status);
        continue;
      }
      if (event.method === 'turn/completed') {
        state.sessionStatusOverrides.set(
          event.threadId,
          event.status === 'completed' || !event.status ? 'completed' : event.status,
        );
        continue;
      }
      if (activeStates.includes(event.status)
          || event.method?.startsWith('turn/')
          || event.method?.startsWith('item/')) {
        state.sessionStatusOverrides.set(event.threadId, 'inProgress');
      }
    }
    if (events.length) renderSessionDrawer();
  }

  function pendingApprovalForSelected() {
    const session = state.sessions.find(item => item.id === state.selectedSessionId);
    return state.approvals.find(item => item.threadId === session?.threadId) || null;
  }

  function approvalExpiryText(approval) {
    const expiresAt = Date.parse(approval?.expiresAt || '');
    if (!Number.isFinite(expiresAt)) return '未处理时将自动拒绝';
    const seconds = Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
    return seconds ? `${seconds} 秒后自动拒绝` : '正在按安全策略自动拒绝';
  }

  function renderApprovals() {
    clearInterval(state.approvalExpiryTimer);
    state.approvalExpiryTimer = 0;
    const tray = $('#approval-tray');
    const selected = state.sessions.find(item => item.id === state.selectedSessionId);
    const approval = pendingApprovalForSelected() || state.approvals[0] || null;
    state.activeApprovalId = approval?.id || '';
    tray.hidden = !approval;
    $('#header-notice-dot').classList.toggle('visible', state.approvals.length > 0);
    if (!approval) return;

    const isSelected = approval.sessionId === selected?.id;
    const sameThreadCount = state.approvals.filter(
      item => item.threadId === approval.threadId
    ).length;
    $('#approval-title').textContent = isSelected ? approval.title : '有会话等待授权';
    $('#approval-session-name').textContent = [
      approval.sessionName,
      sameThreadCount > 1 ? `${sameThreadCount} 项待处理` : '',
    ].filter(Boolean).join(' · ');
    $('#approval-summary').textContent = approval.summary || 'Codex 正在等待授权决定';
    $('#approval-detail').textContent = approval.detail || '';
    const updateExpiry = () => {
      $('#approval-expiry').textContent = approvalExpiryText(approval);
    };
    updateExpiry();
    state.approvalExpiryTimer = setInterval(updateExpiry, 1000);

    const decisions = new Set(approval.availableDecisions || []);
    const resolving = state.approvalResolvingId === approval.id;
    $('#approval-view-session').hidden = isSelected;
    $('#approval-decline').hidden = !isSelected || !decisions.has('decline');
    $('#approval-accept').hidden = !isSelected || !decisions.has('accept');
    const canAllowTurn = Boolean(approval.turnId)
      && (decisions.has('accept') || decisions.has('acceptForSession'));
    $('#approval-accept-session').hidden = !isSelected || !canAllowTurn;
    $$('#approval-actions button').forEach(button => { button.disabled = resolving; });
    $('#approval-actions').setAttribute('aria-busy', String(resolving));
  }

  async function loadApprovals(options = {}) {
    if (state.approvalRefreshInFlight) {
      state.approvalRefreshQueued = true;
      return;
    }
    state.approvalRefreshInFlight = true;
    const previous = state.approvals.map(item => item.id).join(',');
    try {
      state.approvals = await api('/api/remote/approvals');
      renderSessions();
      renderApprovals();
      const changed = previous !== state.approvals.map(item => item.id).join(',');
      if (changed && options.renderConversation !== false && state.conversation) {
        const session = state.sessions.find(item => item.id === state.selectedSessionId);
        if (session) renderConversation(session, state.conversation);
      }
    } finally {
      state.approvalRefreshInFlight = false;
      if (state.approvalRefreshQueued) {
        state.approvalRefreshQueued = false;
        queueMicrotask(() => loadApprovals(options).catch(error => showToast(error.message)));
      }
    }
  }

  async function resolveApproval(decision) {
    const approval = state.approvals.find(item => item.id === state.activeApprovalId);
    const selected = state.sessions.find(item => item.id === state.selectedSessionId);
    if (!approval || approval.sessionId !== selected?.id) return;
    state.approvalResolvingId = approval.id;
    renderApprovals();
    try {
      await api(`/api/remote/approvals/${encodeURIComponent(approval.id)}/decision`, {
        method: 'POST',
        body: JSON.stringify({ decision }),
      });
      showToast({
        accept: '已允许本次操作。',
        acceptForSession: '已允许当前会话继续执行。',
        acceptForTurn: '本轮任务后续授权将自动允许。',
        decline: '已拒绝本次操作。',
      }[decision] || '授权决定已发送。');
    } catch (error) {
      showToast(error.message);
    } finally {
      state.approvalResolvingId = '';
      await loadApprovals().catch(error => showToast(error.message));
      clearTimeout(state.conversationRefreshTimer);
      scheduleConversationRefresh(80);
    }
  }

  async function viewApprovalSession() {
    const approval = state.approvals.find(item => item.id === state.activeApprovalId);
    if (approval?.sessionId) await selectSession(approval.sessionId);
  }

  function renderSessions() {
    const select = $('#session-select');
    select.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = state.sessions.length ? '选择同步会话' : '尚未添加同步会话';
    select.append(placeholder);
    for (const session of state.sessions) {
      const option = document.createElement('option');
      option.value = session.id;
      const pendingCount = state.approvals.filter(
        approval => approval.threadId === session.threadId
      ).length;
      option.textContent = `${session.name}${pendingCount ? ` · 待审批 ${pendingCount}` : ''}`;
      select.append(option);
    }
    select.disabled = state.sessions.length === 0;
    select.value = state.selectedSessionId;
    const selected = state.sessions.find(item => item.id === state.selectedSessionId);
    $('#conversation-title').textContent = state.currentView === 'devices'
      ? '配对设备'
      : (selected?.name || '远程会话');
    renderSessionDrawer();
  }

  function renderSessionDrawer() {
    const list = $('#session-drawer-list');
    list.replaceChildren();
    if (!state.sessions.length) {
      const empty = document.createElement('div');
      empty.className = 'drawer-empty';
      empty.textContent = state.admin
        ? '尚未添加同步会话。点击上方加号从本机 Codex 选择。'
        : '电脑端尚未添加可同步的会话。';
      list.append(empty);
      $('#header-notice-dot').classList.toggle('visible', state.approvals.length > 0);
      return;
    }

    for (const session of state.sessions) {
      const selectedDetail = session.id === state.selectedSessionId
        ? state.conversation
        : null;
      const cachedEntry = state.conversationCache.get(session.id);
      const pendingCount = state.approvals.filter(
        approval => approval.threadId === session.threadId
      ).length;
      let status = sessionSnapshotStatus(session);
      if (pendingCount) status = 'waitingOnApproval';
      else if (selectedDetail) status = conversationStatus(selectedDetail);
      else if (state.sessionStatusOverrides.has(session.threadId)) {
        status = state.sessionStatusOverrides.get(session.threadId);
      } else if (cachedEntry?.conversation) {
        status = conversationStatusWithActivity(cachedEntry.conversation, cachedEntry);
      }
      const statusClass = conversationStatusClass(status);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `drawer-session${session.id === state.selectedSessionId ? ' active' : ''}`;
      button.setAttribute('aria-current', session.id === state.selectedSessionId ? 'true' : 'false');
      button.title = `${session.name || session.threadId} · ${stateLabel(status)}\n${session.threadId}`;

      const indicator = document.createElement('span');
      indicator.className = `drawer-session-indicator ${statusClass}`;
      indicator.setAttribute('aria-hidden', 'true');
      const copy = document.createElement('span');
      copy.className = 'drawer-session-copy';
      const name = document.createElement('strong');
      name.textContent = session.name || session.threadId;
      const detail = document.createElement('small');
      detail.className = `drawer-session-status ${statusClass}`;
      detail.textContent = stateLabel(status);
      detail.title = session.threadId;
      copy.append(name, detail);
      button.append(indicator, copy);
      if (pendingCount) {
        const badge = document.createElement('span');
        badge.className = 'drawer-session-badge';
        badge.textContent = String(pendingCount);
        badge.setAttribute('aria-label', `${pendingCount} 项待审批`);
        button.append(badge);
      }
      button.addEventListener('click', async () => {
        switchView('sessions');
        await selectSession(session.id);
        $('#message-input').focus({ preventScroll: true });
      });
      list.append(button);
    }
    $('#header-notice-dot').classList.toggle('visible', state.approvals.length > 0);
  }

  function clearConversation() {
    clearTimeout(state.conversationRefreshTimer);
    clearTimeout(state.conversationLoadingTimer);
    state.conversation = null;
    state.conversationSignature = '';
    state.lastEvent = null;
    state.lastEventAt = 0;
    state.lastConversationActivityAt = 0;
    state.followTail = true;
    $('#conversation-title').textContent = '远程会话';
    $('#conversation-meta').textContent = '尚未选择会话';
    $('#conversation-status').textContent = '未选择';
    $('#conversation-status').className = 'status-badge stopped';
    $('#session-select').setAttribute('aria-busy', 'false');
    $('#message-input').disabled = true;
    $('#send-button').disabled = true;
    $('#attach-image').disabled = true;
    $('#capture-screen').disabled = true;
    removePendingImage();
    $('#runtime-strip').className = 'runtime-strip';
    $('#runtime-signal').className = 'runtime-signal runtime-rotor';
    $('#runtime-activity').textContent = '等待选择会话';
    $('#runtime-detail').textContent = '选择后将持续同步 Codex 的最新活动';
    $('#runtime-metrics').textContent = '未连接';
    $('#jump-latest').hidden = true;
    const transcript = $('#transcript');
    transcript.setAttribute('aria-busy', 'false');
    transcript.replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'conversation-empty';
    const title = document.createElement('strong');
    title.textContent = state.sessions.length ? '等待选择会话' : '尚未添加同步会话';
    const copy = document.createElement('span');
    copy.textContent = state.admin ? '使用“添加同步会话”从本机 Codex 选择' : '请在电脑端添加需要同步的会话';
    empty.append(title, copy);
    transcript.append(empty);
    renderApprovals();
  }

  function renderSyncManager() {
    const localSelect = $('#local-session-select');
    localSelect.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = state.localSessions.length ? '选择本机 Codex 会话' : '没有可读取的本机会话';
    localSelect.append(placeholder);
    for (const session of state.localSessions) {
      const option = document.createElement('option');
      option.value = session.threadId;
      option.textContent = `${session.name || session.threadId} · ${stateLabel(session.threadStatus)}`;
      option.disabled = Boolean(session.synced);
      localSelect.append(option);
    }
    $('#add-synced-session').disabled = true;
    $('#sync-session-name').value = '';
    $('#synced-session-count').textContent = `${state.sessions.length} 个`;
    const list = $('#synced-session-list');
    list.replaceChildren();
    if (!state.sessions.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = '尚未添加远程同步会话。';
      list.append(empty);
      return;
    }
    for (const session of state.sessions) {
      const row = document.createElement('div');
      row.className = 'synced-session-row';
      const meta = document.createElement('div');
      meta.className = 'synced-session-meta';
      const name = document.createElement('strong');
      name.textContent = session.name;
      const threadId = document.createElement('small');
      threadId.textContent = session.threadId;
      meta.append(name, threadId);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'text-button';
      remove.textContent = '移除';
      remove.addEventListener('click', () => removeSyncedSession(session));
      row.append(meta, remove);
      list.append(row);
    }
  }

  async function openSyncManager() {
    const dialog = $('#sync-session-dialog');
    $('#sync-session-error').textContent = '';
    dialog.showModal();
    try {
      const [localSessions, syncedSessions] = await Promise.all([
        api('/api/remote/local-sessions?limit=50'),
        api('/api/remote/synced-sessions'),
      ]);
      state.localSessions = localSessions;
      state.sessions = syncedSessions;
      renderSessions();
      renderSyncManager();
    } catch (error) {
      $('#sync-session-error').textContent = error.message;
    }
  }

  async function addSyncedSession() {
    const threadId = $('#local-session-select').value;
    const selected = state.localSessions.find(session => session.threadId === threadId);
    const name = $('#sync-session-name').value.trim();
    if (!selected || !name) return;
    const button = $('#add-synced-session');
    button.disabled = true;
    $('#sync-session-error').textContent = '';
    try {
      const created = await api('/api/remote/synced-sessions', {
        method: 'POST', body: JSON.stringify({ threadId, name }),
      });
      state.sessions = await api('/api/remote/synced-sessions');
      state.localSessions = await api('/api/remote/local-sessions?limit=50');
      state.selectedSessionId = created.id;
      renderSessions();
      renderSyncManager();
      await selectSession(created.id, true);
      showToast('会话已加入远程同步。');
    } catch (error) {
      $('#sync-session-error').textContent = error.message;
      button.disabled = false;
    }
  }

  async function removeSyncedSession(session) {
    if (!confirm(`移除“${session.name}”的远程同步？原 Codex 会话不会被删除。`)) return;
    try {
      await api(`/api/remote/synced-sessions/${encodeURIComponent(session.id)}`, { method: 'DELETE' });
      state.sessions = await api('/api/remote/synced-sessions');
      state.localSessions = await api('/api/remote/local-sessions?limit=50');
      state.approvals = await api('/api/remote/approvals');
      state.conversationCache.delete(session.id);
      if (state.selectedSessionId === session.id) state.selectedSessionId = state.sessions[0]?.id || '';
      renderSessions();
      renderSyncManager();
      if (state.selectedSessionId) await selectSession(state.selectedSessionId, true);
      else clearConversation();
      showToast('已移除远程同步，原 Codex 会话保持不变。');
    } catch (error) {
      $('#sync-session-error').textContent = error.message;
    }
  }

  function appendInlineMarkup(container, text) {
    const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
    let cursor = 0;
    for (const match of text.matchAll(pattern)) {
      if (match.index > cursor) container.append(document.createTextNode(text.slice(cursor, match.index)));
      const token = match[0];
      const element = document.createElement(token.startsWith('`') ? 'code' : 'strong');
      element.className = token.startsWith('`') ? 'inline-code' : 'inline-strong';
      element.textContent = token.startsWith('`') ? token.slice(1, -1) : token.slice(2, -2);
      container.append(element);
      cursor = match.index + token.length;
    }
    if (cursor < text.length) container.append(document.createTextNode(text.slice(cursor)));
  }

  function splitMarkdownTableRow(line) {
    const value = String(line || '').trim();
    const cells = [];
    let cell = '';
    let escaped = false;
    for (const character of value) {
      if (escaped) {
        cell += character === '|' || character === '\\' ? character : `\\${character}`;
        escaped = false;
      } else if (character === '\\') {
        escaped = true;
      } else if (character === '|') {
        cells.push(cell.trim());
        cell = '';
      } else {
        cell += character;
      }
    }
    if (escaped) cell += '\\';
    cells.push(cell.trim());
    if (value.startsWith('|') && cells[0] === '') cells.shift();
    if (value.endsWith('|') && cells.at(-1) === '') cells.pop();
    return cells;
  }

  function markdownTableAt(lines, index) {
    if (index + 1 >= lines.length || !lines[index].includes('|')) return null;
    const headers = splitMarkdownTableRow(lines[index]);
    const delimiters = splitMarkdownTableRow(lines[index + 1]);
    if (headers.length < 2 || headers.length !== delimiters.length) return null;
    if (!delimiters.every(cell => /^:?-{3,}:?$/.test(cell))) return null;
    const alignments = delimiters.map(cell => {
      if (cell.startsWith(':') && cell.endsWith(':')) return 'center';
      if (cell.endsWith(':')) return 'right';
      return 'left';
    });
    return { headers, alignments };
  }

  function appendMarkdownTableRow(section, cells, tagName, alignments) {
    const row = document.createElement('tr');
    alignments.forEach((alignment, index) => {
      const cell = document.createElement(tagName);
      cell.className = `align-${alignment}`;
      if (tagName === 'th') cell.setAttribute('scope', 'col');
      appendInlineMarkup(cell, cells[index] || '');
      row.append(cell);
    });
    section.append(row);
  }

  function appendRichText(container, text) {
    container.replaceChildren();
    container.classList.add('rich-text');
    const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    let index = 0;
    const blockStart = (line, lineIndex) => (
      /^\s*(```|#{1,4}\s+|[-*+]\s+|\d+[.)]\s+|>\s*|\*\*[^*]+\*\*\s*$|---+\s*$)/.test(line)
      || Boolean(markdownTableAt(lines, lineIndex))
    );
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }
      if (/^\s*```/.test(line)) {
        const language = line.trim().slice(3).trim();
        const codeLines = [];
        index += 1;
        while (index < lines.length && !/^\s*```/.test(lines[index])) {
          codeLines.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const pre = document.createElement('pre');
        pre.className = 'message-code';
        if (language) pre.dataset.language = language;
        const code = document.createElement('code');
        code.textContent = codeLines.join('\n');
        pre.append(code);
        container.append(pre);
        continue;
      }
      const tableDefinition = markdownTableAt(lines, index);
      if (tableDefinition) {
        const table = document.createElement('table');
        table.className = 'message-table';
        const head = document.createElement('thead');
        const body = document.createElement('tbody');
        appendMarkdownTableRow(
          head,
          tableDefinition.headers,
          'th',
          tableDefinition.alignments,
        );
        index += 2;
        while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
          const cells = splitMarkdownTableRow(lines[index]);
          if (cells.length < 2) break;
          appendMarkdownTableRow(body, cells, 'td', tableDefinition.alignments);
          index += 1;
        }
        table.append(head, body);
        const wrapper = document.createElement('div');
        wrapper.className = 'message-table-scroll';
        wrapper.tabIndex = 0;
        wrapper.setAttribute('role', 'region');
        wrapper.setAttribute('aria-label', '消息表格');
        wrapper.append(table);
        container.append(wrapper);
        continue;
      }
      const headingMatch = line.match(/^\s*#{1,4}\s+(.+)$/);
      const boldHeadingMatch = line.match(/^\s*\*\*([^*]+)\*\*\s*$/);
      if (headingMatch || boldHeadingMatch) {
        const heading = document.createElement('h3');
        heading.className = 'message-heading';
        appendInlineMarkup(heading, headingMatch?.[1] || boldHeadingMatch[1]);
        container.append(heading);
        index += 1;
        continue;
      }
      const unorderedMatch = line.match(/^\s*[-*+]\s+(.+)$/);
      const orderedMatch = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (unorderedMatch || orderedMatch) {
        const ordered = Boolean(orderedMatch);
        const list = document.createElement(ordered ? 'ol' : 'ul');
        list.className = 'message-list';
        while (index < lines.length) {
          const match = lines[index].match(ordered ? /^\s*\d+[.)]\s+(.+)$/ : /^\s*[-*+]\s+(.+)$/);
          if (!match) break;
          const item = document.createElement('li');
          appendInlineMarkup(item, match[1]);
          list.append(item);
          index += 1;
        }
        container.append(list);
        continue;
      }
      if (/^\s*>/.test(line)) {
        const quoteLines = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) {
          quoteLines.push(lines[index].replace(/^\s*>\s?/, ''));
          index += 1;
        }
        const quote = document.createElement('blockquote');
        appendInlineMarkup(quote, quoteLines.join('\n'));
        container.append(quote);
        continue;
      }
      if (/^\s*---+\s*$/.test(line)) {
        container.append(document.createElement('hr'));
        index += 1;
        continue;
      }
      const paragraphLines = [line.trim()];
      index += 1;
      while (index < lines.length && lines[index].trim() && !blockStart(lines[index], index)) {
        paragraphLines.push(lines[index].trim());
        index += 1;
      }
      const paragraph = document.createElement('p');
      appendInlineMarkup(paragraph, paragraphLines.join('\n'));
      container.append(paragraph);
    }
    if (!container.childNodes.length) container.textContent = text;
  }

  function appendStreamCaret(body) {
    const textBlocks = [...body.querySelectorAll('p, li, h3, blockquote')];
    const target = textBlocks.at(-1) || body;
    const caret = document.createElement('span');
    caret.className = 'stream-caret';
    caret.setAttribute('aria-hidden', 'true');
    target.append(caret);
  }

  function messageNode(role, label, text, streaming = false) {
    const message = document.createElement('article');
    message.className = `message ${role}${streaming ? ' streaming' : ''}`;
    const heading = document.createElement('span');
    heading.className = 'message-label';
    heading.textContent = label;
    const body = document.createElement('div');
    body.className = 'message-body';
    appendRichText(body, text);
    if (streaming) appendStreamCaret(body);
    message.append(heading, body);
    return message;
  }

  function conversationStatus(detail) {
    return conversationStatusWithActivity(detail, state);
  }

  function conversationStatusWithActivity(detail, activity) {
    const turns = detail?.turns || [];
    const latest = turns.at(-1);
    const latestItem = (latest?.items || []).at(-1);
    const activeStates = ['active', 'inProgress', 'running', 'started'];
    const latestStatus = detail?.latestTurnStatus || latest?.status || '';
    const detailError = typeof detail?.latestTurnError === 'string' ? detail.latestTurnError : '';
    const latestError = (detailError || (typeof latest?.error === 'string' ? latest.error : '')).trim();
    if (latestError) return 'failed';
    if (['failed', 'systemError'].includes(latestStatus)) return latestStatus;
    if (activeStates.includes(latestStatus)) return 'inProgress';
    if (activeStates.includes(latestItem?.status)) return 'inProgress';
    if (latestStatus === 'completed') return 'completed';
    const recentContent = Date.now() - (activity.lastConversationActivityAt || 0) < LIVE_ACTIVITY_GRACE_MS;
    const recentEvent = Date.now() - (activity.lastEventAt || 0) < LIVE_ACTIVITY_GRACE_MS;
    if (recentContent || recentEvent) return 'inProgress';
    if (latestStatus) return latestStatus;
    if ((detail?.activeFlags || []).some(flag => activeStates.includes(flag))) return 'inProgress';
    return detail?.status === 'notLoaded' ? 'idle' : (detail?.status || 'idle');
  }

  function uuidV7Timestamp(value) {
    const compact = String(value || '').replaceAll('-', '');
    if (!/^[0-9a-f]{32}$/i.test(compact) || compact[12].toLowerCase() !== '7') return 0;
    const timestamp = Number.parseInt(compact.slice(0, 12), 16);
    return Number.isFinite(timestamp) ? timestamp : 0;
  }

  function conversationLooksRecentlyActive(detail) {
    const latest = (detail?.turns || []).at(-1);
    const latestStatus = detail?.latestTurnStatus || latest?.status || '';
    const latestError = detail?.latestTurnError || latest?.error || '';
    if (!latest || latestError || ['completed', 'failed', 'interrupted', 'cancelled'].includes(latestStatus)) return false;
    const lastItem = (latest.items || []).at(-1);
    const activeStates = ['active', 'inProgress', 'running', 'started'];
    const terminalStates = ['completed', 'failed', 'interrupted', 'cancelled'];
    if (activeStates.includes(lastItem?.status)) return true;
    if (terminalStates.includes(lastItem?.status)) return false;
    const unfinishedTypes = new Set([
      'reasoning', 'commandExecution', 'fileChange', 'mcpToolCall', 'webSearch',
      'plan', 'subAgentActivity', 'contextCompaction',
    ]);
    const startedAt = uuidV7Timestamp(latest.id);
    const age = Date.now() - startedAt;
    return unfinishedTypes.has(lastItem?.type)
      && startedAt > 0
      && age >= -60000
      && age < INITIAL_LIVE_TURN_MAX_AGE_MS;
  }

  function conversationStatusClass(status) {
    if (status === 'inProgress') return 'running';
    if (status === 'waitingOnApproval') return 'waiting';
    if (status === 'unknown') return 'loading';
    if (['failed', 'interrupted', 'systemError'].includes(status)) return 'failed';
    if (status === 'completed') return 'completed';
    return 'stopped';
  }

  function activityLabel(item) {
    const labels = {
      commandExecution: '执行命令', fileChange: '修改文件', mcpToolCall: '调用工具',
      webSearch: '搜索网页', reasoning: '分析任务', agentMessage: '生成回复',
      plan: '更新计划', userMessage: '收到消息', subAgentActivity: '子任务协作',
      contextCompaction: '整理上下文',
    };
    if (item?.type === 'mcpToolCall') return [item.server, item.tool].filter(Boolean).join(' / ') || labels.mcpToolCall;
    return labels[item?.type] || item?.label || '任务活动';
  }

  function conversationActivity(detail) {
    const turns = detail?.turns || [];
    const latest = turns.at(-1);
    const items = latest?.items || [];
    const item = items.at(-1);
    const status = conversationStatus(detail);
    if (!item) return status === 'inProgress' ? 'Codex 正在处理' : '暂无运行活动';
    if (status === 'inProgress') return activityLabel(item);
    return `最近：${activityLabel(item)}`;
  }

  function conversationMetrics(detail) {
    const latest = (detail?.turns || []).at(-1);
    const items = latest?.items || [];
    const fileCount = items
      .filter(item => item.type === 'fileChange')
      .reduce((total, item) => total + (Number(item.count) || 1), 0);
    const parts = [`本轮 ${items.length} 项`];
    if (fileCount) parts.push(`文件 ${fileCount} 处`);
    return parts.join(' · ');
  }

  function reasoningNode(item, isLatestRunningReasoning = false) {
    const details = document.createElement('details');
    details.className = 'reasoning-trace';
    details.open = isLatestRunningReasoning;
    const summary = document.createElement('summary');
    const signal = document.createElement('span');
    signal.className = `activity-signal${isLatestRunningReasoning ? ' running' : ' completed'}`;
    signal.setAttribute('aria-hidden', 'true');
    const title = document.createElement('span');
    title.textContent = isLatestRunningReasoning ? '正在分析' : '分析摘要';
    summary.append(signal, title);
    const body = document.createElement('div');
    body.className = 'reasoning-body';
    appendRichText(body, item.text);
    details.append(summary, body);
    return details;
  }

  function activityNode(item) {
    const row = document.createElement('div');
    const itemStatus = item.status || 'completed';
    row.className = `activity-row ${itemStatus}`;
    const signal = document.createElement('span');
    signal.className = `activity-signal ${['active', 'inProgress', 'running', 'started'].includes(itemStatus) ? 'running' : itemStatus}`;
    signal.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span');
    label.className = 'activity-label';
    label.textContent = activityLabel(item);
    const status = document.createElement('span');
    status.className = 'activity-status';
    status.textContent = stateLabel(itemStatus);
    row.append(signal, label, status);
    return row;
  }

  function scrollToLatest(behavior = 'smooth') {
    const transcript = $('#transcript');
    state.followTail = true;
    $('#jump-latest').hidden = true;
    transcript.scrollTo({ top: transcript.scrollHeight, behavior });
  }

  function renderConversationLoading(session) {
    if (!session || session.id !== state.selectedSessionId || state.conversation) return;
    $('#conversation-title').textContent = session.name || '远程会话';
    $('#conversation-meta').textContent = session.threadId;
    $('#conversation-status').textContent = '载入中';
    $('#conversation-status').className = 'status-badge loading';
    $('#runtime-strip').className = 'runtime-strip loading';
    $('#runtime-signal').className = 'runtime-signal runtime-rotor loading';
    $('#runtime-activity').textContent = '正在载入会话';
    $('#runtime-detail').textContent = '同步最近的任务回合与运行状态';
    $('#runtime-metrics').textContent = '读取中';
    const transcript = $('#transcript');
    transcript.setAttribute('aria-busy', 'true');
    transcript.replaceChildren();
    const skeleton = document.createElement('div');
    skeleton.className = 'conversation-skeleton';
    skeleton.setAttribute('aria-label', '正在载入会话内容');
    for (const width of ['58%', '92%', '74%', '42%']) {
      const line = document.createElement('span');
      line.style.setProperty('--skeleton-width', width);
      skeleton.append(line);
    }
    transcript.append(skeleton);
  }

  function renderConversationReadError(session, error) {
    $('#conversation-title').textContent = session?.name || '远程会话';
    $('#conversation-meta').textContent = session?.threadId || '会话读取失败';
    $('#conversation-status').textContent = '读取失败';
    $('#conversation-status').className = 'status-badge failed';
    $('#runtime-strip').className = 'runtime-strip failed';
    $('#runtime-signal').className = 'runtime-signal runtime-rotor failed';
    $('#runtime-activity').textContent = '暂时无法读取会话';
    $('#runtime-detail').textContent = '系统会继续自动重试，也可以点击右上角刷新';
    $('#runtime-metrics').textContent = '连接异常';
    const transcript = $('#transcript');
    transcript.setAttribute('aria-busy', 'false');
    transcript.replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'conversation-empty error';
    const title = document.createElement('strong');
    title.textContent = '会话读取失败';
    const detail = document.createElement('span');
    detail.textContent = error?.message || '等待下一次自动同步';
    empty.append(title, detail);
    transcript.append(empty);
  }

  function renderRuntime(detail) {
    const approval = pendingApprovalForSelected();
    const status = approval ? 'waitingOnApproval' : conversationStatus(detail);
    const statusClass = conversationStatusClass(status);
    const running = statusClass === 'running';
    $('#conversation-status').textContent = stateLabel(status);
    $('#conversation-status').className = `status-badge ${statusClass}`;
    $('#runtime-strip').className = `runtime-strip ${statusClass}`;
    $('#runtime-signal').className = `runtime-signal runtime-rotor ${statusClass}`;
    $('#runtime-activity').textContent = approval
      ? 'Codex 正在等待远程授权'
      : conversationActivity(detail);
    const time = state.lastUpdatedAt
      ? new Date(state.lastUpdatedAt).toLocaleTimeString('zh-CN', { hour12: false })
      : '--:--:--';
    const eventHint = state.lastEvent?.itemType ? ` · ${activityLabel({ type: state.lastEvent.itemType })}` : '';
    $('#runtime-detail').textContent = approval
      ? `${approval.title} · ${approval.sessionName}`
      : `${stateLabel(status)} · 最近同步 ${time}${eventHint}`;
    $('#runtime-metrics').textContent = approval
      ? approvalExpiryText(approval)
      : conversationMetrics(detail);
    $('#transcript').setAttribute('aria-busy', String(running));
    renderSessionDrawer();
  }

  function renderConversation(session, detail) {
    clearTimeout(state.conversationLoadingTimer);
    state.conversationLoadingTimer = 0;
    $('#conversation-title').textContent = session.name || '远程会话';
    $('#conversation-meta').textContent = session.threadId;
    const status = pendingApprovalForSelected()
      ? 'waitingOnApproval'
      : conversationStatus(detail);
    $('#message-input').disabled = false;
    $('#send-button').disabled = false;
    $('#attach-image').disabled = false;
    $('#capture-screen').disabled = false;
    renderRuntime(detail);
    const transcript = $('#transcript');
    const previousScrollTop = transcript.scrollTop;
    transcript.replaceChildren();
    const turns = detail.turns || [];
    if (!turns.length) {
      const empty = document.createElement('div');
      empty.className = 'conversation-empty';
      const strong = document.createElement('strong');
      strong.textContent = '会话还没有可显示的消息';
      const span = document.createElement('span');
      span.textContent = '可以从下方直接发送第一条消息';
      empty.append(strong, span);
      transcript.append(empty);
      return;
    }
    turns.forEach((turn, turnIndex) => {
      const section = document.createElement('section');
      section.className = 'turn';
      const divider = document.createElement('div');
      divider.className = 'turn-divider';
      divider.textContent = `${stateLabel(turn.status)} · ${turn.id.slice(0, 8)}`;
      section.append(divider);
      const items = turn.items || [];
      items.forEach((item, itemIndex) => {
        const latestRunningReasoning = status === 'inProgress'
          && turnIndex === turns.length - 1
          && itemIndex === items.length - 1;
        if (item.type === 'userMessage' && item.text) section.append(messageNode('user', '你', item.text));
        else if (['agentMessage', 'plan'].includes(item.type) && item.text) {
          const streaming = status === 'inProgress'
            && turnIndex === turns.length - 1
            && itemIndex === items.length - 1;
          section.append(messageNode('agent', 'Codex', item.text, streaming));
        }
        else if (item.type === 'reasoning' && item.text) section.append(reasoningNode(item, latestRunningReasoning));
        else section.append(activityNode(item));
      });
      if (turn.error) section.append(messageNode('agent', '任务错误', turn.error));
      if (status === 'inProgress' && turnIndex === turns.length - 1) {
        const tail = document.createElement('div');
        tail.className = 'stream-tail';
        const tailSignal = document.createElement('span');
        tailSignal.setAttribute('aria-hidden', 'true');
        const tailLabel = document.createElement('span');
        tailLabel.textContent = 'Codex 正在继续';
        tail.append(tailSignal, tailLabel);
        section.append(tail);
      }
      transcript.append(section);
    });
    requestAnimationFrame(() => {
      if (state.followTail) scrollToLatest('auto');
      else transcript.scrollTop = previousScrollTop;
    });
  }

  function refreshDelay() {
    if (document.visibilityState === 'hidden') return HIDDEN_REFRESH_MS;
    return conversationStatus(state.conversation) === 'inProgress' ? ACTIVE_REFRESH_MS : IDLE_REFRESH_MS;
  }

  function scheduleConversationRefresh(delay = refreshDelay()) {
    clearTimeout(state.conversationRefreshTimer);
    if (!state.selectedSessionId) return;
    state.conversationRefreshTimer = setTimeout(() => refreshSelectedSession(), delay);
  }

  function restoreCachedConversation(session) {
    const cached = state.conversationCache.get(session?.id);
    if (!cached) return false;
    state.conversation = cached.conversation;
    state.conversationSignature = cached.signature;
    state.lastUpdatedAt = cached.lastUpdatedAt;
    state.lastConversationActivityAt = cached.lastConversationActivityAt;
    renderConversation(session, cached.conversation);
    return true;
  }

  async function refreshSelectedSession({ force = false, quiet = true } = {}) {
    if (!state.selectedSessionId) return;
    const sessionId = state.selectedSessionId;
    if (state.conversationRequests.has(sessionId)) return;
    const selectionVersion = state.conversationSelectionVersion;
    state.conversationRequests.add(sessionId);
    if (!quiet) $('#sync-state').textContent = '读取会话中';
    try {
      const result = await api(`/api/remote/sessions/${encodeURIComponent(sessionId)}?turnLimit=6`);
      const signature = JSON.stringify(result.conversation || {});
      const previous = state.conversationCache.get(sessionId);
      const signatureChanged = Boolean(previous?.signature) && signature !== previous.signature;
      const lastConversationActivityAt = signatureChanged || (
        !previous?.signature && conversationLooksRecentlyActive(result.conversation)
      ) ? Date.now() : (previous?.lastConversationActivityAt || 0);
      const lastUpdatedAt = Date.now();
      state.conversationCache.set(sessionId, {
        conversation: result.conversation,
        signature,
        lastUpdatedAt,
        lastConversationActivityAt,
      });
      const session = state.sessions.find(item => item.id === sessionId) || result;
      state.sessionStatusOverrides.set(
        session.threadId,
        conversationStatusWithActivity(result.conversation, {
          lastConversationActivityAt,
          lastEventAt: state.lastEventAt,
        }),
      );
      if (sessionId !== state.selectedSessionId || selectionVersion !== state.conversationSelectionVersion) return;
      clearTimeout(state.conversationLoadingTimer);
      $('#session-select').setAttribute('aria-busy', 'false');
      state.lastConversationActivityAt = lastConversationActivityAt;
      state.lastUpdatedAt = lastUpdatedAt;
      state.conversation = result.conversation;
      if (force || signature !== state.conversationSignature) {
        state.conversationSignature = signature;
        renderConversation(session, result.conversation);
      } else {
        renderRuntime(result.conversation);
      }
      setConnected(true);
    } catch (error) {
      clearTimeout(state.conversationLoadingTimer);
      if (sessionId === state.selectedSessionId && selectionVersion === state.conversationSelectionVersion) {
        $('#session-select').setAttribute('aria-busy', 'false');
        const session = state.sessions.find(item => item.id === sessionId);
        if (!state.conversation || $('#transcript .conversation-skeleton')) renderConversationReadError(session, error);
      }
      if (!quiet) showToast(error.message);
      setConnected(false, '会话读取失败');
    } finally {
      state.conversationRequests.delete(sessionId);
      if (state.selectedSessionId) {
        scheduleConversationRefresh(sessionId === state.selectedSessionId ? refreshDelay() : 80);
      }
    }
  }

  async function selectSession(sessionId, quiet = false) {
    if (state.selectedSessionId && state.selectedSessionId !== sessionId) {
      removePendingImage();
    }
    clearTimeout(state.conversationRefreshTimer);
    clearTimeout(state.conversationLoadingTimer);
    state.conversationSelectionVersion += 1;
    state.selectedSessionId = sessionId;
    state.conversation = null;
    state.conversationSignature = '';
    state.lastEvent = null;
    state.lastEventAt = 0;
    state.lastConversationActivityAt = 0;
    state.followTail = true;
    renderSessions();
    renderApprovals();
    $('#session-select').setAttribute('aria-busy', 'true');
    const session = state.sessions.find(item => item.id === sessionId);
    const restored = restoreCachedConversation(session);
    $('#message-input').disabled = !restored;
    $('#send-button').disabled = !restored;
    $('#attach-image').disabled = !restored;
    $('#capture-screen').disabled = !restored;
    const selectionVersion = state.conversationSelectionVersion;
    if (!restored) {
      state.conversationLoadingTimer = setTimeout(() => {
        if (selectionVersion === state.conversationSelectionVersion) renderConversationLoading(session);
      }, 180);
    }
    await refreshSelectedSession({ force: !restored, quiet });
  }

  function formatDate(value) {
    if (!value) return '从未连接';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
  }

  function renderRemoteAccess(status) {
    state.adminStatus = status;
    window.consoleSidebar?.setProjectSummary(status?.projectSummary);
    const tunnel = status?.tunnel || {};
    const labels = {
      running: ['FRP 隧道运行中', `客户端进程 ${tunnel.pid || '已连接'} · 随控制台启动和关闭`],
      failed: ['FRP 隧道启动失败', tunnel.detail || '查看 .runtime/frp/frpc.log 获取错误信息'],
      stopped: ['FRP 隧道已停止', '本地配置存在，但客户端进程当前未运行'],
      'not-configured': ['FRP 隧道未配置', '添加本地 frpc 程序和私密配置后自动启用'],
    };
    const [label, detail] = labels[tunnel.state] || ['正在检查隧道', '等待本机状态更新'];
    $('#tunnel-state-label').textContent = label;
    $('#tunnel-state-detail').textContent = detail;
    $('#tunnel-provider').textContent = tunnel.provider === 'frp' ? 'FRP' : (tunnel.provider || '未配置');
    $('#tunnel-started-at').textContent = tunnel.startedAt ? formatDate(tunnel.startedAt) : '尚未启动';
    $('#remote-public-url').textContent = status?.baseUrl || '未配置';
    $('#tunnel-status-dot').className = `tunnel-status-dot ${tunnel.state || ''}`;
  }

  async function refreshAdminStatus(updateInput = false) {
    const response = await fetch('/api/remote/status', { cache: 'no-store' });
    if (!response.ok) throw new Error(`远程访问状态读取失败 (${response.status})`);
    const status = await response.json();
    renderRemoteAccess(status);
    if (updateInput) $('#public-base-url').value = status.baseUrl || '';
    $('#codex-state').textContent = status.codexConnected ? 'App Server 已连接' : 'App Server 不可用';
    return status;
  }

  function renderDevices() {
    const list = $('#device-list');
    list.replaceChildren();
    if (!state.devices.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = '尚未配对移动设备。';
      list.append(empty);
      return;
    }
    for (const device of state.devices) {
      const row = document.createElement('div');
      row.className = 'device-row';
      const meta = document.createElement('div');
      meta.className = 'device-meta';
      const name = document.createElement('strong');
      name.textContent = device.name;
      const created = document.createElement('small');
      created.textContent = `配对于 ${formatDate(device.createdAt)}`;
      meta.append(name, created);
      const seen = document.createElement('small');
      seen.textContent = `最近连接 ${formatDate(device.lastSeenAt)}`;
      const revoke = document.createElement('button');
      revoke.type = 'button';
      revoke.className = 'text-button';
      revoke.textContent = '撤销';
      revoke.addEventListener('click', () => revokeDevice(device.id));
      row.append(meta, seen, revoke);
      list.append(row);
    }
  }

  async function revokeDevice(deviceId) {
    try {
      await api(`/api/remote/devices/${encodeURIComponent(deviceId)}`, { method: 'DELETE' });
      state.devices = await api('/api/remote/devices');
      renderDevices();
      showToast('设备授权已撤销。');
    } catch (error) { showToast(error.message); }
  }

  async function loadWorkspaceData() {
    const tasks = [api('/api/remote/sessions'), api('/api/remote/approvals')];
    if (state.admin) tasks.push(api('/api/remote/devices'));
    const [sessions, approvals, devices = []] = await Promise.all(tasks);
    state.sessions = sessions;
    state.approvals = approvals;
    state.devices = devices;
    const activeSessionIds = new Set(sessions.map(session => session.id));
    const activeThreadIds = new Set(sessions.map(session => session.threadId));
    for (const sessionId of state.conversationCache.keys()) {
      if (!activeSessionIds.has(sessionId)) state.conversationCache.delete(sessionId);
    }
    for (const threadId of state.sessionStatusOverrides.keys()) {
      if (!activeThreadIds.has(threadId)) state.sessionStatusOverrides.delete(threadId);
    }
    if (state.selectedSessionId && !sessions.some(item => item.id === state.selectedSessionId)) state.selectedSessionId = '';
    if (!state.selectedSessionId && sessions.length) state.selectedSessionId = sessions[0].id;
    renderSessions();
    if (state.admin) renderDevices();
    if (state.selectedSessionId) await selectSession(state.selectedSessionId, true);
    else clearConversation();
    setConnected(true);
  }

  function renderScreenshotShortcut() {
    const label = formatScreenshotShortcut(state.screenshotShortcut);
    const action = state.admin ? '区域截图' : '截取屏幕';
    $('#screenshot-shortcut-label').textContent = label;
    const captureButton = $('#capture-screen');
    captureButton.title = action + '（' + label + '）';
    captureButton.setAttribute('aria-label', action + '，快捷键 ' + label);
  }

  function openScreenshotShortcutSettings() {
    closeSessionDrawer({ restoreFocus: false });
    state.screenshotShortcutDraft = { ...state.screenshotShortcut };
    $('#screenshot-shortcut-input').value = formatScreenshotShortcut(
      state.screenshotShortcutDraft
    );
    $('#screenshot-shortcut-error').textContent = '';
    const dialog = $('#screenshot-shortcut-dialog');
    dialog.showModal();
    requestAnimationFrame(() => $('#screenshot-shortcut-input').focus());
  }

  function recordScreenshotShortcut(event) {
    if (event.key === 'Tab') return;
    if (event.key === 'Escape') {
      $('#screenshot-shortcut-dialog').close('cancel');
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    const shortcut = screenshotShortcutFromEvent(event);
    if (shortcut === null) return;
    if (shortcut === false) {
      $('#screenshot-shortcut-error').textContent = '快捷键需包含 Ctrl、Alt 或 Command。';
      return;
    }
    state.screenshotShortcutDraft = shortcut;
    $('#screenshot-shortcut-input').value = formatScreenshotShortcut(shortcut);
    $('#screenshot-shortcut-error').textContent = '';
  }

  function resetScreenshotShortcut() {
    state.screenshotShortcutDraft = { ...DEFAULT_SCREENSHOT_SHORTCUT };
    $('#screenshot-shortcut-input').value = formatScreenshotShortcut(
      state.screenshotShortcutDraft
    );
    $('#screenshot-shortcut-error').textContent = '';
  }

  function saveScreenshotShortcut() {
    storeScreenshotShortcut(state.screenshotShortcutDraft || DEFAULT_SCREENSHOT_SHORTCUT);
    renderScreenshotShortcut();
    $('#screenshot-shortcut-dialog').close('saved');
    showToast(`截图快捷键已设为 ${formatScreenshotShortcut(state.screenshotShortcut)}。`);
  }

  function loadScreenshot(file) {
    return new Promise((resolve, reject) => {
      const objectUrl = URL.createObjectURL(file);
      const image = new Image();
      image.onload = () => {
        URL.revokeObjectURL(objectUrl);
        resolve(image);
      };
      image.onerror = () => {
        URL.revokeObjectURL(objectUrl);
        reject(new Error('\u65e0\u6cd5\u8bfb\u53d6\u8fd9\u5f20\u622a\u56fe\u3002'));
      };
      image.src = objectUrl;
    });
  }

  function renderPendingImage() {
    const preview = $('#attachment-preview');
    if (!state.pendingImage) {
      preview.hidden = true;
      $('#attachment-thumbnail').removeAttribute('src');
      $('#attachment-name').textContent = '';
      return;
    }
    $('#attachment-thumbnail').src = state.pendingImage.dataUrl;
    $('#attachment-name').textContent = state.pendingImage.label;
    preview.hidden = false;
  }

  function removePendingImage() {
    state.pendingImage = null;
    $('#image-input').value = '';
    renderPendingImage();
  }

  async function prepareScreenshot(file) {
    if (!file) return;
    if (!SCREENSHOT_TYPES.has(file.type)) {
      throw new Error('\u4ec5\u652f\u6301 PNG\u3001JPEG \u6216 WebP \u622a\u56fe\u3002');
    }
    if (file.size > SCREENSHOT_MAX_SOURCE_BYTES) {
      throw new Error('\u539f\u59cb\u622a\u56fe\u8fc7\u5927\uff0c\u8bf7\u88c1\u526a\u540e\u91cd\u8bd5\u3002');
    }
    const image = await loadScreenshot(file);
    const sourceWidth = image.naturalWidth || image.width;
    const sourceHeight = image.naturalHeight || image.height;
    if (!sourceWidth || !sourceHeight) {
      throw new Error('\u622a\u56fe\u5c3a\u5bf8\u65e0\u6548\u3002');
    }
    let scale = Math.min(1, SCREENSHOT_MAX_DIMENSION / Math.max(sourceWidth, sourceHeight));
    let dataUrl = '';
    for (let resizeAttempt = 0; resizeAttempt < 4 && !dataUrl; resizeAttempt += 1) {
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(sourceWidth * scale));
      canvas.height = Math.max(1, Math.round(sourceHeight * scale));
      const context = canvas.getContext('2d', { alpha: false });
      if (!context) throw new Error('\u5f53\u524d\u6d4f\u89c8\u5668\u65e0\u6cd5\u5904\u7406\u622a\u56fe\u3002');
      context.fillStyle = '#ffffff';
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      for (const quality of [0.86, 0.74, 0.62, 0.52]) {
        const candidate = canvas.toDataURL('image/jpeg', quality);
        if (candidate.length <= SCREENSHOT_MAX_DATA_URL_LENGTH) {
          dataUrl = candidate;
          break;
        }
      }
      scale *= 0.78;
    }
    if (!dataUrl) {
      throw new Error('\u622a\u56fe\u538b\u7f29\u540e\u4ecd\u8fc7\u5927\uff0c\u8bf7\u88c1\u526a\u540e\u91cd\u8bd5\u3002');
    }
    const encodedLength = dataUrl.length - dataUrl.indexOf(',') - 1;
    const approximateBytes = Math.ceil(encodedLength * 0.75);
    state.pendingImage = {
      dataUrl,
      label: `${file.name || '\u622a\u56fe'} \u00b7 ${Math.max(1, Math.round(approximateBytes / 1024))} KB`,
    };
    renderPendingImage();
  }

  function clipboardImageFile(event) {
    const items = Array.from(event.clipboardData?.items || []);
    const imageItem = items.find(item => item.kind === 'file' && item.type.startsWith('image/'));
    return imageItem?.getAsFile() || null;
  }

  async function handleComposerPaste(event) {
    const file = clipboardImageFile(event);
    if (!file) return;
    event.preventDefault();
    const hadPendingImage = Boolean(state.pendingImage);
    const attachButton = $('#attach-image');
    const captureButton = $('#capture-screen');
    attachButton.disabled = true;
    captureButton.disabled = true;
    try {
      await prepareScreenshot(file);
      showToast(hadPendingImage ? '已替换待发送截图。' : '已粘贴截图，可随消息发送。');
    } catch (error) {
      showToast(error.message);
    } finally {
      const disabled = !state.selectedSessionId;
      attachButton.disabled = disabled;
      captureButton.disabled = disabled;
    }
  }

  async function captureNativeRegionScreenshot() {
    const result = await api('/api/remote/native-screenshot', {
      method: 'POST',
      body: JSON.stringify({}),
    });
    if (!result?.captured) return false;
    if (
      typeof result.image !== 'string'
      || !result.image.startsWith('data:image/png;base64,')
    ) {
      throw new Error('区域截图服务返回了无效图片。');
    }
    const response = await fetch(result.image);
    const blob = await response.blob();
    const file = new File(
      [blob],
      typeof result.name === 'string' ? result.name : 'region.png',
      { type: 'image/png' },
    );
    await prepareScreenshot(file);
    return true;
  }

  async function captureScreenScreenshot() {
    if (state.capturingScreen || !state.selectedSessionId) return;
    if (!state.admin && !navigator.mediaDevices?.getDisplayMedia) {
      showToast('当前浏览器不支持屏幕截图，请直接粘贴图片或使用 + 选择文件。');
      return;
    }
    const button = $('#capture-screen');
    const attachButton = $('#attach-image');
    let stream = null;
    state.capturingScreen = true;
    button.disabled = true;
    button.classList.add('capturing');
    button.setAttribute('aria-busy', 'true');
    attachButton.disabled = true;
    try {
      if (state.admin) {
        const captured = await captureNativeRegionScreenshot();
        showToast(captured ? '已截取选定区域，可随消息发送。' : '已取消截图。');
        return;
      }
      stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
      const video = document.createElement('video');
      video.muted = true;
      video.playsInline = true;
      video.srcObject = stream;
      await new Promise((resolve, reject) => {
        if (video.readyState >= 1) {
          resolve();
          return;
        }
        video.onloadedmetadata = resolve;
        video.onerror = () => reject(new Error('无法读取选择的屏幕画面。'));
      });
      await video.play();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const width = video.videoWidth;
      const height = video.videoHeight;
      if (!width || !height) throw new Error('选择的屏幕画面尺寸无效。');
      const canvas = document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext('2d', { alpha: false });
      if (!context) throw new Error('当前浏览器无法处理屏幕截图。');
      context.drawImage(video, 0, 0, width, height);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
      if (!blob) throw new Error('无法生成屏幕截图。');
      video.srcObject = null;
      stream.getTracks().forEach(track => track.stop());
      stream = null;
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
      const file = new File([blob], `screen-${timestamp}.png`, { type: 'image/png' });
      await prepareScreenshot(file);
      showToast('已截取当前画面，可随消息发送。');
    } catch (error) {
      if (error?.name !== 'AbortError') {
        const fallback = ['NotAllowedError', 'SecurityError'].includes(error?.name)
          ? '未取得屏幕画面，请直接粘贴图片或使用 + 选择文件。'
          : error.message;
        showToast(fallback);
      }
    } finally {
      stream?.getTracks().forEach(track => track.stop());
      state.capturingScreen = false;
      button.classList.remove('capturing');
      button.setAttribute('aria-busy', 'false');
      const disabled = !state.selectedSessionId;
      button.disabled = disabled;
      attachButton.disabled = disabled;
    }
  }

  async function sendMessage(event) {
    event.preventDefault();
    const input = $('#message-input');
    const message = input.value.trim();
    if ((!message && !state.pendingImage) || !state.selectedSessionId) return;
    const button = $('#send-button');
    button.disabled = true;
    $('#attach-image').disabled = true;
    $('#capture-screen').disabled = true;
    state.followTail = true;
    state.lastConversationActivityAt = Date.now();
    const selectedSession = state.sessions.find(item => item.id === state.selectedSessionId);
    if (selectedSession) state.sessionStatusOverrides.set(selectedSession.threadId, 'inProgress');
    renderSessionDrawer();
    $('#runtime-strip').className = 'runtime-strip running';
    $('#runtime-signal').className = 'runtime-signal runtime-rotor running';
    $('#runtime-activity').textContent = '等待 Codex 响应';
    $('#runtime-detail').textContent = '消息已提交，正在确认新的任务轮次';
    $('#transcript').setAttribute('aria-busy', 'true');
    scrollToLatest('smooth');
    try {
      const result = await api(`/api/remote/sessions/${encodeURIComponent(state.selectedSessionId)}/messages`, {
        method: 'POST',
        body: JSON.stringify({ message, image: state.pendingImage?.dataUrl || null }),
      });
      input.value = '';
      input.style.height = '';
      removePendingImage();
      showToast(result.delivery === 'steered' ? '消息已加入当前运行中的任务。' : '消息已启动新的任务轮次。');
      clearTimeout(state.conversationRefreshTimer);
      scheduleConversationRefresh(150);
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
      $('#attach-image').disabled = !state.selectedSessionId;
      $('#capture-screen').disabled = !state.selectedSessionId;
      input.focus();
    }
  }

  function scheduleEventRefresh(events = []) {
    updateSessionStatusesFromEvents(events);
    const approvalChanged = events.some(event => event.method?.startsWith('remote/approval'));
    if (approvalChanged) loadApprovals().catch(error => showToast(error.message));
    const selected = state.sessions.find(session => session.id === state.selectedSessionId);
    const relevant = [...events].reverse().find(event => event.threadId === selected?.threadId);
    if (!relevant) return;
    state.lastEvent = relevant;
    const terminalTurn = relevant.method === 'turn/completed';
    const approvalEvent = relevant.method?.startsWith('remote/approval');
    const eventTime = Date.parse(relevant.timestamp || '');
    state.lastEventAt = !terminalTurn && !approvalEvent && Number.isFinite(eventTime) ? eventTime : 0;
    renderSessionDrawer();
    clearTimeout(state.conversationRefreshTimer);
    scheduleConversationRefresh(60);
  }

  async function pollEvents() {
    if (state.polling) return;
    state.polling = true;
    while (state.polling && (state.admin || state.token)) {
      try {
        const batch = await api(`/api/remote/events?after=${state.cursor}&timeout=25`);
        state.cursor = batch.cursor;
        if (batch.events?.length) scheduleEventRefresh(batch.events);
        setConnected(true);
      } catch (error) {
        setConnected(false);
        if (error.authorizationFailed && !state.admin) {
          state.polling = false;
          showPairScreen(error.message);
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 1600));
      }
    }
  }

  async function createPairing() {
    const button = $('#create-pairing');
    button.disabled = true;
    try {
      const result = await api('/api/remote/pairings', {
        method: 'POST',
        body: JSON.stringify({ baseUrl: $('#public-base-url').value.trim() }),
      });
      state.pairingUrl = result.pairingUrl;
      $('#copy-pairing-link').disabled = false;
      const code = qrcode(0, 'M');
      code.addData(result.pairingUrl);
      code.make();
      $('#pairing-qr').src = code.createDataURL(5, 8);
      $('#desktop-comparison-code').textContent = result.comparisonCode;
      $('#qr-result').hidden = false;
      const expiresAt = new Date(result.expiresAt).getTime();
      clearInterval(createPairing.timer);
      const updateExpiry = () => {
        const seconds = Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
        $('#pairing-expiry').textContent = seconds ? `${seconds} 秒后失效` : '二维码已失效';
        if (!seconds) {
          clearInterval(createPairing.timer);
          state.pairingUrl = '';
          $('#copy-pairing-link').disabled = true;
        }
      };
      updateExpiry();
      createPairing.timer = setInterval(updateExpiry, 1000);
    } catch (error) {
      showToast(error.message);
    } finally { button.disabled = false; }
  }

  async function copyPairingLink() {
    if (!state.pairingUrl) return;
    try {
      await navigator.clipboard.writeText(state.pairingUrl);
      showToast('配对链接已复制。');
    } catch {
      showToast('无法访问剪贴板，请直接使用二维码。');
    }
  }

  function openSessionDrawer() {
    if (state.drawerOpen) return;
    state.drawerOpen = true;
    const modal = drawerUsesModalOverlay();
    state.drawerReturnFocus = modal ? document.activeElement : null;
    storeDrawerOpenPreference(true);
    syncSessionDrawerAccessibility();
    if (modal) requestAnimationFrame(() => $('#close-session-drawer').focus({ preventScroll: true }));
  }

  function closeSessionDrawer({ restoreFocus = true } = {}) {
    if (!state.drawerOpen) return;
    const modal = drawerUsesModalOverlay();
    state.drawerOpen = false;
    storeDrawerOpenPreference(false);
    syncSessionDrawerAccessibility();
    if (restoreFocus && modal) {
      const target = state.drawerReturnFocus?.isConnected
        ? state.drawerReturnFocus
        : $('#open-session-drawer');
      target.focus({ preventScroll: true });
    }
    state.drawerReturnFocus = null;
  }

  function readDrawerOpenPreference() {
    try { return localStorage.getItem(DRAWER_STATE_KEY) === 'true'; } catch { return false; }
  }

  function storeDrawerOpenPreference(open) {
    try { localStorage.setItem(DRAWER_STATE_KEY, String(Boolean(open))); } catch {}
  }

  function drawerUsesModalOverlay() {
    return matchMedia('(max-width: 760px)').matches;
  }

  function syncSessionDrawerAccessibility() {
    const drawer = $('#session-drawer');
    const modal = drawerUsesModalOverlay();
    document.body.classList.toggle('drawer-open', state.drawerOpen);
    drawer.setAttribute('role', modal ? 'dialog' : 'complementary');
    drawer.setAttribute('aria-hidden', String(!state.drawerOpen));
    if (modal && state.drawerOpen) drawer.setAttribute('aria-modal', 'true');
    else drawer.removeAttribute('aria-modal');
    $('#open-session-drawer').setAttribute('aria-expanded', String(state.drawerOpen));
  }

  function setupSessionDrawerGestures() {
    const drawer = $('#session-drawer');
    drawer.addEventListener('touchstart', event => {
      const touch = event.changedTouches[0];
      state.drawerTouchStart = touch ? { x: touch.clientX, y: touch.clientY } : null;
    }, { passive: true });
    drawer.addEventListener('touchend', event => {
      const start = state.drawerTouchStart;
      state.drawerTouchStart = null;
      const touch = event.changedTouches[0];
      if (!start || !touch) return;
      const deltaX = touch.clientX - start.x;
      const deltaY = touch.clientY - start.y;
      if (deltaX < -60 && Math.abs(deltaY) < 80) closeSessionDrawer();
    }, { passive: true });

    document.addEventListener('keydown', event => {
      if (!state.drawerOpen) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        closeSessionDrawer();
        return;
      }
      if (event.key !== 'Tab' || !drawerUsesModalOverlay()) return;
      const focusable = $$(
        '#session-drawer a[href], #session-drawer button:not([disabled]):not([hidden]), '
        + '#session-drawer input:not([disabled]), #session-drawer select:not([disabled]), '
        + '#session-drawer textarea:not([disabled]), #session-drawer [tabindex]:not([tabindex="-1"])'
      ).filter(element => !element.hidden && element.getClientRects().length > 0);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
  }

  function switchView(view) {
    state.currentView = view === 'devices' && state.admin ? 'devices' : 'sessions';
    $$('.drawer-tool[data-view]').forEach(tool => {
      const active = tool.dataset.view === state.currentView;
      tool.classList.toggle('active', active);
      tool.setAttribute('aria-pressed', String(active));
    });
    $$('[data-view-panel]').forEach(panel => {
      const active = panel.dataset.viewPanel === state.currentView;
      panel.classList.toggle('active', active);
      panel.hidden = !active;
    });
    const session = state.sessions.find(item => item.id === state.selectedSessionId);
    $('#conversation-title').textContent = state.currentView === 'devices'
      ? '配对设备'
      : (session?.name || '远程会话');
    if (drawerUsesModalOverlay()) closeSessionDrawer();
  }

  function setupInteractions() {
    $('#open-session-drawer').addEventListener('click', () => {
      if (state.drawerOpen) closeSessionDrawer();
      else openSessionDrawer();
    });
    $('#close-session-drawer').addEventListener('click', () => closeSessionDrawer());
    $('#session-drawer-backdrop').addEventListener('click', () => closeSessionDrawer());
    $('#remote-theme-toggle').addEventListener('click', toggleRemoteTheme);
    $$('.drawer-tool[data-view]').forEach(tool => {
      tool.addEventListener('click', () => switchView(tool.dataset.view));
    });
    setupSessionDrawerGestures();
    matchMedia('(max-width: 760px)').addEventListener('change', syncSessionDrawerAccessibility);
    $('#start-qr-scan').addEventListener('click', startQrScanner);
    $('#stop-qr-scan').addEventListener('click', stopQrScanner);
    $('#reset-pairing').addEventListener('click', resetPairing);
    $('#pair-link-form').addEventListener('submit', event => {
      event.preventDefault();
      acceptPairingUrl($('#pair-link-input').value);
    });
    $('#claim-pairing').addEventListener('click', claimPairing);
    $('#confirm-pairing').addEventListener('click', enterAfterPairing);
    $('#create-pairing').addEventListener('click', createPairing);
    $('#copy-pairing-link').addEventListener('click', copyPairingLink);
    $('#composer').addEventListener('submit', sendMessage);
    $('#attach-image').addEventListener('click', () => $('#image-input').click());
    $('#capture-screen').addEventListener('click', captureScreenScreenshot);
    $('#image-input').addEventListener('change', async event => {
      const button = $('#attach-image');
      const captureButton = $('#capture-screen');
      button.disabled = true;
      captureButton.disabled = true;
      try {
        await prepareScreenshot(event.target.files?.[0]);
      } catch (error) {
        removePendingImage();
        showToast(error.message);
      } finally {
        const disabled = !state.selectedSessionId;
        button.disabled = disabled;
        captureButton.disabled = disabled;
      }
    });
    $('#remove-attachment').addEventListener('click', removePendingImage);
    $('#screenshot-shortcut-settings').addEventListener('click', openScreenshotShortcutSettings);
    $('#screenshot-shortcut-input').addEventListener('keydown', recordScreenshotShortcut);
    $('#reset-screenshot-shortcut').addEventListener('click', resetScreenshotShortcut);
    $('#save-screenshot-shortcut').addEventListener('click', saveScreenshotShortcut);
    $('#screenshot-shortcut-dialog').addEventListener('click', event => {
      if (event.target === event.currentTarget) event.currentTarget.close('cancel');
    });
    $('#screenshot-shortcut-dialog').addEventListener('close', () => {
      $('#open-session-drawer').focus({ preventScroll: true });
    });
    $('#approval-view-session').addEventListener('click', viewApprovalSession);
    $('#approval-decline').addEventListener('click', () => resolveApproval('decline'));
    $('#approval-accept-session').addEventListener('click', () => resolveApproval('acceptForTurn'));
    $('#approval-accept').addEventListener('click', () => resolveApproval('accept'));
    $('#session-select').addEventListener('change', event => {
      if (event.target.value) selectSession(event.target.value);
      else {
        state.selectedSessionId = '';
        clearConversation();
      }
    });
    $('#manage-synced-sessions').addEventListener('click', () => {
      closeSessionDrawer({ restoreFocus: false });
      openSyncManager();
    });
    $('#local-session-select').addEventListener('change', event => {
      const selected = state.localSessions.find(session => session.threadId === event.target.value);
      $('#sync-session-name').value = selected?.name || '';
      $('#add-synced-session').disabled = !selected;
    });
    $('#sync-session-name').addEventListener('input', event => {
      $('#add-synced-session').disabled = !$('#local-session-select').value || !event.target.value.trim();
    });
    $('#add-synced-session').addEventListener('click', addSyncedSession);
    $('#refresh-button').addEventListener('click', () => loadWorkspaceData().catch(error => showToast(error.message)));
    $('#jump-latest').addEventListener('click', () => scrollToLatest());
    $('#transcript').addEventListener('scroll', event => {
      const transcript = event.currentTarget;
      const distance = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight;
      state.followTail = distance < 72;
      $('#jump-latest').hidden = state.followTail;
    }, { passive: true });
    $('#message-input').addEventListener('input', event => {
      event.target.style.height = 'auto';
      const viewportHeight = window.visualViewport?.height || window.innerHeight;
      const maximumHeight = Math.min(160, Math.max(72, Math.round(viewportHeight * .32)));
      event.target.style.height = `${Math.min(event.target.scrollHeight, maximumHeight)}px`;
    });
    $('#message-input').addEventListener('paste', handleComposerPaste);
    document.addEventListener('keydown', event => {
      if (
        event.repeat
        || event.defaultPrevented
        || $('#screenshot-shortcut-dialog').open
        || !matchesScreenshotShortcut(event)
      ) return;
      event.preventDefault();
      captureScreenScreenshot();
    });
    addEventListener('storage', event => {
      if (event.key === THEME_KEY && ['light', 'dark'].includes(event.newValue)) applyTheme(event.newValue);
      if (event.key === SCREENSHOT_SHORTCUT_KEY) {
        state.screenshotShortcut = readScreenshotShortcut();
        renderScreenshotShortcut();
      }
    });
    addEventListener('online', () => {
      setConnected(true);
      if (!state.polling && (state.admin || state.token)) startWorkspace({ quiet: true });
    });
    addEventListener('offline', () => setConnected(false, { immediate: true }));
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') stopQrScanner();
      if (!state.selectedSessionId) return;
      clearTimeout(state.conversationRefreshTimer);
      scheduleConversationRefresh(document.visibilityState === 'hidden' ? HIDDEN_REFRESH_MS : 80);
    });
  }

  function scheduleWorkspaceRetry() {
    clearTimeout(state.workspaceRetryTimer);
    state.workspaceRetryTimer = setTimeout(
      () => startWorkspace({ quiet: true }),
      WORKSPACE_RETRY_MS,
    );
  }

  async function startWorkspace(options = {}) {
    clearTimeout(state.workspaceRetryTimer);
    $('#app-shell').hidden = false;
    $('#pair-screen').hidden = true;
    try {
      await loadWorkspaceData();
      pollEvents();
    } catch (error) {
      if (!state.admin && error.authorizationFailed) showPairScreen(error.message);
      else {
        setConnected(false);
        if (!options.quiet) showToast('连接暂时波动，工作台正在自动重试。');
        scheduleWorkspaceRetry();
      }
    }
  }

  async function initialize() {
    setupVisualViewport();
    renderRemoteTheme();
    renderScreenshotShortcut();
    state.drawerOpen = readDrawerOpenPreference();
    syncSessionDrawerAccessibility();
    setupInteractions();
    parsePairingLink();
    state.token = readToken();
    state.device = readStoredDevice();
    const pendingPairingRestored = restorePendingPairing();
    let status = null;
    try {
      status = await refreshAdminStatus(true);
    } catch {}
    state.admin = Boolean(status);
    document.body.classList.toggle('admin-mode', state.admin);
    document.body.classList.toggle('remote-mode', !state.admin);
    renderScreenshotShortcut();
    renderDeviceIdentity();
    $$('.admin-only').forEach(element => {
      if (!state.admin) element.hidden = true;
      else if (!element.hasAttribute('data-view-panel')) element.hidden = false;
    });
    if (state.admin) {
      await startWorkspace();
      setInterval(() => refreshAdminStatus().catch(() => {}), 5000);
    } else if (state.token) {
      try {
        storeRememberedDevice(await api('/api/remote/device'));
        renderDeviceIdentity();
        await startWorkspace();
      } catch (error) {
        if (error.authorizationFailed) showPairScreen(error.message);
        else {
          $('#app-shell').hidden = false;
          $('#pair-screen').hidden = true;
          setConnected(false);
          scheduleWorkspaceRetry();
        }
      }
    } else if (pendingPairingRestored) {
      showPairScreen();
    } else {
      showPairScreen();
    }
    if ('serviceWorker' in navigator && window.isSecureContext) {
      navigator.serviceWorker.addEventListener('controllerchange', () => {
        if (state.qrStream || state.pairingSecret || state.pendingToken) return;
        const reloadKey = 'localhost-project-console.remote-worker-reloaded';
        if (sessionStorage.getItem(reloadKey)) return;
        sessionStorage.setItem(reloadKey, '1');
        location.reload();
      });
      navigator.serviceWorker.register('/service-worker.js')
        .then(registration => registration.update())
        .catch(() => {});
    }
  }

  initialize();
})();
