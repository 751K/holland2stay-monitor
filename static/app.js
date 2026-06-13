/* ===================================================================
   Holland2Stay — Frontend JS
   =================================================================== */

// ── Theme ──────────────────────────────────────────────────────────
(function(){
  var t = localStorage.getItem('h2s-theme');
  if(!t) t = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  applyTheme(t);
})();

function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  var icon = document.getElementById('theme-icon');
  if(icon) icon.className = 'bi ' + (t === 'dark' ? 'bi-sun' : 'bi-moon-stars-fill');
}

function toggleTheme() {
  var cur = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  var html = document.documentElement;
  html.classList.add('theme-transitioning');
  applyTheme(cur);
  localStorage.setItem('h2s-theme', cur);
  setTimeout(function(){ html.classList.remove('theme-transitioning'); }, 400);
}

// ── Mobile Sidebar ────────────────────────────────────────────────
function toggleSidebar() {
  var sb = document.querySelector('.sidebar');
  var ov = document.querySelector('.sidebar-overlay');
  if(!sb) return;
  sb.classList.toggle('open');
  if(ov) ov.classList.toggle('show');
}

function closeSidebar() {
  var sb = document.querySelector('.sidebar');
  var ov = document.querySelector('.sidebar-overlay');
  if(sb) sb.classList.remove('open');
  if(ov) ov.classList.remove('show');
}

// ── Page Transitions ──────────────────────────────────────────────
document.addEventListener('click', function(e) {
  var a = e.target.closest('a[href]');
  if(!a) return;
  var href = a.getAttribute('href');
  if(!href || href.startsWith('#') || href.startsWith('http') || href.startsWith('mailto') ||
     a.target === '_blank' || a.hasAttribute('data-no-transition') ||
     e.ctrlKey || e.metaKey || e.shiftKey) return;
  e.preventDefault();
  var content = document.getElementById('main-content');
  if(content) {
    content.classList.add('content-exit');
    setTimeout(function(){ window.location.href = href; }, 150);
  } else {
    window.location.href = href;
  }
});

// ── Utility Functions ─────────────────────────────────────────────
function getLang() {
  var meta = document.querySelector('meta[name="lang"]');
  return (meta && meta.getAttribute('content')) || 'zh';
}

