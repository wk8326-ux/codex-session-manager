(() => {
  const STORAGE_KEY = 'localhost-project-console.relay-setup.v1';
  const THEME_KEY = 'localhost-project-console.theme';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];

  const defaults = {
    currentStep: 1,
    completedSteps: [],
    vpsHost: '',
    sshUser: 'ubuntu',
    domain: '',
    frpPort: 7000,
    remotePort: 18766,
    publicUrl: '',
    dnsMatched: false,
    bundleSummary: null,
    verified: false,
    savedAt: '',
  };

  let state = loadState();
  let activeBundle = '';
  let serverPlan = null;
  let clientStatus = null;
  let toastTimer = 0;
  let saveTimer = 0;

  function normalizeCompletedSteps(value) {
    const submitted = new Set(
      Array.isArray(value)
        ? value.filter(step => Number.isInteger(step) && step >= 1 && step <= 5)
        : [],
    );
    const contiguous = [];
    for (let step = 1; step <= 5 && submitted.has(step); step += 1) contiguous.push(step);
    return contiguous;
  }

  function nextAvailableStep(completedSteps = state?.completedSteps || []) {
    return Math.min(5, completedSteps.length + 1);
  }

  function loadState() {
    try {
      const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      if (!value || typeof value !== 'object') return { ...defaults };
      const completedSteps = normalizeCompletedSteps(value.completedSteps);
      return {
        ...defaults,
        ...value,
        completedSteps,
        currentStep: Math.max(1, Math.min(
          nextAvailableStep(completedSteps),
          Number(value.currentStep) || 1,
        )),
        bundleSummary: value.bundleSummary && typeof value.bundleSummary === 'object'
          ? value.bundleSummary
          : null,
      };
    } catch {
      return { ...defaults };
    }
  }

  function persistState(immediate = false) {
    clearTimeout(saveTimer);
    const save = () => {
      state.savedAt = new Date().toISOString();
      const persisted = { ...state };
      delete persisted.bundle;
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted)); } catch {}
      $('#save-state').textContent = '已保存在本机';
    };
    $('#save-state').textContent = '正在保存';
    if (immediate) save();
    else saveTimer = setTimeout(save, 700);
  }

  function showToast(message) {
    const toast = $('#setup-toast');
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 2600);
  }

  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(path, {
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options,
      });
    } catch {
      $('#save-state').textContent = '连接波动，进度已保存在本机';
      throw new Error('控制台暂时不可达，请稍后重试；当前向导进度不会丢失。');
    }
    let body = {};
    try { body = await response.json(); } catch {}
    if (!response.ok) throw new Error(body.message || `请求失败 (${response.status})`);
    return body;
  }

  function payload() {
    return {
      vpsHost: state.vpsHost.trim(),
      sshUser: state.sshUser.trim(),
      domain: state.domain.trim(),
      frpPort: Number(state.frpPort),
      remotePort: Number(state.remotePort),
      frpVersion: '0.61.1',
    };
  }

  function setBusy(button, busy, label) {
    const labelNode = button.querySelector('.button-label');
    if (!button.dataset.label) button.dataset.label = labelNode?.textContent.trim() || button.textContent.trim();
    button.disabled = busy;
    button.setAttribute('aria-busy', String(busy));
    if (labelNode) labelNode.textContent = busy ? label : button.dataset.label;
  }

  function markComplete(step) {
    if (!state.completedSteps.includes(step)) state.completedSteps.push(step);
    state.completedSteps.sort((a, b) => a - b);
    persistState(true);
  }

  function canOpenStep(step) {
    return step <= nextAvailableStep();
  }

  function goToStep(step) {
    const next = Math.max(1, Math.min(5, Number(step) || 1));
    if (!canOpenStep(next)) return;
    state.currentStep = next;
    $$('.step-panel').forEach(panel => {
      const active = Number(panel.dataset.stepPanel) === next;
      panel.hidden = !active;
      panel.classList.toggle('active', active);
    });
    renderProgress();
    persistState(true);
    if (next === 2) renderDnsRecord();
    if (next === 3) refreshServerPlan();
    if (next === 4) refreshClientStatus(false);
    if (next === 5) renderVerificationBase();
    $('#setup-stage').focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }

  function renderProgress() {
    const current = state.currentStep;
    const completed = new Set(state.completedSteps);
    const list = $('.step-list');
    const progress = Math.max(0, Math.min(1, (Math.max(current - 1, completed.size) / 4)));
    list.style.setProperty('--progress-height', `${progress * 264}px`);
    list.style.setProperty('--progress-width', `calc(${progress * 100}% - ${progress * 36}px)`);
    $$('[data-step-target]').forEach(button => {
      const step = Number(button.dataset.stepTarget);
      const isCurrent = step === current;
      const isComplete = completed.has(step);
      button.classList.toggle('current', isCurrent);
      button.classList.toggle('complete', isComplete && !isCurrent);
      button.disabled = !canOpenStep(step);
      button.setAttribute('aria-current', isCurrent ? 'step' : 'false');
      const status = button.querySelector('.step-state');
      status.textContent = isCurrent ? '当前' : (isComplete ? '已完成' : '待完成');
    });
  }

  function restoreFields() {
    $('#vps-host').value = state.vpsHost;
    $('#ssh-user').value = state.sshUser;
    $('#public-domain').value = state.domain;
    $('#frp-port').value = state.frpPort;
    $('#remote-port').value = state.remotePort;
    if (state.bundleSummary) renderBundleSummary(state.bundleSummary);
    renderBundleAvailability();
  }

  function collectFields() {
    state.vpsHost = $('#vps-host').value.trim();
    state.sshUser = $('#ssh-user').value.trim();
    state.domain = $('#public-domain').value.trim().replace(/^https?:\/\//i, '').replace(/\/$/, '');
    state.frpPort = Number($('#frp-port').value);
    state.remotePort = Number($('#remote-port').value);
  }

  function clearErrors(panel) {
    panel.querySelectorAll('[aria-invalid="true"]').forEach(element => element.removeAttribute('aria-invalid'));
    panel.querySelectorAll('.field-error,.form-error').forEach(element => { element.textContent = ''; });
  }

  function showPanelError(step, message, field = null) {
    const error = $(`#step-${step}-error`);
    if (error) error.textContent = message;
    if (field) {
      field.setAttribute('aria-invalid', 'true');
      field.focus();
    }
  }

  async function submitServerInfo(event) {
    event.preventDefault();
    const panel = $('[data-step-panel="1"]');
    clearErrors(panel);
    collectFields();
    const button = panel.querySelector('[type="submit"]');
    setBusy(button, true, '正在校验...');
    try {
      serverPlan = await api('/api/relay-setup/server-plan', {
        method: 'POST',
        body: JSON.stringify(payload()),
      });
      Object.assign(state, {
        vpsHost: serverPlan.vpsHost,
        sshUser: serverPlan.sshUser,
        domain: serverPlan.domain,
        frpPort: serverPlan.frpPort,
        remotePort: serverPlan.remotePort,
        publicUrl: serverPlan.publicUrl,
        dnsMatched: false,
        verified: false,
      });
      markComplete(1);
      goToStep(2);
    } catch (error) {
      const message = error.message || '服务器信息无效。';
      const field = message.includes('VPS') ? $('#vps-host')
        : message.includes('SSH') ? $('#ssh-user')
          : message.includes('域名') ? $('#public-domain') : null;
      showPanelError(1, message, field);
    } finally {
      setBusy(button, false, '');
    }
  }

  function renderDnsRecord() {
    $('#dns-host-label').textContent = state.domain || '尚未填写';
    $('#dns-target-label').textContent = state.vpsHost || '尚未填写';
    if (state.dnsMatched) {
      renderCheckResult($('#dns-check-result'), 'success', '解析已经确认', `${state.domain} 指向 ${state.vpsHost}`);
    }
  }

  function renderCheckResult(element, className, title, detail) {
    element.className = `check-result ${className}`;
    element.querySelector('strong').textContent = title;
    element.querySelector('small').textContent = detail;
  }

  async function checkDns() {
    const button = $('#check-dns');
    const result = $('#dns-check-result');
    renderCheckResult(result, 'checking', '正在查询 DNS', '检查域名当前的公开解析结果。');
    setBusy(button, true, '正在检查...');
    try {
      const response = await api('/api/relay-setup/dns-check', {
        method: 'POST',
        body: JSON.stringify({ domain: state.domain, vpsHost: state.vpsHost }),
      });
      state.dnsMatched = response.matched;
      if (response.matched) {
        renderCheckResult(result, 'success', '解析已经生效', `${response.domain} → ${response.addresses.join(', ')}`);
        markComplete(2);
        setTimeout(() => goToStep(3), 450);
      } else {
        renderCheckResult(result, 'failure', '解析尚未匹配', response.detail);
        persistState(true);
      }
    } catch (error) {
      renderCheckResult(result, 'failure', 'DNS 检查失败', error.message);
    } finally {
      setBusy(button, false, '');
    }
  }

  async function refreshServerPlan() {
    try {
      serverPlan = await api('/api/relay-setup/server-plan', {
        method: 'POST',
        body: JSON.stringify(payload()),
      });
      $('#server-command').textContent = serverPlan.serverCommand;
    } catch (error) {
      $('#server-command').textContent = '无法生成命令，请返回步骤 1 检查服务器信息。';
      showPanelError(3, error.message);
    }
  }

  async function validateBundle() {
    const button = $('#validate-bundle');
    const field = $('#server-bundle');
    const panel = $('[data-step-panel="3"]');
    clearErrors(panel);
    setBusy(button, true, '正在验证...');
    try {
      const raw = field.value.trim().replace(/^LPC_CONFIG_BUNDLE=/, '');
      const summary = await api('/api/relay-setup/bundle-check', {
        method: 'POST',
        body: JSON.stringify({ bundle: raw }),
      });
      activeBundle = raw;
      state.bundleSummary = summary;
      state.publicUrl = summary.publicUrl;
      field.value = '';
      renderBundleSummary(summary);
      renderBundleAvailability();
      markComplete(3);
      goToStep(4);
    } catch (error) {
      field.setAttribute('aria-invalid', 'true');
      showPanelError(3, error.message, field);
    } finally {
      setBusy(button, false, '');
    }
  }

  function renderBundleSummary(summary) {
    $('#bundle-server').textContent = `${summary.serverAddr}:${summary.serverPort}`;
    $('#bundle-public-url').textContent = summary.publicUrl;
  }

  function renderBundleAvailability() {
    const button = $('#copy-bundle');
    const note = $('#bundle-memory-note');
    const available = Boolean(activeBundle);
    button.disabled = !available;
    note.classList.toggle('warning', Boolean(state.bundleSummary) && !available);
    note.textContent = available
      ? '连接凭据只保留在当前页面内，完成本机安装前不要刷新或关闭页面。'
      : state.bundleSummary
        ? '出于安全考虑，连接凭据没有持久化。请返回步骤 3 重新粘贴配置包。'
        : '连接凭据只保留在当前页面内，刷新或关闭页面后需要重新粘贴配置包。';
  }

  function renderClientCheck(name, success, successText, waitingText) {
    const row = $(`[data-client-check="${name}"]`);
    row.classList.toggle('success', success);
    row.classList.toggle('failure', !success && name === 'tunnel' && clientStatus?.tunnel?.state === 'failed');
    row.querySelector('small').textContent = success ? successText : waitingText;
  }

  async function refreshClientStatus(startTunnel) {
    const button = $('#refresh-client-status');
    const error = $('#step-4-error');
    error.textContent = '';
    if (startTunnel) setBusy(button, true, '正在检查...');
    try {
      clientStatus = await api('/api/relay-setup/status');
      renderExistingStatus(clientStatus);
      $('#client-command').textContent = clientStatus.clientCommand || '本机安装脚本不存在。';
      if (startTunnel && clientStatus.frpcExecutableReady && clientStatus.frpcConfigReady && !clientStatus.tunnel?.running) {
        const result = await api('/api/relay-setup/tunnel/start', { method: 'POST', body: '{}' });
        clientStatus.tunnel = result.tunnel;
        renderExistingStatus(clientStatus);
      }
      renderClientCheck('script', clientStatus.clientScriptReady, '脚本已就绪', '安装脚本不存在');
      renderClientCheck('binary', clientStatus.frpcExecutableReady, '官方文件校验完成', '运行本机安装命令');
      renderClientCheck('config', clientStatus.frpcConfigReady, '配置保存在 .runtime', '等待导入配置包');
      renderClientCheck('tunnel', Boolean(clientStatus.tunnel?.running), '隧道正在运行', tunnelDetail(clientStatus.tunnel));
      if (clientStatus.tunnel?.running) {
        markComplete(4);
        if (startTunnel) setTimeout(() => goToStep(5), 450);
      } else if (startTunnel && (!clientStatus.frpcExecutableReady || !clientStatus.frpcConfigReady)) {
        error.textContent = '请先复制配置包并运行上方 PowerShell 命令，然后重新检查。';
      }
    } catch (caught) {
      error.textContent = caught.message;
    } finally {
      if (startTunnel) setBusy(button, false, '');
    }
  }

  function tunnelDetail(tunnel = {}) {
    if (tunnel.state === 'failed') return tunnel.detail || '隧道启动失败，请查看 .runtime/frp/frpc.log';
    if (tunnel.state === 'stopped') return '配置已存在，等待启动';
    return '尚未连接';
  }

  function renderExistingStatus(status = {}) {
    const panel = $('#existing-status');
    const tunnel = status.tunnel || {};
    const running = Boolean(tunnel.running);
    const failed = tunnel.state === 'failed';
    panel.className = `existing-status ${running ? 'running' : failed ? 'failed' : 'idle'}`;
    panel.querySelector('strong').textContent = running
      ? '现有 FRP 隧道运行中'
      : failed
        ? '现有隧道需要处理'
        : '当前设备尚未完成中继配置';
    panel.querySelector('small').textContent = running
      ? `进程 ${tunnel.pid || '已连接'} · 向导不会自动覆盖配置`
      : failed
        ? (tunnel.detail || '可以继续向导重新生成配置')
        : '完成 5 个步骤后会随控制台自动启动';
  }

  async function restoreSetupStatus() {
    try {
      clientStatus = await api('/api/relay-setup/status');
      renderExistingStatus(clientStatus);
    } catch {
      const panel = $('#existing-status');
      panel.className = 'existing-status failed';
      panel.querySelector('strong').textContent = '暂时无法读取本机状态';
      panel.querySelector('small').textContent = '向导草稿仍安全保存在当前浏览器';
    }
  }

  async function requestPersistentStorage() {
    try {
      if (navigator.storage?.persist) await navigator.storage.persist();
    } catch {}
  }

  function renderVerificationBase() {
    setVerification('dns', state.dnsMatched ? 'success' : 'idle', state.dnsMatched ? '解析已匹配' : '等待验证');
    const running = Boolean(clientStatus?.tunnel?.running);
    setVerification('tunnel', running ? 'success' : 'idle', running ? '隧道正在运行' : '等待验证');
  }

  function setVerification(name, status, detail) {
    const row = $(`[data-verification="${name}"]`);
    row.className = status;
    row.querySelector('small').textContent = detail;
  }

  async function verifyAll() {
    const button = $('#verify-all');
    $('#step-5-error').textContent = '';
    setBusy(button, true, '正在验证...');
    for (const name of ['dns', 'tunnel', 'https']) setVerification(name, 'checking', '检查中');
    try {
      const dns = await api('/api/relay-setup/dns-check', {
        method: 'POST', body: JSON.stringify({ domain: state.domain, vpsHost: state.vpsHost }),
      });
      setVerification('dns', dns.matched ? 'success' : 'failure', dns.detail);
      const status = await api('/api/relay-setup/status');
      clientStatus = status;
      renderExistingStatus(clientStatus);
      const running = Boolean(status.tunnel?.running);
      setVerification('tunnel', running ? 'success' : 'failure', running ? 'FRP 客户端正在运行' : tunnelDetail(status.tunnel));
      const publicResult = await api('/api/relay-setup/verify', {
        method: 'POST', body: JSON.stringify({ publicUrl: state.publicUrl }),
      });
      setVerification('https', publicResult.reachable ? 'success' : 'failure', publicResult.detail);
      if (dns.matched && running && publicResult.reachable) {
        setVerification('pairing', 'idle', '现在可以回到远程会话生成二维码');
        state.verified = true;
        markComplete(5);
        $('#completion-note').hidden = false;
        $('#finish-setup').hidden = false;
        button.hidden = true;
      } else {
        throw new Error('仍有检查项未通过，请根据状态修复后重试。');
      }
    } catch (error) {
      $('#step-5-error').textContent = error.message;
    } finally {
      setBusy(button, false, '');
    }
  }

  async function copyText(text, message) {
    if (!text) {
      showToast('当前没有可复制的内容。');
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      showToast(message);
    } catch {
      showToast('无法访问剪贴板，请手动选择复制。');
    }
  }

  function setupTheme() {
    const button = $('#setup-theme-toggle');
    const apply = (theme, persist) => {
      const next = theme === 'dark' ? 'dark' : 'light';
      document.documentElement.dataset.theme = next;
      document.documentElement.style.colorScheme = next;
      const dark = next === 'dark';
      button.setAttribute('aria-label', dark ? '切换至浅色模式' : '切换至深色模式');
      button.title = dark ? '切换至浅色模式' : '切换至深色模式';
      $('meta[name="theme-color"]').content = dark ? '#0f1513' : '#eef1ef';
      if (persist) try { localStorage.setItem(THEME_KEY, next); } catch {}
    };
    apply(document.documentElement.dataset.theme, false);
    button.addEventListener('click', () => apply(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark', true));
  }

  function resetWizard() {
    if (!confirm('重新开始会清除这个浏览器保存的向导进度，但不会删除 VPS 或 .runtime 中的配置。')) return;
    try { localStorage.removeItem(STORAGE_KEY); } catch {}
    state = { ...defaults };
    activeBundle = '';
    serverPlan = null;
    clientStatus = null;
    restoreFields();
    renderBundleAvailability();
    renderDnsRecord();
    $('#completion-note').hidden = true;
    $('#finish-setup').hidden = true;
    $('#verify-all').hidden = false;
    goToStep(1);
  }

  function setupInteractions() {
    $('#server-info-form').addEventListener('submit', submitServerInfo);
    $('#server-info-form').addEventListener('input', () => { collectFields(); persistState(); });
    $$('[data-step-target]').forEach(button => button.addEventListener('click', () => goToStep(button.dataset.stepTarget)));
    $$('[data-back-step]').forEach(button => button.addEventListener('click', () => goToStep(button.dataset.backStep)));
    $('#check-dns').addEventListener('click', checkDns);
    $('#validate-bundle').addEventListener('click', validateBundle);
    $('#refresh-client-status').addEventListener('click', () => refreshClientStatus(true));
    $('#verify-all').addEventListener('click', verifyAll);
    $('#copy-server-command').addEventListener('click', () => copyText(serverPlan?.serverCommand, 'VPS 安装命令已复制。'));
    $('#copy-client-command').addEventListener('click', () => copyText(clientStatus?.clientCommand, '本机安装命令已复制。'));
    $('#copy-bundle').addEventListener('click', () => copyText(activeBundle, activeBundle ? '配置包已复制，请粘贴到 PowerShell 提示中。' : '配置包未保留，请返回步骤 3 重新粘贴。'));
    $('#copy-dns-record').addEventListener('click', () => copyText(`A\t${state.domain}\t${state.vpsHost}`, 'DNS 记录已复制。'));
    $('#reset-wizard').addEventListener('click', resetWizard);
    addEventListener('online', () => restoreSetupStatus());
    addEventListener('offline', () => {
      $('#save-state').textContent = '网络波动，进度已保存在本机';
    });
  }

  function initialize() {
    restoreFields();
    setupTheme();
    setupInteractions();
    requestPersistentStorage();
    restoreSetupStatus();
    goToStep(state.currentStep);
    if (state.verified) {
      $('#completion-note').hidden = false;
      $('#finish-setup').hidden = false;
      $('#verify-all').hidden = true;
    }
  }

  initialize();
})();
