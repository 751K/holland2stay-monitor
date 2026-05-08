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
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
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

// ── Flash Dismiss ─────────────────────────────────────────────────
document.addEventListener('click', function(e) {
  var btn = e.target.closest('.alert-dismiss');
  if(btn) {
    var alert = btn.closest('.alert');
    if(alert) { alert.style.opacity = '0'; alert.style.transition = 'opacity .15s'; setTimeout(function(){ alert.remove(); }, 150); }
  }
});

// ── Monitor Status ────────────────────────────────────────────────
function updateMonitorBadge() {
  var badge = document.getElementById('mon-badge');
  if(!badge) return;
  fetch('/api/status').then(function(r){ return r.json(); }).then(function(d){
    badge.classList.remove('running','stopped');
    badge.classList.add(d.running ? 'running' : 'stopped');
    var txt = badge.querySelector('.mon-text');
    if(txt) {
      var zh = getLang() === 'zh';
      txt.textContent = d.running ? (zh ? '监控运行中' : 'Monitor running') : (zh ? '监控未启动' : 'Monitor stopped');
    }
  }).catch(function(){});
}
updateMonitorBadge();
setInterval(updateMonitorBadge, 15000);

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
    }).catch(function(){});
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
    .catch(function(){});
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
function connectSSE() {
  if(!window.EventSource) return;
  var src = new EventSource('/api/events?last_id=' + _notifLastId);
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
    }).catch(function(){});
    if(_notifPanelOpen) loadNotifications();
  };
  src.onerror = function() {
    src.close();
    setTimeout(connectSSE, 10000);
  };
}

// ── Init ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
  // Load notifications first, then connect SSE (avoids race where SSE
  // uses _notifLastId=0 before loadNotifications updates it)
  loadNotifications().then(function(){ connectSSE(); });

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

    function position() {
      var r = trigger.getBoundingClientRect();
      dropdown.style.top  = (r.bottom + 4) + 'px';
      dropdown.style.left = r.left + 'px';
      dropdown.style.minWidth = r.width + 'px';
    }

    function update() {
      var sel = [];
      checkboxes.forEach(function(cb) {
        if (cb.checked) sel.push(cb.parentElement.textContent.trim());
      });
      trigger.querySelectorAll('.ms-tag').forEach(function(t) { t.remove(); });
      if (sel.length === 0) {
        textEl.textContent = '';
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

    trigger.addEventListener('click', function(e) {
      e.stopPropagation();
      position();
      ms.classList.toggle('open');
    });

    window.addEventListener('resize', function() {
      if (ms.classList.contains('open')) position();
    });

    checkboxes.forEach(function(cb) {
      cb.addEventListener('change', update);
    });

    document.addEventListener('click', function(e) {
      if (!ms.contains(e.target)) ms.classList.remove('open');
    });
  });
});