function escapeHtml(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function timeAgo(iso) {
  if(!iso) return '';
  try {
    var d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
    var secs = Math.floor((Date.now() - d.getTime()) / 1000);
    var zh = getLang() === 'zh';
    if(secs < 60) return secs + (zh ? '秒前' : 's ago');
    if(secs < 3600) return Math.floor(secs / 60) + (zh ? '分钟前' : 'm ago');
    if(secs < 86400) return Math.floor(secs / 3600) + (zh ? '小时前' : 'h ago');
    return Math.floor(secs / 86400) + (zh ? '天前' : 'd ago');
  } catch(e) { return iso; }
}

function resultToast(ok, msg) {
  var wrap = document.getElementById('toast-container');
  if(!wrap) return;
  var div = document.createElement('div');
  div.className = 'toast';
  div.style.borderColor = ok ? 'var(--success)' : 'var(--danger)';
  div.innerHTML =
    '<div class="toast-title" style="color:' + (ok ? 'var(--success)' : 'var(--danger)') + '">' +
    escapeHtml(ok ? (getLang() === 'zh' ? '成功' : 'Success') : (getLang() === 'zh' ? '失败' : 'Failed')) +
    '</div><div class="toast-body">' + escapeHtml(msg || '') + '</div>';
  wrap.appendChild(div);
  setTimeout(function(){
    div.style.transition = 'opacity .2s';
    div.style.opacity = '0';
    setTimeout(function(){ div.remove(); }, 220);
  }, 3500);
}

// ── Flash Dismiss ─────────────────────────────────────────────────
document.addEventListener('click', function(e) {
  var btn = e.target.closest('.alert-dismiss');
  if(btn) {
    var alert = btn.closest('.alert');
    if(alert) { alert.style.opacity = '0'; alert.style.transition = 'opacity .15s'; setTimeout(function(){ alert.remove(); }, 150); }
  }
});

// ── Monitor Status ────────────────────────────────────────────────
// 把定时器引用挂模块作用域，pagehide 时能 clearInterval ——和 SSE 同样的
// 道理：活跃定时器在某些浏览器（Safari 尤甚）会阻止 bfcache。
var _monitorTimer = null;

function updateMonitorPausedBanner(d) {
  var banner = document.getElementById('monitor-paused-banner');
  if(banner) banner.classList.toggle('hidden', !!d.running);
  var inline = document.getElementById('system-monitor-paused-note');
  if(inline) inline.classList.toggle('hidden', !!d.running);
}

function updateMaintenanceBanner(d) {
  var banner = document.getElementById('upstream-maintenance-banner');
  if(!banner) return;
  var maintenance = d && d.upstream_maintenance ? d.upstream_maintenance : {};
  var active = !!maintenance.active;
  banner.classList.toggle('hidden', !active);

  var since = document.getElementById('upstream-maintenance-since');
  if(!since) return;
  if(active && maintenance.since) {
    var zh = getLang() === 'zh';
    since.textContent = '· ' + (zh ? '自 ' : 'Since ') + String(maintenance.since);
    since.classList.remove('hidden');
  } else {
    since.textContent = '';
    since.classList.add('hidden');
  }
}

function updateSystemMonitorControls(d) {
  var startBtn = document.getElementById('monitor-start-btn');
  var stopBtn = document.getElementById('monitor-stop-btn');
  var restartBtn = document.getElementById('monitor-restart-btn');
  if(startBtn) startBtn.classList.toggle('hidden', !!d.running);
  if(stopBtn) stopBtn.classList.toggle('hidden', !d.running);
  if(restartBtn) restartBtn.disabled = false;
  var cell = document.getElementById('system-monitor-status-cell');
  if(cell) {
    var zh = getLang() === 'zh';
    if(d.running) {
      cell.innerHTML = '<span class="badge badge-success">' +
        (zh ? '监控运行中' : 'Monitor running') +
        (d.pid ? ' (PID ' + escapeHtml(String(d.pid)) + ')' : '') +
        '</span>';
    } else {
      cell.innerHTML = '<span class="badge badge-danger">' +
        (zh ? '系统暂停' : 'System paused') +
        '</span>';
    }
  }
}

function updateMonitorBadge() {
  var badge = document.getElementById('mon-badge');
  if(!badge) return;
  fetch('/api/status').then(function(r){ return r.json(); }).then(function(d){
    badge.classList.remove('running','stopped');
    badge.classList.add(d.running ? 'running' : 'stopped');
    var txt = badge.querySelector('.mon-text');
    if(txt) {
      var zh = getLang() === 'zh';
      txt.textContent = d.running ? (zh ? '监控运行中' : 'Monitor running') : (zh ? '系统暂停' : 'System paused');
    }
    updateMonitorPausedBanner(d);
    updateMaintenanceBanner(d);
    updateSystemMonitorControls(d);
  }).catch(function(e){ console.error('FlatRadar fetch error:', e); });
}

function controlMonitor(btn, action) {
  var lang = getLang();
  var labels = {
    start: lang === 'zh' ? '启动中...' : 'Starting...',
    stop: lang === 'zh' ? '停止中...' : 'Stopping...',
    restart: lang === 'zh' ? '重启中...' : 'Restarting...'
  };
  if(action === 'stop') {
    var stopMsg = lang === 'zh'
      ? '确定要暂停监控吗？\\n\\n暂停后所有用户都不会收到新房源/状态变更，自动预订也会停止。'
      : 'Pause monitoring?\\n\\nAll users will stop receiving new listing/status updates and auto-booking will stop.';
    if(!confirm(stopMsg)) return;
  } else if(action === 'restart') {
    var restartMsg = lang === 'zh'
      ? '确定要重启监控进程吗？\\n\\n这会中断本轮抓取并重新加载代码。'
      : 'Restart the monitor process?\\n\\nThis interrupts the current scrape and reloads code.';
    if(!confirm(restartMsg)) return;
  }
  var csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
  var orig = btn ? btn.innerHTML : '';
  if(btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner mr-1"></span>' + (labels[action] || labels.restart);
  }
  fetch('/api/monitor/' + action, {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrf },
  }).then(function(r){ return r.json().then(function(d){ d._status = r.status; return d; }); })
    .then(function(d) {
      var ok = !!d.ok;
      var msg = ok ? d.message : (d.error || (lang === 'zh' ? '未知错误' : 'Unknown error'));
      resultToast(ok, msg);
      updateMonitorBadge();
      setTimeout(updateMonitorBadge, 1500);
    })
    .catch(function() {
      resultToast(false, lang === 'zh' ? '请求失败' : 'Request failed');
    })
    .finally(function() {
      if(btn) {
        btn.disabled = false;
        btn.innerHTML = orig;
      }
    });
}

