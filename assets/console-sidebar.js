(() => {
  const host = document.querySelector('[data-console-sidebar]');
  if (!host) return;

  const THEME_KEY = 'localhost-project-console.theme';
  const SUMMARY_KEY = 'localhost-project-console.project-summary';
  const active = host.dataset.active || 'projects';
  const icons = {
    activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    layout: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 9h8M8 15h5"/>',
    globe: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10Z"/>',
    refresh: '<path d="M20 11a8.1 8.1 0 1 0 2 5.3"/><path d="M20 4v7h-7"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.66 6.34l1.41-1.41"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z"/>',
  };
  const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name]}</svg>`;
  const navLink = (id, href, iconName, label) => {
    const current = active === id ? ' aria-current="page"' : '';
    return `<a class="nav-link" href="${href}"${current}>${icon(iconName)}<span>${label}</span></a>`;
  };

  host.innerHTML = `
    <a class="brand" href="/" aria-label="本地项目控制台首页">
      <span class="brand-mark" aria-hidden="true">${icon('activity')}</span>
      <span class="brand-copy"><strong>本地项目控制台</strong><span>LOCALHOST / 8765</span></span>
    </a>
    <section class="sidebar-summary" aria-labelledby="summary-heading">
      <p class="summary-label" id="summary-heading">本地服务运行中</p>
      <div class="summary-number"><strong id="running-count">0</strong><span>/ <span id="local-count">0</span> 个</span></div>
      <div class="summary-track" aria-hidden="true"><span id="running-track"></span></div>
    </section>
    <nav class="workspace-nav" aria-label="主导航">
      ${navLink('projects', '/', 'layout', '项目控制台')}
      ${navLink('watchdog', '/watchdog', 'activity', '会话监控')}
      ${navLink('remote', '/remote', 'globe', '远程会话')}
    </nav>
    <div class="sidebar-spacer" aria-hidden="true"></div>
    <div class="theme-controls">
      <button class="theme-toggle" id="theme-toggle" type="button" aria-pressed="false" aria-label="切换至深色模式" title="切换至深色模式">
        <span class="theme-toggle-copy">日间模式</span>
        <span class="theme-switch" aria-hidden="true">
          <span class="theme-glyph sun">${icon('sun')}</span>
          <span class="theme-glyph moon">${icon('moon')}</span>
          <span class="theme-thumb"></span>
        </span>
      </button>
    </div>
    <div class="host-status">
      <div class="host-line"><span class="light" aria-hidden="true"></span><span>控制台在线</span></div>
      <p class="host-address">127.0.0.1:8765</p>
      <p class="refresh-state">${icon('refresh')}<span>每 5 秒同步一次状态</span></p>
    </div>`;

  const button = host.querySelector('#theme-toggle');
  const label = button.querySelector('.theme-toggle-copy');
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  let explicitPreference = false;
  try { explicitPreference = ['light', 'dark'].includes(localStorage.getItem(THEME_KEY)); } catch {}

  function applyTheme(theme, persist = false) {
    const next = theme === 'dark' ? 'dark' : 'light';
    const dark = next === 'dark';
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) themeColor.content = dark ? '#0f1513' : '#e9eeeb';
    button.setAttribute('aria-pressed', String(dark));
    button.setAttribute('aria-label', dark ? '切换至浅色模式' : '切换至深色模式');
    button.title = dark ? '切换至浅色模式' : '切换至深色模式';
    label.textContent = dark ? '夜间模式' : '日间模式';
    if (persist) {
      explicitPreference = true;
      try { localStorage.setItem(THEME_KEY, next); } catch {}
    }
  }

  function setProjectSummary(summary = {}, persist = true) {
    const running = Math.max(0, Number(summary.runningCount) || 0);
    const local = Math.max(0, Number(summary.localCount) || 0);
    host.querySelector('#running-count').textContent = running;
    host.querySelector('#local-count').textContent = local;
    host.querySelector('#running-track').style.width = `${local ? Math.round(running / local * 100) : 0}%`;
    if (persist) {
      try { sessionStorage.setItem(SUMMARY_KEY, JSON.stringify({ runningCount: running, localCount: local })); } catch {}
    }
  }

  try {
    const rememberedSummary = JSON.parse(sessionStorage.getItem(SUMMARY_KEY) || 'null');
    if (rememberedSummary) setProjectSummary(rememberedSummary, false);
  } catch {}

  async function refreshProjectSummary() {
    try {
      const response = await fetch('/api/shell/project-summary', { cache: 'no-store' });
      if (!response.ok) return;
      setProjectSummary(await response.json());
    } catch {
      // Keep the most recent counts when the local console is temporarily busy.
    }
  }

  applyTheme(document.documentElement.dataset.theme);
  button.addEventListener('click', () => applyTheme(
    document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark',
    true,
  ));
  media.addEventListener?.('change', event => {
    if (!explicitPreference) applyTheme(event.matches ? 'dark' : 'light');
  });
  window.addEventListener('storage', event => {
    if (event.key !== THEME_KEY || !['light', 'dark'].includes(event.newValue)) return;
    explicitPreference = true;
    applyTheme(event.newValue);
  });

  window.consoleSidebar = { setProjectSummary };
  host.dataset.ready = 'true';
  if (active !== 'projects') {
    refreshProjectSummary();
    setInterval(refreshProjectSummary, 5000);
  }
})();
