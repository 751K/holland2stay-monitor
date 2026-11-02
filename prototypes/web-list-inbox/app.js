'use strict';
const groups=document.querySelector('#groups'), detail=document.querySelector('#detail'), search=document.querySelector('#search');
let view='all', selected=0;
const escapeText=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function render(){
  const q=search.value.trim().toLowerCase();
  const visible=items.map((item,id)=>({...item,id})).filter(item=>(view==='all'||item.status===view)&&(!q||[item.name,item.city,item.source].some(v=>v.toLowerCase().includes(q))));
  if(!visible.some(item=>item.id===selected)) selected=visible.length?visible[0].id:null;
  const groupNames=[['11 月 2 日',r=>r.found.startsWith('11 月 2 日')],['10 月 31 日',r=>r.found.startsWith('10 月 31 日')],['更早发现',r=>!r.found.startsWith('11 月 2 日')&&!r.found.startsWith('10 月 31 日')]];
  groups.innerHTML=visible.length?groupNames.map(([name,test])=>{const entries=visible.filter(test);if(!entries.length)return '';return `<div class="group"><div class="group-label">${name} <span>${entries.length}</span></div>${entries.map(item=>`<button class="listing ${item.id===selected?'selected':''}" data-id="${item.id}" aria-pressed="${item.id===selected}"><span class="status-icon ${item.status}" aria-hidden="true"></span><span class="listing-body"><span class="listing-title">${escapeText(item.name)}</span><span class="listing-meta">${escapeText(item.city)}<i>·</i>${escapeText(item.source)}<i>·</i>${escapeText(item.area)}</span><span class="listing-bottom"><span class="status-text ${item.status}">${escapeText(item.label)}</span>${item.inferred?'<span class="inferred">系统推测</span>':''}<span class="time">${escapeText(item.found)}</span></span></span><span class="listing-value"><strong>${escapeText(item.price)}</strong><small>来源价格</small></span></button>`).join('')}</div>`}).join(''):'<div class="empty">没有找到符合条件的房源。<br><span>试试调整搜索或筛选条件。</span></div>';
  document.querySelector('#count').textContent=visible.length;
  document.querySelector('#result-count').textContent=`${visible.length} 条房源 · 快照数据`;
  if(selected===null){detail.innerHTML='<div class="detail-empty">选择一条房源查看详情</div>';return}
  const item=items[selected];
  detail.innerHTML=`<div class="detail-top"><span>房源详情</span><span class="detail-snapshot">快照</span></div><div class="detail-body"><div class="detail-name">${escapeText(item.name)}</div><p class="detail-location">${escapeText(item.city)} <span>·</span> ${escapeText(item.source)}</p><div class="detail-price">${escapeText(item.price)} <span>来源价格</span></div><div class="detail-status"><span class="status-icon ${item.status}"></span><strong>${escapeText(item.label)}</strong>${item.inferred?'<span class="detail-inferred">系统推测</span>':''}</div><div class="separator"></div><h3>房源信息</h3><dl><div><dt>面积</dt><dd>${escapeText(item.area)}</dd></div><div><dt>可入住日期</dt><dd>${item.available==='—'?'未提供':escapeText(item.available)}</dd></div><div><dt>首次发现</dt><dd>${escapeText(item.found)}</dd></div><div><dt>来源平台</dt><dd>${escapeText(item.source)}</dd></div></dl><div class="detail-note">这是采样时记录的信息，房源实际状态可能已经变化。</div></div>`;
  groups.querySelectorAll('[data-id]').forEach(button=>button.addEventListener('click',()=>{selected=Number(button.dataset.id);render();groups.querySelector(`[data-id="${selected}"]`)?.focus()}));
}
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{view=button.dataset.view;document.querySelectorAll('[data-view]').forEach(b=>{const active=b===button;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active))});const title=button.querySelectorAll('span')[1].textContent;document.querySelector('#page-title').textContent=title;document.querySelector('#breadcrumb').textContent=title;render()}));
search.addEventListener('input',render);render();
