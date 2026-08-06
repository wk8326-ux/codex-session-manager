(() => {
  const TOKEN_KEY = 'localhost-project-console.remote-token';
  const DEVICE_KEY = 'localhost-project-console.remote-device';
  const PENDING_PAIRING_KEY = 'localhost-project-console.remote-pending-pairing';
  const ACTIVE_REFRESH_MS = 1200;
  const IDLE_REFRESH_MS = 5000;
  const HIDDEN_REFRESH_MS = 12000;
  const LIVE_ACTIVITY_GRACE_MS = 180000;
  const INITIAL_LIVE_TURN_MAX_AGE_MS = 7200000;
  const CONNECTION_FAILURE_THRESHOLD = 3;
  const CONNECTION_FAILURE_GRACE_MS = 8000;
  const WORKSPACE_RETRY_MS = 2500;
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
    conversationRefreshInFlight: false,
    followTail: true,
    lastUpdatedAt: 0,
    lastEvent: null,
    lastEventAt: 0,
    lastConversationActivityAt: 0,
    adminStatus: null,
    connectionFailureCount: 0,
    connectionFailureStartedAt: 0,
    workspaceRetryTimer: 0,
  };

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
    identity.hidden = state.admin || !device?.name;
    identity.textContent = device?.name ? `已配对设备 · ${device.name}` : '';
  }

  function showToast(message) {
    const toast = $('#toast');
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => { toast.hidden = true; }, 3200);
  }

  function setConnected(connected, options = {}) {
    if (connected) {
      state.connectionFailureCount = 0;
      state.connectionFailureStartedAt = 0;
      $('#offline-banner').hidden = true;
      $('#sync-state').textContent = '已同步';
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
      running: '正在运行', started: '正在运行', online: '在线', stopped: '已停止', offline: '离线',
    };
    return labels[value] || value || '未知';
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
      option.textContent = session.name;
      select.append(option);
    }
    select.disabled = state.sessions.length === 0;
    select.value = state.selectedSessionId;
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
    $('#conversation-meta').textContent = '尚未选择会话';
    $('#conversation-status').textContent = '未选择';
    $('#conversation-status').className = 'status-badge stopped';
    $('#session-select').setAttribute('aria-busy', 'false');
    $('#message-input').disabled = true;
    $('#send-button').disabled = true;
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

  function appendRichText(container, text) {
    container.replaceChildren();
    container.classList.add('rich-text');
    const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    let index = 0;
    const blockStart = line => /^\s*(```|#{1,4}\s+|[-*+]\s+|\d+[.)]\s+|>\s*|\*\*[^*]+\*\*\s*$|---+\s*$)/.test(line);
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
      while (index < lines.length && lines[index].trim() && !blockStart(lines[index])) {
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
    const turns = detail?.turns || [];
    const latest = turns.at(-1);
    const latestItem = (latest?.items || []).at(-1);
    const activeStates = ['active', 'inProgress', 'running', 'started'];
    if (activeStates.includes(latest?.status)) return 'inProgress';
    if (activeStates.includes(latestItem?.status)) return 'inProgress';
    if ((detail?.activeFlags || []).some(flag => activeStates.includes(flag))) return 'inProgress';
    if (activeStates.includes(detail?.status)) return 'inProgress';
    if (latest?.status === 'completed') return 'completed';
    const recentContent = Date.now() - state.lastConversationActivityAt < LIVE_ACTIVITY_GRACE_MS;
    const recentEvent = Date.now() - state.lastEventAt < LIVE_ACTIVITY_GRACE_MS;
    if (recentContent || recentEvent) return 'inProgress';
    if (latest?.status) return latest.status;
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
    if (!latest || latest.error || latest.status === 'completed') return false;
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
    const status = conversationStatus(detail);
    const statusClass = conversationStatusClass(status);
    const running = statusClass === 'running';
    $('#conversation-status').textContent = stateLabel(status);
    $('#conversation-status').className = `status-badge ${statusClass}`;
    $('#runtime-strip').className = `runtime-strip ${statusClass}`;
    $('#runtime-signal').className = `runtime-signal runtime-rotor ${statusClass}`;
    $('#runtime-activity').textContent = conversationActivity(detail);
    const time = state.lastUpdatedAt
      ? new Date(state.lastUpdatedAt).toLocaleTimeString('zh-CN', { hour12: false })
      : '--:--:--';
    const eventHint = state.lastEvent?.itemType ? ` · ${activityLabel({ type: state.lastEvent.itemType })}` : '';
    $('#runtime-detail').textContent = `${stateLabel(status)} · 最近同步 ${time}${eventHint}`;
    $('#runtime-metrics').textContent = conversationMetrics(detail);
    $('#transcript').setAttribute('aria-busy', String(running));
  }

  function renderConversation(session, detail) {
    clearTimeout(state.conversationLoadingTimer);
    state.conversationLoadingTimer = 0;
    $('#conversation-meta').textContent = session.threadId;
    const status = conversationStatus(detail);
    $('#message-input').disabled = false;
    $('#send-button').disabled = false;
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

  async function refreshSelectedSession({ force = false, quiet = true } = {}) {
    if (!state.selectedSessionId || state.conversationRefreshInFlight) return;
    const sessionId = state.selectedSessionId;
    const selectionVersion = state.conversationSelectionVersion;
    state.conversationRefreshInFlight = true;
    if (!quiet) $('#sync-state').textContent = '读取会话中';
    try {
      const result = await api(`/api/remote/sessions/${encodeURIComponent(sessionId)}?turnLimit=6`);
      if (sessionId !== state.selectedSessionId || selectionVersion !== state.conversationSelectionVersion) return;
      clearTimeout(state.conversationLoadingTimer);
      $('#session-select').setAttribute('aria-busy', 'false');
      const session = state.sessions.find(item => item.id === sessionId) || result;
      const signature = JSON.stringify(result.conversation || {});
      const signatureChanged = Boolean(state.conversationSignature)
        && signature !== state.conversationSignature;
      if (signatureChanged || (
        !state.conversationSignature && conversationLooksRecentlyActive(result.conversation)
      )) {
        state.lastConversationActivityAt = Date.now();
      }
      state.lastUpdatedAt = Date.now();
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
      state.conversationRefreshInFlight = false;
      if (state.selectedSessionId) {
        scheduleConversationRefresh(sessionId === state.selectedSessionId ? refreshDelay() : 80);
      }
    }
  }

  async function selectSession(sessionId, quiet = false) {
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
    $('#session-select').setAttribute('aria-busy', 'true');
    $('#message-input').disabled = true;
    $('#send-button').disabled = true;
    const session = state.sessions.find(item => item.id === sessionId);
    const selectionVersion = state.conversationSelectionVersion;
    state.conversationLoadingTimer = setTimeout(() => {
      if (selectionVersion === state.conversationSelectionVersion) renderConversationLoading(session);
    }, 180);
    await refreshSelectedSession({ force: true, quiet });
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
    const tasks = [api('/api/remote/sessions')];
    if (state.admin) tasks.push(api('/api/remote/devices'));
    const [sessions, devices = []] = await Promise.all(tasks);
    state.sessions = sessions;
    state.devices = devices;
    if (state.selectedSessionId && !sessions.some(item => item.id === state.selectedSessionId)) state.selectedSessionId = '';
    if (!state.selectedSessionId && sessions.length) state.selectedSessionId = sessions[0].id;
    renderSessions();
    if (state.admin) renderDevices();
    if (state.selectedSessionId) await selectSession(state.selectedSessionId, true);
    else clearConversation();
    setConnected(true);
  }

  async function sendMessage(event) {
    event.preventDefault();
    const input = $('#message-input');
    const message = input.value.trim();
    if (!message || !state.selectedSessionId) return;
    const button = $('#send-button');
    button.disabled = true;
    state.followTail = true;
    state.lastConversationActivityAt = Date.now();
    $('#runtime-strip').className = 'runtime-strip running';
    $('#runtime-signal').className = 'runtime-signal runtime-rotor running';
    $('#runtime-activity').textContent = '等待 Codex 响应';
    $('#runtime-detail').textContent = '消息已提交，正在确认新的任务轮次';
    $('#transcript').setAttribute('aria-busy', 'true');
    scrollToLatest('smooth');
    try {
      const result = await api(`/api/remote/sessions/${encodeURIComponent(state.selectedSessionId)}/messages`, {
        method: 'POST', body: JSON.stringify({ message }),
      });
      input.value = '';
      input.style.height = '';
      showToast(result.delivery === 'steered' ? '消息已加入当前运行中的任务。' : '消息已启动新的任务轮次。');
      clearTimeout(state.conversationRefreshTimer);
      scheduleConversationRefresh(150);
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
      input.focus();
    }
  }

  function scheduleEventRefresh(events = []) {
    const selected = state.sessions.find(session => session.id === state.selectedSessionId);
    const relevant = [...events].reverse().find(event => event.threadId === selected?.threadId);
    if (!relevant) return;
    state.lastEvent = relevant;
    const terminalTurn = relevant.method === 'turn/completed';
    const eventTime = Date.parse(relevant.timestamp || '');
    state.lastEventAt = !terminalTurn && Number.isFinite(eventTime) ? eventTime : 0;
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

  function switchView(view) {
    $$('.view-tab').forEach(tab => {
      const active = tab.dataset.view === view;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-pressed', String(active));
    });
    $$('[data-view-panel]').forEach(panel => {
      const active = panel.dataset.viewPanel === view;
      panel.classList.toggle('active', active);
      panel.hidden = !active;
    });
  }

  function switchMobileView(view) {
    document.body.dataset.mobileView = view;
    $$('.mobile-nav button').forEach(button => button.classList.toggle('active', button.dataset.mobileView === view));
    if (view === 'devices') switchView('devices');
    else switchView('sessions');
  }

  function setupInteractions() {
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
    $('#session-select').addEventListener('change', event => {
      if (event.target.value) selectSession(event.target.value);
      else {
        state.selectedSessionId = '';
        clearConversation();
      }
    });
    $('#manage-synced-sessions').addEventListener('click', openSyncManager);
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
      event.target.style.height = `${Math.min(event.target.scrollHeight, 160)}px`;
    });
    $$('.view-tab').forEach(tab => tab.addEventListener('click', () => switchView(tab.dataset.view)));
    $$('.mobile-nav button').forEach(button => button.addEventListener('click', () => switchMobileView(button.dataset.mobileView)));
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
