'use strict';
const platformSpecs = [['holland2stay','Holland2Stay'],['plaza','Plaza'],['ourdomain','OurDomain'],['studentexperience','Student Experience'],['xior','Xior'],['magis','Magis'],['ourcampus','OurCampus']];
const platformMap = Object.fromEntries(platformSpecs);
const sourceIds = platformSpecs.map(([id]) => id);
const statusMap = {'Available to book':['available','可预订'],'Reserved':['reserved','已预留'],'Occupied':['occupied','已入住'],'Not available':['occupied','不可申请']};
const items = snapshot.listings.map((row,id) => ({...row,id,platform:platformMap[row.source] || row.source,statusInfo:statusMap[row.status] || ['occupied',row.status || '状态未知']})).sort((a,b) => Date.parse(b.first_seen)-Date.parse(a.first_seen));
const dateFormat = new Intl.DateTimeFormat('zh-CN',{timeZone:'Europe/Amsterdam',year:'numeric',month:'numeric',day:'numeric'});
const timeFormat = new Intl.DateTimeFormat('zh-CN',{timeZone:'Europe/Amsterdam',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
const dateTime = s => `${dateFormat.format(new Date(s))} ${timeFormat.format(new Date(s))}`;
const esc = s => String(s ?? '').replace(/[&<>"']/g,c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const $ = selector => document.querySelector(selector);
const groups = $('#groups'), detail = $('#detail'), search = $('#search'), platforms = $('#platforms');
const rolePreview = $('#preview-role'), filterDialog = $('#filter-dialog'), filterForm = $('#filter-form');
const entryDialog = $('#entry-dialog'), loginDialog = $('#login-dialog');
const storageKey = 'flatradar-personal-inbox-demo-v1';
const blankFilters = () => ({city:'',minPrice:null,maxPrice:null,minArea:null,sources:[...sourceIds]});
const cloneFilters = filters => ({...filters,sources:[...filters.sources]});
let personalFilters = {...blankFilters(),maxPrice:1500,minArea:20};
let personalNote = '演示条件 · 尚未保存';
try {
  const saved = JSON.parse(localStorage.getItem(storageKey));
  if (InboxModel.validFilters(saved,sourceIds)) { personalFilters = saved; personalNote = '个人条件 · 已保存到本机'; }
} catch { /* Browsing still works if local storage is unavailable. */ }
let guestFilters = blankFilters();
let role = 'user', view = 'all', selected = null;
const activeFilters = () => role === 'guest' ? guestFilters : personalFilters;
const platformIcon = '<svg class="platform-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.45" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 8.3 10 3l6.5 5.3M5.3 7.4V17h9.4V7.4M8.2 17v-4.1h3.6V17"/></svg>';
$('#snapshot-time').textContent = `采样于 ${dateTime(snapshot.capturedAt)}`;
$('#city-input').innerHTML = '<option value="">全荷兰</option>' + InboxModel.cities.map(city => `<option value="${esc(city)}">${esc(city)}</option>`).join('');
$('#source-choices').innerHTML = platformSpecs.map(([id,name]) => `<label><input type="checkbox" name="sources" value="${id}"><span>${name}</span></label>`).join('');

function filterSummary(filters) {
  const parts = [filters.city || '全荷兰'];
  const money = number => `€${number.toLocaleString('en-US')}`;
  if (filters.minPrice != null && filters.maxPrice != null) parts.push(`${money(filters.minPrice)}–${money(filters.maxPrice)}`);
  else if (filters.minPrice != null) parts.push(`≥ ${money(filters.minPrice)}`);
  else if (filters.maxPrice != null) parts.push(`≤ ${money(filters.maxPrice)}`);
  else parts.push('租金不限');
  if (filters.minArea != null) parts.push(`≥ ${filters.minArea} m²`);
  if (filters.sources.length !== sourceIds.length) parts.push(`${filters.sources.length} 个平台`);
  return parts.join(' · ');
}
function area(item) { return item.area_value == null ? '' : `${item.area_value} m²`; }
function render() {
  const filters = activeFilters();
  const matched = items.filter(item => InboxModel.matches(item,filters));
  const q = search.value.trim().toLowerCase();
  const visible = matched.filter(item => (view === 'all' || (view === 'available' ? item.statusInfo[0] === 'available' : item.source === view)) && (!q || [item.name,item.city,item.platform].some(value => (value || '').toLowerCase().includes(q))));
  if (!visible.some(item => item.id === selected)) selected = visible[0]?.id ?? null;
  $('#all-count').textContent = matched.length;
  $('#available-count').textContent = matched.filter(item => item.statusInfo[0] === 'available').length;
  platforms.innerHTML = platformSpecs.filter(([id]) => filters.sources.includes(id)).map(([id,name]) => `<button class="nav platform-nav" data-view="${id}" aria-pressed="false">${platformIcon}<span class="platform-name">${name}</span><b>${matched.filter(item => item.source === id).length}</b></button>`).join('');
  document.querySelectorAll('[data-view]').forEach(button => {
    const active = button.dataset.view === view;
    button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active));
  });
  $('#page-title').textContent = view === 'all' ? (role === 'guest' ? '浏览房源' : '我的房源') : view === 'available' ? '可预订' : platformMap[view];
  $('#filter-summary').innerHTML = `${esc(filterSummary(filters))}<span class="edit-filters">调整筛选 <span aria-hidden="true">↗</span></span>`;
  $('#scope-note').textContent = role === 'guest' ? '临时筛选 · 登录后可保留为个人条件' : personalNote;
  $('#count').textContent = visible.length;
  $('#result-count').textContent = `匹配样本共 ${matched.length} 条 · 当前显示 ${visible.length} 条 · 非实时数据`;
  const grouped = new Map();
  visible.forEach(item => { const day = dateFormat.format(new Date(item.first_seen)); if (!grouped.has(day)) grouped.set(day,[]); grouped.get(day).push(item); });
  groups.innerHTML = visible.length ? [...grouped].map(([day,entries]) => `<div class="group"><div class="group-label">${esc(day)} <span>${entries.length}</span></div>${entries.map(item => `<button class="listing ${item.id === selected ? 'selected' : ''}" data-id="${item.id}" aria-pressed="${item.id === selected}"><span class="listing-body"><span class="listing-title">${esc(item.name || '未命名房源')}</span><span class="listing-meta">${[item.city,item.platform,area(item)].filter(Boolean).map(esc).join('<i>·</i>')}</span><span class="listing-bottom"><span class="status-text ${item.statusInfo[0]}">${esc(item.statusInfo[1])}</span>${item.status_is_inferred ? '<span class="inferred">系统推测</span>' : ''}<span class="time">${esc(timeFormat.format(new Date(item.first_seen)))}</span></span></span><span class="listing-value"><strong>${esc(item.price_raw || '价格未提供')}</strong><small>来源价格</small></span></button>`).join('')}</div>`).join('') : `<div class="empty"><strong>${q ? '没有找到相关房源' : '目前没有符合条件的房源'}</strong><span>${q ? '搜索仅查找当前范围内的房源。' : '可以调整筛选条件，或切换到其他平台。'}</span><button class="text-button" type="button" ${q ? 'data-clear-search' : 'data-open-filters'}>${q ? '清除搜索' : '调整筛选'}</button></div>`;
  renderDetail(visible.find(item => item.id === selected));
}
function renderDetail(item) {
  if (!item) { detail.innerHTML = '<div class="detail-empty">有匹配房源后，可在这里查看详情。</div>'; return; }
  let href = ''; try { const url = new URL(item.url); if (url.protocol === 'https:') href = url.href; } catch {}
  detail.innerHTML = `<div class="detail-top"><span>房源详情</span><span class="detail-snapshot">快照</span></div><div class="detail-body"><div class="detail-name">${esc(item.name || '未命名房源')}</div><p class="detail-location">${esc(item.city || '城市未提供')} <span>·</span> ${esc(item.platform)}</p><div class="detail-price">${esc(item.price_raw || '价格未提供')} <span>来源价格</span></div><div class="detail-status"><strong>${esc(item.statusInfo[1])}</strong>${item.status_is_inferred ? '<span class="detail-inferred">系统推测</span>' : ''}</div><div class="separator"></div><h3>房源信息</h3><dl><div><dt>面积</dt><dd>${esc(area(item) || '未提供')}</dd></div><div><dt>可入住日期</dt><dd>${esc(item.available_from || '未提供')}</dd></div><div><dt>首次发现</dt><dd>${esc(dateTime(item.first_seen))}</dd></div><div><dt>来源平台</dt><dd>${esc(item.platform)}</dd></div></dl>${href ? `<a class="source-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">查看来源平台 <span aria-hidden="true">↗</span></a>` : ''}<div class="detail-note">这是采样时记录的信息，房源实际状态可能已经变化。</div></div>`;
}

function fillFilterForm(filters) {
  ['city','minPrice','maxPrice','minArea'].forEach(key => { filterForm.elements.namedItem(key).value = filters[key] ?? ''; });
  filterForm.querySelectorAll('[name="sources"]').forEach(input => { input.checked = filters.sources.includes(input.value); });
  $('#filter-error').hidden = true;
}
function openFilters() {
  fillFilterForm(activeFilters());
  const guest = role === 'guest';
  $('#filter-title').textContent = guest ? '临时筛选' : '筛选与通知';
  $('#filter-kicker').textContent = guest ? '浏览房源' : '个人收件箱';
  $('#filter-description').textContent = guest ? '调整当前浏览范围。登录演示身份后，可以将这些条件带入个人收件箱。' : '这些条件决定你在各个平台收件箱中看到的房源。';
  $('#notification-note').hidden = guest;
  $('#filter-save-note').textContent = guest ? '仅影响本次游客浏览，不会修改个人条件。' : '仅保存到当前浏览器，不修改服务器上的个人配置。';
  $('#apply-filters').textContent = guest ? '应用临时筛选' : '保存并应用';
  filterDialog.showModal();
}
function savePersonalFilters() {
  try { localStorage.setItem(storageKey,JSON.stringify(personalFilters)); personalNote = '个人条件 · 已保存到本机'; }
  catch { personalNote = '个人条件 · 本次有效，浏览器无法保存'; }
}
filterForm.addEventListener('submit',event => {
  event.preventDefault();
  const form = new FormData(filterForm);
  const numberOrNull = key => form.get(key).trim() === '' ? null : Number(form.get(key));
  const candidate = {city:form.get('city'),minPrice:numberOrNull('minPrice'),maxPrice:numberOrNull('maxPrice'),minArea:numberOrNull('minArea'),sources:form.getAll('sources')};
  if (!InboxModel.validFilters(candidate,sourceIds)) {
    $('#filter-error').textContent = !candidate.sources.length ? '请至少选择一个平台。' : '最高租金不能低于最低租金；数值必须大于或等于零。';
    $('#filter-error').hidden = false; return;
  }
  if (role === 'guest') guestFilters = candidate;
  else { personalFilters = candidate; savePersonalFilters(); }
  if (!['all','available',...candidate.sources].includes(view)) view = 'all';
  filterDialog.close(); render();
});
$('#filter-summary').addEventListener('click',openFilters);
$('#filter-entry').addEventListener('click',openFilters);
$('#cancel-filters').addEventListener('click',() => filterDialog.close());
$('#reset-filters').addEventListener('click',() => fillFilterForm(blankFilters()));
search.addEventListener('input',render);
groups.addEventListener('click',event => {
  const button = event.target.closest('[data-id]');
  if (button) { selected = Number(button.dataset.id); render(); groups.querySelector(`[data-id="${selected}"]`)?.focus(); }
  if (event.target.closest('[data-open-filters]')) openFilters();
  if (event.target.closest('[data-clear-search]')) { search.value = ''; render(); search.focus(); }
});
$('#mail-navigation').addEventListener('click',event => {
  const button = event.target.closest('[data-view]');
  if (button) { view = button.dataset.view; render(); groups.scrollTop = 0; }
});

const entries = {
  calendar:{title:'日历',description:'日历将使用当前收件箱的同一套筛选条件，按可入住日期展示房源。此视图尚未设计。'},
  map:{title:'地图',description:'地图将使用当前收件箱的同一套筛选条件。当前快照不包含可靠坐标，此视图尚未接入。'},
  stats:{title:'房源统计',description:'此处将围绕当前筛选范围展示房源统计。当前仅有少量采样记录，不能代表市场全量。'},
  account:{title:'我的账号',description:'个人平台账号与预订配置属于自己的账号。管理员也保留此入口。账号页面尚未接入。'},
  theme:{title:'外观',description:'当前原型仅提供浅色主题。'},
  language:{title:'语言',description:'当前原型仅展示中文布局。'},
  support:{title:'帮助与支持',description:'使用说明和支持渠道的入口。'},
  donate:{title:'支持项目',description:'支持 FlatRadar 项目的入口。'},
  privacy:{title:'隐私条款',description:'正式服务的数据处理说明与隐私条款入口。'},
  terms:{title:'使用条款',description:'正式服务的使用条款入口。'},
  logout:{title:'退出登录',description:'原型没有真实登录会话。可通过顶部“身份预览”切换到游客。'}
};
const adminModules = {
  users:{title:'用户管理',description:'管理用户及其配置。'}, settings:{title:'系统设置',description:'管理运行参数，区分已保存、已生效和需要重启的修改。'},
  devices:{title:'App 账号与设备',description:'管理 App 账号、设备授权与推送测试。'}, monitoring:{title:'监控',description:'查看各平台抓取情况、受影响范围和运行状态。'},
  logs:{title:'日志',description:'查询抓取、通知和预订的运行日志。'}, crashes:{title:'崩溃报告',description:'查看应用崩溃记录和诊断信息。'},
  system:{title:'系统信息',description:'查看部署版本与系统运行信息。'}, events:{title:'系统事件',description:'查看管理员需要处理的系统通知与异常事件。'}
};
$('#admin-nav').innerHTML = Object.entries(adminModules).map(([key,module]) => `<button class="nav" type="button" data-admin="${key}">${module.title}</button>`).join('');
$('#more-nav').innerHTML = ['theme','language','support','donate','privacy','terms','logout'].map(key => `<button class="nav" type="button" data-entry="${key}" ${key === 'logout' ? 'id="logout-entry"' : ''}>${entries[key].title}</button>`).join('');
function openEntry(key) {
  const entry = entries[key]; if (!entry) return;
  $('#entry-title').textContent = entry.title; $('#entry-description').textContent = entry.description; entryDialog.showModal();
}
function openManagement(key = null) {
  if (role !== 'admin') return;
  $('#mail-navigation').hidden = true; $('#mail-main').hidden = true;
  $('#management-navigation').hidden = false; $('#management-main').hidden = false; $('#admin-entry').hidden = true;
  const module = adminModules[key];
  $('#identity-caption').textContent = '管理工作区 · 本机演示';
  $('#admin-title').textContent = module?.title || '管理后台';
  $('#admin-description').textContent = module?.description || '系统管理与个人房源分开呈现。';
  $('#admin-content').innerHTML = module ? '<div class="admin-placeholder"><h2>此管理页面尚未接入</h2><p>这里展示入口与导航关系，不读取或修改服务器数据。</p><button class="text-button" type="button" data-admin-home>返回管理首页</button></div>' : '<p class="admin-preview-note">管理工作区预览 · 以下模块尚未连接管理接口</p><div class="admin-destinations">' + Object.entries(adminModules).map(([id,item]) => `<button type="button" data-admin="${id}"><strong>${item.title}<span aria-hidden="true">↗</span></strong><span>${item.description}</span></button>`).join('') + '</div>';
  document.querySelectorAll('#admin-nav [data-admin]').forEach(button => { const active = button.dataset.admin === key; button.classList.toggle('active',active); if (active) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current'); });
}
function returnToMail() {
  $('#mail-navigation').hidden = false; $('#mail-main').hidden = false;
  $('#management-navigation').hidden = true; $('#management-main').hidden = true; $('#admin-entry').hidden = role !== 'admin';
  $('#identity-caption').textContent = role === 'guest' ? '游客浏览 · 本次临时条件' : '个人收件箱 · 本机演示';
  render();
}
function setRole(nextRole) {
  role = nextRole; rolePreview.value = role; view = 'all'; selected = null;
  entryDialog.close(); filterDialog.close(); loginDialog.close();
  const guest = role === 'guest';
  $('#mailbox-label').textContent = guest ? '房源浏览' : '我的房源';
  $('#all-label').textContent = guest ? '全部房源' : '全部匹配';
  $('#platform-count-label').textContent = guest ? '筛选样本' : '匹配样本';
  $('#filter-entry').textContent = guest ? '临时筛选' : '筛选与通知';
  $('#account-entry').textContent = guest ? '登录 / 注册' : '我的账号';
  $('#identity-caption').textContent = guest ? '游客浏览 · 本次临时条件' : '个人收件箱 · 本机演示';
  $('#logout-entry').hidden = guest;
  search.placeholder = guest ? '搜索当前房源' : '搜索匹配房源';
  returnToMail();
}
rolePreview.addEventListener('change',() => setRole(rolePreview.value));
$('#admin-entry').addEventListener('click',() => openManagement());
$('#back-to-mail').addEventListener('click',returnToMail);
$('#account-entry').addEventListener('click',() => role === 'guest' ? loginDialog.showModal() : openEntry('account'));
$('#cancel-login').addEventListener('click',() => loginDialog.close());
$('#demo-login').addEventListener('click',() => { personalFilters = cloneFilters(guestFilters); personalNote = '已带入游客条件 · 尚未保存'; setRole('user'); });
document.addEventListener('click',event => {
  const entryButton = event.target.closest('[data-entry]'); if (entryButton) openEntry(entryButton.dataset.entry);
  const adminButton = event.target.closest('[data-admin]'); if (adminButton) openManagement(adminButton.dataset.admin);
  if (event.target.closest('[data-admin-home]')) openManagement();
});
setRole('user');