function startMonitorPoll() {
  if(_monitorTimer) return;
  updateMonitorBadge();
  _monitorTimer = setInterval(updateMonitorBadge, 15000);
}

function stopMonitorPoll() {
  if(_monitorTimer) {
    clearInterval(_monitorTimer);
    _monitorTimer = null;
  }
}

startMonitorPoll();

// ── Notification System ───────────────────────────────────────────
var _notifLastId = 0;
var _notifUnread = 0;
var _notifPanelOpen = false;

function updateNotifBadge(count) {
  var b = document.getElementById('notif-badge');
  if(!b) return;
  _notifUnread = Math.max(0, count);
  if(_notifUnread > 0) {
    b.style.display = 'block';
    b.textContent = _notifUnread > 99 ? '99+' : _notifUnread;
  } else {
    b.style.display = 'none';
  }
}

function showToast(n) {
  var wrap = document.getElementById('toast-container');
  if(!wrap) return;
  var d = document.createElement('div');
  d.className = 'toast';
  var bodyHtml = n.body ? '<div class="toast-body">' + escapeHtml(n.body) + '</div>' : '';
  d.innerHTML = '<div class="toast-title">' + escapeHtml(n.title) + '</div>' + bodyHtml;
  if(n.url) { d.style.cursor = 'pointer'; d.onclick = function(){ window.open(n.url, '_blank'); }; }
  wrap.appendChild(d);
  setTimeout(function() {
    d.style.transition = 'opacity .3s';
    d.style.opacity = '0';
    setTimeout(function(){ d.remove(); }, 300);
  }, 5000);
}

function renderNotifications(items) {
  var list = document.getElementById('notif-list');
  if(!list) return;
  if(!items || !items.length) {
    var noNotifs = getLang() === 'zh' ? '暂无通知' : 'No notifications';
    list.innerHTML = '<div class="notif-empty">' + noNotifs + '</div>';
    return;
  }
  list.innerHTML = '';
  items.forEach(function(n) {
    var div = document.createElement('div');
    div.className = 'notif-item' + (n.read ? '' : ' unread');
    if (n.url) {
      div.style.cursor = 'pointer';
      div.addEventListener('click', function() { window.open(n.url, '_blank'); });
    }
    div.innerHTML =
      '<div class="notif-item-title">' + escapeHtml(n.title) + '</div>' +
      (n.body ? '<div class="notif-item-body">' + escapeHtml(n.body) + '</div>' : '') +
      '<div class="notif-item-time">' + timeAgo(n.created_at) + '</div>';
    list.appendChild(div);
  });
}

function loadNotifications() {
  return fetch('/api/notifications?limit=20')
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if(!d.ok) return;
      renderNotifications(d.notifications);
      updateNotifBadge(d.unread);
      if(d.notifications.length)
        _notifLastId = Math.max.apply(null, d.notifications.map(function(n){ return n.id; }));
    }).catch(function(e){ console.error('FlatRadar fetch error:', e); });
}

function toggleNotifications() {
  var panel = document.getElementById('notif-panel');
  if(!panel) return;
  _notifPanelOpen = !_notifPanelOpen;
  if(_notifPanelOpen) { panel.classList.add('open'); loadNotifications(); }
  else { panel.classList.remove('open'); }
}

function markAllRead(e) {
  if(e) e.stopPropagation();
  var csrf = document.querySelector('meta[name="csrf-token"]').getAttribute('content');
  fetch('/api/notifications/read', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  }).then(function(r){ return r.json(); })
    .then(function(){ updateNotifBadge(0); loadNotifications(); })
    .catch(function(e){ console.error('FlatRadar fetch error:', e); });
}

// Close panel when clicking outside
document.addEventListener('click', function(e) {
  var panel = document.getElementById('notif-panel');
  if(!panel || !_notifPanelOpen) return;
  var trigger = document.querySelector('.notif-trigger');
  if(!panel.contains(e.target) && (!trigger || !trigger.contains(e.target))) {
    panel.classList.remove('open');
    _notifPanelOpen = false;
  }
});

