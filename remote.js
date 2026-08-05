(() => {
  const TOKEN_KEY = 'localhost-project-console.remote-token';
  const THEME_KEY = 'localhost-project-console.theme';
  const ACTIVE_REFRESH_MS = 1200;
  const IDLE_REFRESH_MS = 5000;
  const HIDDEN_REFRESH_MS = 12000;
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const state = {
    admin: false,
    token: '',
    pendingToken: '',
    sessions: [],
    localSessions: [],
    projects: [],
    devices: [],
    selectedSessionId: '',
    cursor: 0,
    polling: false,
    pairingSecret: '',
    pairingId: '',
    conversation: null,
    conversationSignature: '',
    conversationRefreshTimer: 0,
    conversationRefreshInFlight: false,
    followTail: true,
    lastUpdatedAt: 0,
    lastEvent: null,
    adminStatus: null,
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
    state.conversation = null;
    state.conversationSignature = '';
    state.lastEvent = null;
    state.followTail = true;
    $('#conversation-meta').textContent = '尚未选择会话';
    $('#conversation-status').textContent = '未选择';
    $('#conversation-status').className = 'status-badge';
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
    copy.textContent = state.admin ? '使用“管理同步”从本机 Codex 选择' : '请在电脑端添加需要同步的会话';
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

  function messageNode(role, label, text) {
    const message = document.createElement('article');
    message.className = `message ${role}`;
    const heading = document.createElement('span');
    heading.className = 'message-label';
    heading.textContent = label;
    const body = document.createElement('div');
    body.className = 'message-body';
    appendRichText(body, text);
    message.append(heading, body);
    return message;
  }

  function conversationStatus(detail) {
    const turns = detail?.turns || [];
    const latest = turns.at(-1);
    const activeStates = ['active', 'inProgress', 'running', 'started'];
    if (activeStates.includes(latest?.status)) return 'inProgress';
    if ((detail?.activeFlags || []).some(flag => activeStates.includes(flag))) return 'inProgress';
    if (activeStates.includes(detail?.status)) return 'inProgress';
    if (latest?.status) return latest.status;
    return detail?.status === 'notLoaded' ? 'idle' : (detail?.status || 'idle');
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

  function renderRuntime(detail) {
    const status = conversationStatus(detail);
    const running = status === 'inProgress';
    const failed = ['failed', 'interrupted', 'systemError'].includes(status);
    $('#runtime-strip').className = `runtime-strip${running ? ' running' : ''}${failed ? ' failed' : ''}`;
    $('#runtime-signal').className = `runtime-signal runtime-rotor${running ? ' running' : ''}${failed ? ' failed' : ''}`;
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
    $('#conversation-meta').textContent = session.threadId;
    const status = conversationStatus(detail);
    $('#conversation-status').textContent = stateLabel(status);
    $('#conversation-status').className = `status-badge${status === 'inProgress' ? ' running' : ''}`;
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
        else if (['agentMessage', 'plan'].includes(item.type) && item.text) section.append(messageNode('agent', 'Codex', item.text));
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
    state.conversationRefreshInFlight = true;
    if (!quiet) $('#sync-state').textContent = '读取会话中';
    try {
      const result = await api(`/api/remote/sessions/${encodeURIComponent(sessionId)}?turnLimit=6`);
      if (sessionId !== state.selectedSessionId) return;
      const session = state.sessions.find(item => item.id === sessionId) || result;
      const signature = JSON.stringify(result.conversation || {});
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
    state.selectedSessionId = sessionId;
    state.conversation = null;
    state.conversationSignature = '';
    state.lastEvent = null;
    state.followTail = true;
    renderSessions();
    await refreshSelectedSession({ force: true, quiet });
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

  function renderRemoteAccess(status) {
    state.adminStatus = status;
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
    addEventListener('online', () => setConnected(true));
    addEventListener('offline', () => setConnected(false));
    document.addEventListener('visibilitychange', () => {
      if (!state.selectedSessionId) return;
      clearTimeout(state.conversationRefreshTimer);
      scheduleConversationRefresh(document.visibilityState === 'hidden' ? HIDDEN_REFRESH_MS : 80);
    });
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
      status = await refreshAdminStatus(true);
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
      await startWorkspace();
      setInterval(() => refreshAdminStatus().catch(() => {}), 5000);
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
