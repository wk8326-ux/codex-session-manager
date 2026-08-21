(() => {
  const host = document.querySelector('[data-session-nav]');
  if (!host) return;

  const THEME_KEY = 'codex-session-manager.theme';
  const active = host.dataset.active || 'overview';
  const paths = {
    overview: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 9h8M8 15h5"/>',
    monitor: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    remote: '<rect x="3" y="4" width="18" height="14" rx="2"/><path d="M8 21h8M12 18v3"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z"/>',
  };
  const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]}</svg>`;
  const nav = (id, href, label) => `<a class="nav-link" href="${href}"${active === id ? ' aria-current="page"' : ''}>${icon(id === 'overview' ? 'overview' : id)}<span>${label}</span></a>`;

  host.innerHTML = `
    <a class="brand" href="/" aria-label="Codex 会话管理首页">
      <span class="brand-mark" aria-hidden="true">${icon('monitor')}</span>
      <span class="brand-copy"><strong>Codex 会话管理</strong><span>SESSION / 8767</span></span>
    </a>
    <section class="runtime-summary" aria-label="运行状态">
      <span class="runtime-light" aria-hidden="true"></span>
      <div><strong>服务运行中</strong><small>管理 8767 · PWA 8766</small></div>
    </section>
    <nav class="workspace-nav" aria-label="会话管理导航">
      ${nav('overview', '/', '功能总览')}
      ${nav('monitor', '/watchdog', '监控与续跑')}
      ${nav('remote', '/remote', '远程会话')}
    </nav>
    <div class="sidebar-spacer" aria-hidden="true"></div>
    <button class="theme-toggle" type="button" aria-pressed="false">
      <span class="theme-label">日间模式</span>
      <span class="theme-icons" aria-hidden="true">${icon('sun')}${icon('moon')}</span>
    </button>`;

  const button = host.querySelector('.theme-toggle');
  const label = host.querySelector('.theme-label');
  function apply(theme, persist = false) {
    const next = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    button.setAttribute('aria-pressed', String(next === 'dark'));
    label.textContent = next === 'dark' ? '夜间模式' : '日间模式';
    if (persist) try { localStorage.setItem(THEME_KEY, next); } catch {}
  }
  button.addEventListener('click', () => apply(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark', true));
  apply(document.documentElement.dataset.theme);
})();
