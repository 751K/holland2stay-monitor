/* Calendar navigation replaces server-rendered fragments, preserving the page
   shell and native links as a fallback when JavaScript is unavailable. */
(function () {
  'use strict';
  const status = document.getElementById('calendar-load-status');
  if (!status) return;
  const zh = document.documentElement.lang === 'zh';
  const label = (cn,en) => zh ? cn : en;
  let requestNumber = 0;
  let pending = null;
  let renderedURL = location.href;

  function initKeyboard(focusDate) {
    const days = [...document.querySelectorAll('.calendar-day')];
    const active = days.find(link => link.hasAttribute('aria-current')) || days.find(link => link.classList.contains('today')) || days[0];
    days.forEach(link => { link.tabIndex = link === active ? 0 : -1; });
    if (focusDate) active?.focus({preventScroll:true});
  }

  function updateContext(url,selection) {
    // Platform/view links and native forms must keep the newly displayed date,
    // rather than the month embedded in the original document.
    document.querySelectorAll('.sidebar a[href],.mail-tools a[href]').forEach(link => {
      const target = new URL(link.href,location.href);
      if (target.origin !== url.origin || target.pathname !== url.pathname) return;
      ['month','date','undated','selected'].forEach(key => target.searchParams.delete(key));
      Object.entries(selection).forEach(([key,value]) => { if (value != null) target.searchParams.set(key,value); });
      link.href = target.href;
    });
    document.querySelectorAll('#mail-filter form,.mail-search').forEach(form => {
      form.querySelectorAll('input[type="hidden"]').forEach(input => {
        if (['month','date','undated'].includes(input.name)) input.remove();
      });
      Object.entries(selection).forEach(([key,value]) => {
        if (value == null) return;
        const input = document.createElement('input');
        input.type = 'hidden'; input.name = key; input.value = value;
        form.append(input);
      });
    });
  }

  function apply(payload,url,focusDate) {
    const parsed = new DOMParser().parseFromString(payload.html,'text/html');
    const next = parsed.querySelector('.calendar-workspace');
    const workspace = document.querySelector('.calendar-workspace');
    if (!next || !workspace || !Array.isArray(payload.items) || !payload.selection || typeof payload.selection.month !== 'string') throw new Error('Invalid calendar response');
    const currentMonth = workspace.querySelector('.calendar-month');
    const newMonth = next.querySelector('.calendar-month');
    const nextSide = next.querySelector('.calendar-side');
    if (!currentMonth || !newMonth || !nextSide) throw new Error('Invalid calendar fragment');
    if (workspace.dataset.calendarMonth === next.dataset.calendarMonth) {
      // Retain the grid nodes and scroll position for ordinary day selection.
      const replacements = new Map([...newMonth.querySelectorAll('.calendar-day')].map(link => [link.dataset.calendarDate,link]));
      currentMonth.querySelectorAll('.calendar-day').forEach(link => {
        const replacement = replacements.get(link.dataset.calendarDate);
        if (!replacement) return;
        link.className = replacement.className;
        link.href = replacement.href;
        link.setAttribute('aria-label',replacement.getAttribute('aria-label'));
        if (replacement.hasAttribute('aria-current')) link.setAttribute('aria-current','date');
        else link.removeAttribute('aria-current');
        if (link.innerHTML !== replacement.innerHTML) link.innerHTML = replacement.innerHTML;
      });
      ['.calendar-month-nav','.calendar-month-footer','.calendar-month-empty'].forEach(selector => {
        const old = currentMonth.querySelector(selector), fresh = newMonth.querySelector(selector);
        if (old && fresh) old.replaceWith(fresh);
        else if (old) old.remove();
        else if (fresh) currentMonth.append(fresh);
      });
    } else {
      currentMonth.replaceWith(newMonth);
      workspace.dataset.calendarMonth = next.dataset.calendarMonth;
    }
    workspace.querySelector('.calendar-side').replaceWith(nextSide);
    updateContext(url,payload.selection);
    const unread = document.getElementById('inbox-unread-count');
    if (unread && Number.isInteger(payload.unread_count)) unread.textContent = Math.min(Number(unread.textContent),payload.unread_count);
    document.dispatchEvent(new CustomEvent('inbox:items-updated',{detail:{items:payload.items}}));
    initKeyboard(focusDate);
  }

  async function navigate(href,{push=true,focusDate=false}={}) {
    const url = new URL(href,location.href);
    if (url.origin !== location.origin || url.pathname !== location.pathname) return;
    const sequence = ++requestNumber;
    pending?.abort();
    const controller = new AbortController(); pending = controller;
    const timeout = setTimeout(() => controller.abort(),15000);
    document.querySelector('.calendar-workspace')?.setAttribute('aria-busy','true');
    status.hidden = true; status.replaceChildren();
    try {
      const response = await fetch(url.href,{headers:{Accept:'application/json'},credentials:'same-origin',cache:'no-store',signal:controller.signal});
      if (!response.ok || response.redirected || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Calendar request failed');
      const payload = await response.json();
      if (sequence !== requestNumber) return;
      apply(payload,url,focusDate);
      if (push && url.href !== location.href) history.pushState(null,'',url);
      renderedURL = url.href;
    } catch {
      if (sequence !== requestNumber) return;
      // Keep the complete previous view on failure, including its URL.
      if (!push) history.replaceState(null,'',renderedURL);
      status.hidden = false;
      status.append(document.createTextNode(label('日历加载失败，当前内容已保留。','Calendar could not load. Your current view is preserved.')));
      const retry = document.createElement('button');
      retry.type = 'button'; retry.textContent = label('重试','Retry');
      retry.addEventListener('click',() => navigate(href,{push,focusDate}));
      status.append(retry);
    } finally {
      clearTimeout(timeout);
      if (sequence === requestNumber) document.querySelector('.calendar-workspace')?.removeAttribute('aria-busy');
    }
  }

  document.addEventListener('click',event => {
    const link = event.target.closest('a[data-calendar-nav]');
    if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(link.href,{focusDate:event.detail===0});
  });
  window.addEventListener('popstate',() => navigate(location.href,{push:false}));

  document.addEventListener('inbox:listing-selected',() => {
    renderedURL = location.href;
    const agenda = document.getElementById('calendar-agenda'), detail = document.getElementById('mail-detail');
    agenda.hidden = true; detail.hidden = false;
    detail.querySelector('[data-close-detail]')?.focus();
    if (window.matchMedia('(max-width:750px)').matches) detail.scrollIntoView({block:'start'});
  });
  document.addEventListener('inbox:detail-closed',() => {
    renderedURL = location.href;
    const agenda = document.getElementById('calendar-agenda'), detail = document.getElementById('mail-detail');
    detail.hidden = true; agenda.hidden = false;
    agenda.querySelector('[data-listing-id][aria-current="true"]')?.focus();
  });
  document.addEventListener('keydown',event => {
    const link = event.target.closest('.calendar-day');
    if (!link) return;
    const days = [...document.querySelectorAll('.calendar-day')];
    const index = days.indexOf(link), column = link.closest('td').cellIndex;
    const target = {ArrowLeft:index-1,ArrowRight:index+1,ArrowUp:index-7,ArrowDown:index+7,Home:index-column,End:index+6-column}[event.key];
    if (target == null) return;
    event.preventDefault();
    const next = days[Math.max(0,Math.min(days.length-1,target))];
    days.forEach(day => { day.tabIndex = day === next ? 0 : -1; }); next.focus();
  });
  initKeyboard(false);
})();
