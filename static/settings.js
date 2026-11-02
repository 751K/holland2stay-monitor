// Global settings: select-all for city lists, the Xior building picker, and the
// maintenance actions (reload / restart the monitor, reset the database). Row
// selection and unsaved markers come from account.js.
(() => {
  const form = document.getElementById('account-form');
  if (!form) return;
  const zh = document.documentElement.lang === 'zh';
  const label = (cn, en) => zh ? cn : en;
  const csrf = form.querySelector('input[name="csrf_token"]').value;

  function toast(ok, message) {
    const wrap = document.getElementById('toast-container');
    if (!wrap) return;
    const node = document.createElement('div');
    node.className = `toast ${ok ? 'toast-success' : 'toast-error'}`;
    node.textContent = message;
    wrap.appendChild(node);
    setTimeout(() => node.remove(), 4000);
  }

  // A form change event so account.js marks the row as unsaved
  const changed = box => box.dispatchEvent(new Event('change', {bubbles: true}));

  // Select all / clear for a plain checkbox list
  function syncToggle(button) {
    const boxes = [...button.closest('[data-panel]').querySelectorAll('[data-check-group] input')];
    button.textContent = boxes.every(b => b.checked) ? label('清空', 'Clear all') : label('全选', 'Select all');
  }
  form.querySelectorAll('[data-toggle-all]').forEach(button => {
    syncToggle(button);
    button.addEventListener('click', () => {
      const boxes = [...button.closest('[data-panel]').querySelectorAll('[data-check-group] input')];
      const on = !boxes.every(b => b.checked);
      boxes.forEach(b => { b.checked = on; });
      if (boxes[0]) changed(boxes[0]);
      syncToggle(button);
    });
    button.closest('[data-panel]').addEventListener('change', () => syncToggle(button));
  });

  // Xior: one checkbox per city selects all its buildings; the tally shows how many
  // are picked while the city stays folded
  const xior = form.querySelector('[data-xior]');
  if (xior) {
    const groups = [...xior.querySelectorAll('.xior-group')];
    const all = () => [...xior.querySelectorAll('.xior-bldg input')];
    const count = form.querySelector('[data-xior-count]');
    const toggle = form.querySelector('[data-xior-toggle]');
    const sync = () => {
      let on = 0;
      groups.forEach(group => {
        const boxes = [...group.querySelectorAll('.xior-bldg input')];
        const n = boxes.filter(b => b.checked).length;
        on += n;
        group.querySelector('.xior-tally').textContent = `${n}/${boxes.length}`;
        const city = group.querySelector('.xior-city');
        city.checked = n === boxes.length && n > 0;
        city.indeterminate = n > 0 && n < boxes.length;   // half-picked must not look like none
      });
      const total = all().length;
      count.textContent = label(`已选 ${on} / ${total} 栋`, `${on} of ${total} selected`);
      toggle.textContent = on === total ? label('清空', 'Clear all') : label('全选', 'Select all');
    };
    // Ticking the city box must not fold/unfold the group
    xior.addEventListener('click', event => { if (event.target.classList.contains('xior-city')) event.stopPropagation(); }, true);
    xior.addEventListener('change', event => {
      if (event.target.classList.contains('xior-city')) {
        const want = event.target.checked;
        event.target.closest('.xior-group').querySelectorAll('.xior-bldg input').forEach(b => { b.checked = want; });
      }
      sync();
    });
    toggle.addEventListener('click', () => {
      const boxes = all();
      const on = !boxes.every(b => b.checked);
      boxes.forEach(b => { b.checked = on; });
      if (boxes[0]) changed(boxes[0]);
      sync();
    });
    sync();
  }

  // Monitor: reload the configuration now, or restart the process
  form.addEventListener('click', event => {
    const button = event.target.closest('[data-monitor-action]');
    if (!button) return;
    const restart = button.dataset.monitorAction === 'restart';
    if (restart && !confirm(label('确定要重启监控进程吗？这会中断本轮抓取，只在改了代码后需要。',
                                  'Restart the monitor? This interrupts the current scrape; only needed after code changes.'))) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = restart ? label('重启中…', 'Restarting…') : label('发送中…', 'Sending…');
    fetch(restart ? '/api/monitor/restart' : '/api/reload', {method: 'POST', headers: {'X-CSRF-Token': csrf}})
      .then(r => r.json())
      .then(d => toast(d.ok, d.ok ? d.message : (d.error || label('未知错误', 'Unknown error'))))
      .catch(() => toast(false, label('请求失败', 'Request failed')))
      .finally(() => { button.disabled = false; button.textContent = original; });
  });

  // Reset database, behind a confirmation dialog
  const dialog = document.getElementById('reset-dialog');
  form.querySelector('[data-open-reset]')?.addEventListener('click', () => dialog.showModal());
  dialog?.addEventListener('click', event => {
    if (event.target.closest('[data-close-dialog]') || event.target === dialog) { dialog.close(); return; }
    const button = event.target.closest('[data-confirm-reset]');
    if (!button) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = label('清空中…', 'Resetting…');
    fetch('/api/reset-db', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify({confirm: true})})
      .then(r => r.json())
      .then(d => { dialog.close(); toast(d.ok, d.ok ? d.message : (d.error || label('未知错误', 'Unknown error'))); })
      .catch(() => toast(false, label('请求失败', 'Request failed')))
      .finally(() => { button.disabled = false; button.textContent = original; });
  });
})();
