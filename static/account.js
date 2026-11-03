// Account workspace: switch sections and rows in place (all panels share one form,
// so unsaved edits survive switching), mark unsaved rows, and run the small
// async actions (test notification, email verification, neighbourhood lists).
(() => {
  const root = document.querySelector('[data-account]');
  const form = document.getElementById('account-form');
  if (!root || !form) return;
  const zh = document.documentElement.lang === 'zh';
  const label = (cn, en) => zh ? cn : en;
  const userId = root.dataset.userId;
  const titleSuffix = document.title.includes(' · ') ? document.title.slice(document.title.indexOf(' · ')) : '';
  const csrf = form.querySelector('input[name="csrf_token"]').value;
  const detail = form.querySelector('.account-detail');
  const remembered = {};
  let dirty = false;
  let submitting = false;

  const escape = value => String(value ?? '').replace(/[&<>"']/g, ch =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[ch]);
  const rowFor = key => form.querySelector(`[data-account-item="${key}"]`);
  const pageUrl = (section, item) => {
    const url = new URL(location.href);
    url.hash = '';
    url.searchParams.set('section', section);
    if (item) url.searchParams.set('item', item); else url.searchParams.delete('item');
    return url;
  };

  function selectItem(section, item, {open = true} = {}) {
    const key = `${section}:${item}`;
    const panel = form.querySelector(`[data-panel="${key}"]`);
    if (!panel) return;
    form.querySelectorAll('[data-panel]').forEach(p => { p.hidden = p !== panel; });
    form.querySelectorAll('[data-account-item]').forEach(row => {
      const active = row.dataset.accountItem === key;
      row.classList.toggle('selected', active);
      if (active) row.setAttribute('aria-current', 'true'); else row.removeAttribute('aria-current');
    });
    form.querySelector('[data-selected-section]').value = section;
    form.querySelector('[data-selected-item]').value = item;
    remembered[section] = item;
    detail.classList.toggle('is-open', open);
    detail.scrollTop = 0;
    history.replaceState(null, '', pageUrl(section, open ? item : null));
  }

  function selectSection(section) {
    const rows = form.querySelector(`[data-section-rows="${section}"]`);
    const head = document.querySelector(`[data-section-head="${section}"]`);
    if (!rows || !head) return;
    form.querySelectorAll('[data-section-rows]').forEach(r => { r.hidden = r !== rows; });
    document.querySelectorAll('[data-account-section]').forEach(link => {
      const active = link.dataset.accountSection === section;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    document.querySelector('[data-account-title]').textContent = head.dataset.title;
    document.querySelector('[data-account-summary]').textContent = head.dataset.summary;
    document.title = `${head.dataset.title}${titleSuffix}`;
    const first = rows.querySelector('[data-account-item]');
    const item = remembered[section] || (first && first.dataset.accountItem.split(':')[1]);
    if (item) selectItem(section, item, {open: false});
    closeSidebar(false);
    const title = document.querySelector('[data-account-title]');
    title.tabIndex = -1;
    title.focus({preventScroll:true});
  }

  document.addEventListener('click', event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;
    const row = event.target.closest('[data-account-item]');
    if (row) {
      event.preventDefault();
      const [section, item] = row.dataset.accountItem.split(':');
      selectItem(section, item);
      detail.querySelector('h2')?.focus?.();
      return;
    }
    const nav = event.target.closest('[data-account-section]');
    if (nav) { event.preventDefault(); selectSection(nav.dataset.accountSection); return; }
    if (event.target.closest('[data-close-detail]')) {
      event.preventDefault();
      detail.classList.remove('is-open');
      history.replaceState(null, '', pageUrl(form.querySelector('[data-selected-section]').value, null));
      return;
    }
    // Links back to this page (onboarding steps) switch in place, keeping unsaved edits
    const link = event.target.closest('a[href]');
    if (link && !link.target) {
      const url = new URL(link.href, location.href);
      const section = url.searchParams.get('section');
      if (url.pathname === location.pathname && section && document.querySelector(`[data-section-head="${section}"]`)) {
        event.preventDefault();
        selectSection(section);
        const item = url.searchParams.get('item');
        if (item && rowFor(`${section}:${item}`)) selectItem(section, item);
      }
    }
  });
  detail.querySelectorAll('h2').forEach(h => h.setAttribute('tabindex', '-1'));

  // Unsaved edits: flag the row of the panel that changed
  form.addEventListener('input', markDirty);
  form.addEventListener('change', markDirty);
  function markDirty(event) {
    const panel = event.target.closest('[data-panel]');
    if (!panel || event.target.type === 'hidden' || event.target.hasAttribute('data-local-pref')) return;
    const row = rowFor(panel.dataset.panel);
    if (!row) return;
    dirty = true;
    row.classList.add('is-dirty');
    row.querySelector('.account-dirty').hidden = false;
  }
  window.addEventListener('beforeunload', event => {
    if (dirty && !submitting) { event.preventDefault(); event.returnValue = ''; }
  });

  // Channels are submitted as one comma-separated field
  const channelsInput = document.getElementById('channels_hidden');
  const emailInput = document.getElementById('email_to_input');
  function syncChannels() {
    if (!channelsInput) return;   // pages without notification channels (global settings)
    channelsInput.value = [...form.querySelectorAll('.channel-chk:checked')].map(c => c.dataset.ch).join(',');
    // Required only while email is on; a hidden required field would block every save
    if (emailInput) emailInput.required = !!form.querySelector('.channel-chk[data-ch="email"]:checked');
  }
  form.querySelectorAll('.channel-chk').forEach(c => c.addEventListener('change', syncChannels));
  syncChannels();

  form.addEventListener('submit', event => {
    if (emailInput && emailInput.required && !emailInput.value.trim()) {
      // The email panel may be hidden; show it so the browser can point at the field
      selectItem('notifications', 'email');
    }
    submitting = true;
  });
  form.addEventListener('invalid', event => {
    const panel = event.target.closest('[data-panel]');
    if (panel && panel.hidden) { const [s, i] = panel.dataset.panel.split(':'); selectSection(s); selectItem(s, i); }
  }, true);
  document.querySelectorAll('form[data-confirm]').forEach(f => f.addEventListener('submit', event => {
    if (!confirm(f.dataset.confirm)) event.preventDefault();
  }));

  // Test notification: results go to the panel that asked
  form.addEventListener('click', event => {
    const button = event.target.closest('[data-test-notify]');
    if (!button) return;
    const box = button.closest('[data-panel]').querySelector('[data-test-result]');
    const original = button.textContent;
    button.disabled = true;
    button.textContent = label('发送中…', 'Sending…');
    box.hidden = true;
    fetch(`/users/${encodeURIComponent(userId)}/test`, {method: 'POST', headers: {'X-CSRF-Token': csrf}})
      .then(r => r.json())
      .then(data => {
        const results = data.results || [];
        box.innerHTML = data.error && !results.length
          ? escape(data.error)
          : `${label('测试结果（使用已保存的设置）', 'Test result (uses saved settings)')}<ul>${results.map(r =>
              `<li>${escape(r.channel)}：${r.ok ? label('已发送', 'sent') : label('失败', 'failed') + (r.error ? ' — ' + escape(r.error) : '')}</li>`).join('')}</ul>`;
      })
      .catch(error => { box.textContent = label('请求失败：', 'Request failed: ') + error.message; })
      .finally(() => { box.hidden = false; button.disabled = false; button.textContent = original; });
  });

  // Email verification for the address currently in the field
  const verifyArea = document.getElementById('email_verify_area');
  function renderVerify() {
    if (!emailInput || !verifyArea) return;
    const value = emailInput.value.trim();
    if (value === verifyArea.dataset.savedEmail) return;
    if (!value) { verifyArea.textContent = ''; return; }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) { verifyArea.textContent = label('邮箱格式不正确', 'Invalid email address'); return; }
    verifyArea.innerHTML = `${escape(label('新地址需要验证。', 'The new address needs verification.'))} <button type="button" class="account-text-button" data-send-verification>${escape(label('发送验证邮件', 'Send verification email'))}</button>`;
  }
  emailInput?.addEventListener('input', renderVerify);
  verifyArea?.addEventListener('click', event => {
    const button = event.target.closest('[data-send-verification]');
    if (!button) return;
    button.disabled = true;
    const body = new URLSearchParams({csrf_token: csrf});
    if (emailInput.value.trim()) body.set('email', emailInput.value.trim());
    fetch(`/users/${encodeURIComponent(userId)}/resend-verify`, {method: 'POST', body})
      .then(r => r.json().then(j => [r.status, j]))
      .then(([status, j]) => {
        verifyArea.textContent = status === 200 && j.ok
          ? (j.msg || label('验证邮件已发送，请查收邮箱。', 'Verification email sent. Check your inbox.'))
          : (j.error || label('发送失败', 'Send failed'));
      })
      .catch(() => { verifyArea.textContent = label('网络错误', 'Network error'); });
  });

  // Appearance is stored per browser, same key as the theme script in base.html
  const themeChoice = document.querySelector('[data-theme-choice]');
  if (themeChoice) {
    let stored = null;
    try { stored = localStorage.getItem('h2s-theme'); } catch (e) {}
    const current = stored === 'light' || stored === 'dark' ? stored : 'auto';
    themeChoice.querySelector(`input[value="${current}"]`).checked = true;
    themeChoice.addEventListener('change', event => {
      const value = event.target.value;
      try {
        if (value === 'auto') localStorage.removeItem('h2s-theme'); else localStorage.setItem('h2s-theme', value);
      } catch (e) {}
      const theme = value === 'auto' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : value;
      document.documentElement.classList.add('theme-transitioning');
      applyTheme(theme);
      setTimeout(() => document.documentElement.classList.remove('theme-transitioning'), 400);
    });
  }

  // Neighbourhoods and buildings follow the cities of their own filter
  // (notification filters and, on the admin editor, auto-booking filters)
  const dynamic = [...form.querySelectorAll('[data-dynamic-options]')];
  function loadDynamic(box) {
    const cities = [...form.querySelectorAll(`input[name="${box.dataset.cities}"]:checked`)].map(c => c.value);
    const kind = box.dataset.dynamicOptions;
    const name = box.dataset.name;
    const keep = new Set([...box.querySelectorAll('input:checked')].map(c => c.value));
    const url = `/api/${kind}` + (cities.length ? `?cities=${encodeURIComponent(cities.join(','))}` : '');
    fetch(url).then(r => r.json()).then(data => {
      const values = data[kind] || [];
      keep.forEach(v => { if (!values.includes(v)) values.unshift(v); });
      box.innerHTML = values.map(v => `<label><input type="checkbox" name="${name}" value="${escape(v)}"${keep.has(v) ? ' checked' : ''}> ${escape(v)}</label>`).join('');
    }).catch(error => console.error('FlatRadar fetch error:', error));
  }
  dynamic.forEach(box => {
    box.dataset.empty = label('当前城市没有可选项', 'Nothing to choose for these cities');
    form.querySelectorAll(`input[name="${box.dataset.cities}"]`).forEach(c => c.addEventListener('change', () => loadDynamic(box)));
    loadDynamic(box);
  });
})();