// ── SSE ───────────────────────────────────────────────────────────
//
// bfcache 友好的 SSE 生命周期管理：
// - _sseConn       当前 EventSource 引用，方便 pagehide 时 close
// - _sseRetryTimer 失败重连的 setTimeout 引用，必须能取消——否则
//                  pagehide 后这个 timer 在隐藏页面上 fire，又新开一条
//                  EventSource，bfcache 还是被破坏。
//
// 不 close + cancel timer 的话浏览器 bfcache 拒绝缓存当前页（活跃连接 /
// pending timer 都是 bfcache 杀手），用户点"返回"就得整页重拉，单线程
// dev server 还被 SSE 占着 worker，偶尔出现空白卡死。
var _sseConn = null;
var _sseRetryTimer = null;

function connectSSE() {
  if(!window.EventSource) return;
  if(_sseConn) { try { _sseConn.close(); } catch(_){} }
  if(_sseRetryTimer) { clearTimeout(_sseRetryTimer); _sseRetryTimer = null; }
  var src = new EventSource('/api/events?last_id=' + _notifLastId);
  _sseConn = src;
  src.onmessage = function(e) {
    var items;
    try { items = JSON.parse(e.data); } catch(_) { return; }
    if(!items || !items.length) return;
    _notifLastId = items[items.length - 1].id;
    // 只对未读通知弹 toast（新标签页不会重复弹已读的）
    items.filter(function(n){ return !n.read; }).slice(0, 3).forEach(showToast);
    // 从服务端同步真实未读数，避免本地计数不准
    fetch('/api/notifications?limit=1').then(function(r){ return r.json(); }).then(function(d){
      if(d.ok && typeof d.unread === 'number') updateNotifBadge(d.unread);
    }).catch(function(e){ console.error('FlatRadar fetch error:', e); });
    if(_notifPanelOpen) loadNotifications();
  };
  src.onerror = function() {
    try { src.close(); } catch(_){}
    if(_sseConn === src) _sseConn = null;
    // 关键：retry timer 必须可取消。closeSSE() 会清掉它，所以 pagehide
    // 后 10s 内即使本来要重连，也不会真的 fire。
    _sseRetryTimer = setTimeout(function(){
      _sseRetryTimer = null;
      connectSSE();
    }, 10000);
  };
}

function closeSSE() {
  if(_sseConn) {
    try { _sseConn.close(); } catch(_){}
    _sseConn = null;
  }
  if(_sseRetryTimer) {
    clearTimeout(_sseRetryTimer);
    _sseRetryTimer = null;
  }
}

