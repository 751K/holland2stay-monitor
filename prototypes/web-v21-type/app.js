'use strict';
// Example data illustrates existing listing fields; it does not represent live listings.
const listings = [
  {name:'示例房源 A', building:'示例楼盘 A', city:'Eindhoven', source:'Holland2Stay', status:'available', statusText:'可预订', price:'€995', area:'26 m²', found:'12 分钟前', move:'2026 年 12 月'},
  {name:'示例房源 B', building:'示例楼盘 B', city:'Utrecht', source:'Plaza', status:'lottery', statusText:'抽签中', price:'€820', area:'21 m²', found:'1 小时前', move:'入住日期待确认'},
  {name:'示例房源 C', building:'示例楼盘 C', city:'Amsterdam', source:'Magis', status:'available', statusText:'可预订', price:'€1,180', area:'31 m²', found:'3 小时前', move:'2027 年 1 月'},
  {name:'示例户型 D', building:'示例楼盘 D', city:'Leiden', source:'Student Experience', status:'available', statusText:'可预订', price:'€1,250 起', area:'20–25 m²', found:'昨天', move:'入住日期待确认'},
  {name:'示例房源 E', building:'示例楼盘 E', city:'Eindhoven', source:'Holland2Stay', status:'reserved', statusText:'已预留 · 推测', price:'€1,060', area:'28 m²', found:'昨天', move:'入住日期待确认'}
];
let activeFilter = 'all';
const rows = document.querySelector('#listing-rows');
const search = document.querySelector('#search');
function render() {
  const query = search.value.trim().toLowerCase();
  const filtered = listings.filter(item => (activeFilter === 'all' || item.status === activeFilter) && [item.name,item.building,item.city,item.source].some(value => value.toLowerCase().includes(query)));
  rows.innerHTML = filtered.length ? filtered.map(item => `<article class="listing"><div class="listing-main"><div class="listing-title"><span class="listing-symbol ${item.status}" aria-hidden="true"></span><h3>${item.name}</h3><span class="found">${item.found}</span></div><div class="listing-sub">${item.building}<span>·</span>${item.city}<span>·</span>${item.source}</div><div class="listing-details">${item.area}<span>·</span>${item.move}</div></div><div class="listing-status ${item.status}"><span class="status-mark"></span>${item.statusText}</div><div class="listing-price">${item.price}<small> / 月</small></div></article>`).join('') : '<p class="empty">没有找到符合条件的示例房源。</p>';
  document.querySelector('#result-count').textContent = `显示 ${filtered.length} 条示例房源`;
}
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
  activeFilter = button.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach(item => { const selected = item === button; item.classList.toggle('selected', selected); item.setAttribute('aria-pressed', String(selected)); });
  render();
}));
search.addEventListener('input', render);
const menu = document.querySelector('.menu-button'), sidebar = document.querySelector('.sidebar');
menu.addEventListener('click', () => { const open = menu.getAttribute('aria-expanded') !== 'true'; menu.setAttribute('aria-expanded', String(open)); menu.setAttribute('aria-label', open ? '关闭导航' : '打开导航'); sidebar.classList.toggle('open', open); });
sidebar.querySelectorAll('a').forEach(link => link.addEventListener('click', () => {sidebar.classList.remove('open');menu.setAttribute('aria-expanded','false');menu.setAttribute('aria-label','打开导航');}));
render();
