(() => {
  const TOKEN_KEY = 'localhost-project-console.remote-token';
  const THEME_KEY = 'localhost-project-console.theme';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const state = {
    admin: false,
    token: '',
    pendingToken: '',
    sessions: [],
    projects: [],
    devices: [],
    selectedSessionId: '',
    cursor: 0,
    polling: false,
    pairingSecret: '',
    pairingId: '',
    refreshTimer: 0,
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

  function showToast(message) {
    const toast = $('#toast');
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => { toast.hidden = true; }, 3200);
  }

  function setConnected(connected, detail = '') {
    $('#offline-banner').hidden = connected;
    $('.connection-dot')?.classList.toggle('offline', !connected);
    $('#connection-label').textContent = connected ? '主机已连接' : '主机连接中断';
    $('#connection-detail').textContent = detail || (connected ? '事件通道已连接' : '等待重新连接');
    $('#sync-state').textContent = connected ? '已同步' : '重新连接中';
  }

  async function api(path, options = {}) {
    const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
    if (!state.admin && state.token) headers.Authorization = `Bearer ${state.token}`;
    const response = await fetch(path, { ...options, headers, cache: 'no-store' });
    const payload = response.status === 204
      ? null
      : await response.json().catch(() => ({ message: '服务返回了无法读取的响应。' }));
    if (response.status === 401 && !state.admin) {
      storeToken('');
      throw new Error('设备授权已失效，请重新扫码配对。');
    }
    if (!response.ok) throw new Error(payload?.message || `请求失败 (${response.status})`);
    return payload;
  }

  function applyTheme(theme, persist = false) {
    const next = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    document.querySelector('meta[name="theme-color"]').content = next === 'dark' ? '#0f1513' : '#e9eeeb';
    const dark = next === 'dark';
    $('#theme-toggle').setAttribute('aria-pressed', String(dark));
    $('#theme-label').textContent = dark ? '夜间模式' : '日间模式';
    if (persist) {
      try { localStorage.setItem(THEME_KEY, next); } catch {}
    }
  }

  function setupTheme() {
    applyTheme(document.documentElement.dataset.theme);
    $('#theme-toggle').addEventListener('click', () => {
      applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark', true);
    });
  }

  function parsePairingLink() {
    const query = new URLSearchParams(location.search);
    const fragment = new URLSearchParams(location.hash.slice(1));
    state.pairingId = query.get('pairing') || '';
    state.pairingSecret = fragment.get('secret') || '';
    if (state.pairingSecret) history.replaceState(null, '', `${location.pathname}?pairing=${encodeURIComponent(state.pairingId)}`);
  }

  function showPairScreen(message = '') {
    $('#app-shell').hidden = true;
    $('#pair-screen').hidden = false;
    $('#pair-error').textContent = message;
    const validLink = Boolean(state.pairingId && state.pairingSecret);
    $('#claim-pairing').disabled = !validLink;
    if (!validLink && !message) $('#pair-error').textContent = '请在电脑端生成二维码后重新扫码。';
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
      state.pairingSecret = '';
      $('#claim-pairing').hidden = true;
      $('#pair-device-name').disabled = true;
      $('#mobile-comparison-code').textContent = result.comparisonCode;
      $('#mobile-comparison').hidden = false;
    } catch (error) {
      $('#pair-error').textContent = error.message;
      button.disabled = false;
    }
  }

  async function enterAfterPairing() {
    storeToken(state.pendingToken);
    state.pendingToken = '';
    $('#pair-screen').hidden = true;
    $('#app-shell').hidden = false;
    await startWorkspace();
  }

  function stateLabel(value) {
    const labels = {
      active: '运行中', inProgress: '运行中', idle: '空闲', completed: '已完成',
      failed: '失败', interrupted: '已中断', systemError: '系统错误', notLoaded: '未载入',
      running: '运行中', online: '在线', stopped: '已关闭', offline: '离线',
    };
    return labels[value] || value || '未知';
  }

  function renderSessions() {
    const select = $('#session-select');
    select.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = state.sessions.length ? '选择已登记会话' : '尚未登记监控会话';
    select.append(placeholder);
    for (const session of state.sessions) {
      const option = document.createElement('option');
      option.value = session.id;
      option.textContent = `${session.name} · ${stateLabel(session.lastSessionState)}`;
      select.append(option);
    }
    select.disabled = state.sessions.length === 0;
    select.value = state.selectedSessionId;
  }

  function messageNode(role, label, text) {
    const message = document.createElement('article');
    message.className = `message ${role}`;
    const heading = document.createElement('span');
    heading.className = 'message-label';
    heading.textContent = label;
    const body = document.createElement('div');
    body.className = 'message-body';
    body.textContent = text;
    message.append(heading, body);
    return message;
  }

  function activityNode(item) {
    const row = document.createElement('div');
    row.className = 'activity-row';
    const detail = [item.label || '任务活动', item.tool, item.status && stateLabel(item.status)]
      .filter(Boolean).join(' · ');
    row.textContent = detail;
    return row;
  }

  function renderConversation(session, detail) {
    $('#conversation-meta').textContent = session.threadId;
    const status = detail.status || session.lastSessionState;
    $('#conversation-status').textContent = stateLabel(status);
    $('#conversation-status').className = `status-badge${['active', 'inProgress'].includes(status) ? ' running' : ''}`;
    $('#message-input').disabled = false;
    $('#send-button').disabled = false;
    const transcript = $('#transcript');
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
    for (const turn of turns) {
      const section = document.createElement('section');
      section.className = 'turn';
      const divider = document.createElement('div');
      divider.className = 'turn-divider';
      divider.textContent = `${stateLabel(turn.status)} · ${turn.id.slice(0, 8)}`;
      section.append(divider);
      for (const item of turn.items || []) {
        if (item.type === 'userMessage' && item.text) section.append(messageNode('user', '你', item.text));
        else if (['agentMessage', 'plan'].includes(item.type) && item.text) section.append(messageNode('agent', 'Codex', item.text));
        else if (item.type === 'reasoning' && item.text) section.append(messageNode('agent', '过程摘要', item.text));
        else section.append(activityNode(item));
      }
      if (turn.error) section.append(messageNode('agent', '任务错误', turn.error));
      transcript.append(section);
    }
    requestAnimationFrame(() => { transcript.scrollTop = transcript.scrollHeight; });
  }

  async function selectSession(sessionId, quiet = false) {
    state.selectedSessionId = sessionId;
    renderSessions();
    if (!quiet) $('#sync-state').textContent = '读取会话中';
    try {
      const result = await api(`/api/remote/sessions/${encodeURIComponent(sessionId)}`);
      const session = state.sessions.find(item => item.id === sessionId) || result;
      renderConversation(session, result.conversation);
      setConnected(true);
    } catch (error) {
      if (!quiet) showToast(error.message);
      setConnected(false, '会话读取失败');
    }
  }

  function renderProjects() {
    const list = $('#project-list');
    list.replaceChildren();
    $('#project-count').textContent = `${state.projects.length} 个项目`;
    for (const project of state.projects) {
      const row = document.createElement('div');
      row.className = 'project-row';
      const name = document.createElement('strong');
      name.textContent = project.name;
      const mode = document.createElement('small');
      mode.textContent = project.mode === 'external' ? '网页入口' : '本地项目';
      const status = document.createElement('span');
      status.className = `project-state ${project.state}`;
      status.textContent = project.stateLabel;
      row.append(name, mode, status);
      list.append(row);
    }
  }

  function formatDate(value) {
    if (!value) return '从未连接';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
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
    const tasks = [api('/api/remote/sessions'), api('/api/remote/projects')];
    if (state.admin) tasks.push(api('/api/remote/devices'));
    const [sessions, projects, devices = []] = await Promise.all(tasks);
    state.sessions = sessions;
    state.projects = projects;
    state.devices = devices;
    if (state.selectedSessionId && !sessions.some(item => item.id === state.selectedSessionId)) state.selectedSessionId = '';
    if (!state.selectedSessionId && sessions.length) state.selectedSessionId = sessions[0].id;
    renderSessions();
    renderProjects();
    if (state.admin) renderDevices();
    if (state.selectedSessionId) await selectSession(state.selectedSessionId, true);
    setConnected(true);
  }

  async function sendMessage(event) {
    event.preventDefault();
    const input = $('#message-input');
    const message = input.value.trim();
    if (!message || !state.selectedSessionId) return;
    const button = $('#send-button');
    button.disabled = true;
    try {
      const result = await api(`/api/remote/sessions/${encodeURIComponent(state.selectedSessionId)}/messages`, {
        method: 'POST', body: JSON.stringify({ message }),
      });
      input.value = '';
      input.style.height = '';
      showToast(result.delivery === 'steered' ? '消息已加入当前运行中的任务。' : '消息已启动新的任务轮次。');
      setTimeout(() => selectSession(state.selectedSessionId, true), 450);
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
      input.focus();
    }
  }

  function scheduleEventRefresh() {
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(async () => {
      try {
        const sessions = await api('/api/remote/sessions');
        state.sessions = sessions;
        renderSessions();
        if (state.selectedSessionId) await selectSession(state.selectedSessionId, true);
      } catch {}
    }, 300);
  }

  async function pollEvents() {
    if (state.polling) return;
    state.polling = true;
    while (state.polling && (state.admin || state.token)) {
      try {
        const batch = await api(`/api/remote/events?after=${state.cursor}&timeout=25`);
        state.cursor = batch.cursor;
        if (batch.events?.length) scheduleEventRefresh();
        setConnected(true);
      } catch (error) {
        setConnected(false);
        if (!state.token && !state.admin) {
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
        if (!seconds) clearInterval(createPairing.timer);
      };
      updateExpiry();
      createPairing.timer = setInterval(updateExpiry, 1000);
    } catch (error) {
      showToast(error.message);
    } finally { button.disabled = false; }
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
    if (view === 'projects') switchView('projects');
    else if (view === 'devices') switchView('devices');
    else switchView('sessions');
  }

  function setupInteractions() {
    $('#claim-pairing').addEventListener('click', claimPairing);
    $('#confirm-pairing').addEventListener('click', enterAfterPairing);
    $('#create-pairing').addEventListener('click', createPairing);
    $('#composer').addEventListener('submit', sendMessage);
    $('#session-select').addEventListener('change', event => {
      if (event.target.value) selectSession(event.target.value);
    });
    $('#refresh-button').addEventListener('click', () => loadWorkspaceData().catch(error => showToast(error.message)));
    $('#message-input').addEventListener('input', event => {
      event.target.style.height = 'auto';
      event.target.style.height = `${Math.min(event.target.scrollHeight, 160)}px`;
    });
    $$('.view-tab').forEach(tab => tab.addEventListener('click', () => switchView(tab.dataset.view)));
    $$('.mobile-nav button').forEach(button => button.addEventListener('click', () => switchMobileView(button.dataset.mobileView)));
    addEventListener('online', () => setConnected(true));
    addEventListener('offline', () => setConnected(false));
  }

  async function startWorkspace() {
    $('#app-shell').hidden = false;
    $('#pair-screen').hidden = true;
    try {
      await loadWorkspaceData();
      pollEvents();
    } catch (error) {
      if (!state.admin && !state.token) showPairScreen(error.message);
      else {
        setConnected(false);
        showToast(error.message);
      }
    }
  }

  async function initialize() {
    setupTheme();
    setupInteractions();
    parsePairingLink();
    state.token = readToken();
    let status = null;
    try {
      const response = await fetch('/api/remote/status', { cache: 'no-store' });
      if (response.ok) status = await response.json();
    } catch {}
    state.admin = Boolean(status);
    document.body.classList.toggle('admin-mode', state.admin);
    document.body.classList.toggle('remote-mode', !state.admin);
    $$('.admin-only').forEach(element => {
      if (!state.admin) element.hidden = true;
      else if (!element.hasAttribute('data-view-panel')) element.hidden = false;
    });
    if (state.admin) {
      $('#host-label').textContent = 'LOCALHOST / 8765';
      $('#public-base-url').value = status.baseUrl || '';
      $('#codex-state').textContent = status.codexConnected ? 'App Server 已连接' : 'App Server 不可用';
      await startWorkspace();
    } else if (state.token) {
      await startWorkspace();
    } else {
      showPairScreen();
    }
    if ('serviceWorker' in navigator && window.isSecureContext) {
      navigator.serviceWorker.register('/service-worker.js').catch(() => {});
    }
  }

  initialize();
})();