// ── Init ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
  // 仅 admin 加载通知 + SSE（访客不显示通知，避免无意义的 403 请求）
  if(window._isAdmin === true) {
    // Load notifications first, then connect SSE (avoids race where SSE
    // uses _notifLastId=0 before loadNotifications updates it)
    loadNotifications().then(function(){ connectSSE(); });
  }

  // bfcache 友好：导航离开页面前**关掉所有活跃连接和定时器**，浏览器才
  // 会把当前页放进 back-forward cache。Safari 对这块尤其严格——任何一
  // 个活跃 EventSource / setInterval 都让它拒绝 bfcache。
  //
  // - pagehide:  导航离开（含点返回、点链接、关 tab）
  // - pageshow:  导航回到（含从 bfcache 复原）。event.persisted=true 表
  //              示是从 bfcache 复活的，把连接和定时器恢复回来。
  function _onPageHide() {
    closeSSE();
    stopMonitorPoll();
  }
  function _onPageShow(e) {
    if(e.persisted) {
      if(window._isAdmin === true && !_sseConn) connectSSE();
      startMonitorPoll();
    }
  }
  window.addEventListener('pagehide', _onPageHide);
  window.addEventListener('pageshow', _onPageShow);

  // Close sidebar when clicking overlay
  var overlay = document.querySelector('.sidebar-overlay');
  if(overlay) overlay.addEventListener('click', closeSidebar);

  // Close sidebar on Escape
  document.addEventListener('keydown', function(e) {
    if(e.key === 'Escape') closeSidebar();
  });

  // ── Multi-select dropdown ──
  document.querySelectorAll('.multi-select').forEach(function(ms) {
    var trigger  = ms.querySelector('.ms-trigger');
    var dropdown = ms.querySelector('.ms-dropdown');
    var textEl   = ms.querySelector('.ms-text');
    var checkboxes = ms.querySelectorAll('input[type="checkbox"]');
    var placeholder = textEl.textContent.trim() || textEl.getAttribute('data-placeholder') || '';

    // a11y: aria attributes for screen readers
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('role', 'combobox');
    dropdown.setAttribute('role', 'listbox');
    checkboxes.forEach(function(cb) { cb.closest('label')?.setAttribute('role', 'option'); });

	    function position() {
	      dropdown.style.minWidth = Math.max(trigger.offsetWidth, 180) + 'px';
	    }

    function update() {
      var sel = [];
      checkboxes.forEach(function(cb) {
        if (cb.checked) sel.push(cb.parentElement.textContent.trim());
      });
      trigger.querySelectorAll('.ms-tag').forEach(function(t) { t.remove(); });
      if (sel.length === 0) {
        textEl.textContent = placeholder;
        textEl.style.display = '';
      } else {
        textEl.textContent = '';
        textEl.style.display = 'none';
        sel.forEach(function(label, i) {
          var tag = document.createElement('span');
          tag.className = 'ms-tag';
          tag.textContent = label;
          var rm = document.createElement('span');
          rm.className = 'ms-rm';
          rm.textContent = '×';
          rm.onclick = function(e) {
            e.stopPropagation();
            // 找到对应的 checkbox 并取消勾选
            checkboxes.forEach(function(cb) {
              if (cb.parentElement.textContent.trim() === label) cb.checked = false;
            });
            update();
          };
          tag.appendChild(rm);
          trigger.insertBefore(tag, textEl);
        });
      }
    }

    ms._update = function() {
      checkboxes = ms.querySelectorAll('input[type="checkbox"]');
      checkboxes.forEach(function(cb) { cb.addEventListener('change', update); });
      update();
    };

    update();

    function flipDropdown() {
      var rect = trigger.getBoundingClientRect();
      var spaceBelow = window.innerHeight - rect.bottom;
      var spaceAbove = rect.top;
      // Reset to CSS defaults
      dropdown.style.top = '';
      dropdown.style.bottom = '';
      // Flip upward if insufficient space below and more room above
      if (spaceBelow < 240 && spaceAbove > spaceBelow) {
        dropdown.style.top = 'auto';
        dropdown.style.bottom = 'calc(100% + 4px)';
      }
    }

    trigger.addEventListener('click', function(e) {
      e.stopPropagation();
      position();
      ms.classList.toggle('open');
      if (ms.classList.contains('open')) flipDropdown();
    });

    window.addEventListener('resize', function() {
      if (ms.classList.contains('open')) { position(); flipDropdown(); }
    });

    checkboxes.forEach(function(cb) {
      cb.addEventListener('change', update);
    });

    document.addEventListener('click', function(e) {
      if (!ms.contains(e.target)) ms.classList.remove('open');
    });

    // 防止滚轮事件穿透到页面
    dropdown.addEventListener('wheel', function(e) {
      var atTop = dropdown.scrollTop <= 0;
      var atBottom = dropdown.scrollTop + dropdown.clientHeight >= dropdown.scrollHeight - 1;
      if ((atTop && e.deltaY < 0) || (atBottom && e.deltaY > 0)) {
        e.preventDefault();
      }
    }, {passive: false});
  });
});

// ── 公共：手动刷新一个 multi-select 的标签显示 ────────────
// 供 copyNotifFilters 等外部调用，不依赖 init 闭包中的旧 checkbox 引用
window.refreshMultiSelect = function(ms) {
  var cbs = ms.querySelectorAll('input[type="checkbox"]');
  var trigger = ms.querySelector('.ms-trigger');
  var textEl  = ms.querySelector('.ms-text');
  if (!trigger || !textEl) return;
  trigger.querySelectorAll('.ms-tag').forEach(function(t){ t.remove(); });
  var sel = [];
  cbs.forEach(function(cb){
    if (cb.checked) sel.push({label: cb.parentElement.textContent.trim(), cb: cb});
  });
  if (sel.length === 0) {
    textEl.style.display = '';
  } else {
    textEl.style.display = 'none';
    sel.forEach(function(item){
      var tag = document.createElement('span');
      tag.className = 'ms-tag';
      tag.textContent = item.label;
      var rm = document.createElement('span');
      rm.className = 'ms-rm';
      rm.textContent = '×';
      rm.onclick = function(e){
        e.stopPropagation();
        item.cb.checked = false;
        window.refreshMultiSelect(ms);
      };
      tag.appendChild(rm);
      trigger.insertBefore(tag, textEl);
    });
  }
};
