(() => {
  'use strict';
  // Synthetic examples illustrate existing listing fields, not live properties.
  const listings = [
    { name:'示例房源 A', nameEn:'Example listing A', building:'示例楼盘 A', buildingEn:'Example building A', city:'Eindhoven', source:'Holland2Stay', status:'available', rent:995, area:'26 m²', foundZh:'12 分钟前', foundEn:'12 minutes ago', startZh:'2026 年 12 月', startEn:'December 2026' },
    { name:'示例房源 B', nameEn:'Example listing B', building:'示例楼盘 B', buildingEn:'Example building B', city:'Utrecht', source:'Plaza', status:'lottery', rent:820, area:'21 m²', foundZh:'1 小时前', foundEn:'1 hour ago', startZh:'日期待确认', startEn:'Date to be confirmed' },
    { name:'示例房源 C', nameEn:'Example listing C', building:'示例楼盘 C', buildingEn:'Example building C', city:'Amsterdam', source:'Magis', status:'available', rent:1180, area:'31 m²', foundZh:'3 小时前', foundEn:'3 hours ago', startZh:'2027 年 1 月', startEn:'January 2027' },
    { name:'示例户型 D', nameEn:'Example room type D', building:'示例楼盘 D', buildingEn:'Example building D', city:'Leiden', source:'Student Experience', status:'available', rent:1250, area:'20–25 m²', foundZh:'昨天', foundEn:'Yesterday', startZh:'日期待确认', startEn:'Date to be confirmed', starting:true },
    { name:'示例房源 E', nameEn:'Example listing E', building:'示例楼盘 E', buildingEn:'Example building E', city:'Eindhoven', source:'Holland2Stay', status:'reserved', rent:1060, area:'28 m²', foundZh:'昨天', foundEn:'Yesterday', startZh:'日期待确认', startEn:'Date to be confirmed', inferred:true }
  ];
  const words={zh:{available:'可预订',lottery:'抽签中',reserved:'已预留',inferred:'系统推测',monthly:'/ 月',starting:'起价',found:'发现于',move:'入住',count:n=>`${n} 条示例房源`,dark:'深色模式',light:'浅色模式',open:'打开导航',close:'关闭导航'},en:{available:'Available',lottery:'Lottery',reserved:'Reserved',inferred:'Inferred',monthly:'/ month',starting:'From',found:'Found',move:'Move-in',count:n=>`${n} example ${n===1?'listing':'listings'}`,dark:'Dark mode',light:'Light mode',open:'Open navigation',close:'Close navigation'}};
  let lang=localStorage.getItem('flatradar-prototype-language')==='en'?'en':'zh';
  let theme=localStorage.getItem('flatradar-prototype-theme')==='dark'?'dark':'light';
  const root=document.documentElement, themeButton=document.getElementById('theme'), languageButton=document.getElementById('language'), menu=document.querySelector('.menu-button'), sidebar=document.getElementById('sidebar'), scrim=document.querySelector('.sidebar-scrim'), filters=document.getElementById('filters'), mobile=window.matchMedia('(max-width: 720px)');
  function text(zh,en){return lang==='zh'?zh:en}
  function safe(value){return String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
  function listingHTML(item){
    const w=words[lang], price=item.starting&&lang==='en'?`From €${item.rent.toLocaleString('en-US')}`:`€${item.rent.toLocaleString('en-US')}`;
    return `<article class="listing-row"><div class="listing-details"><h3 class="listing-name">${safe(text(item.name,item.nameEn))}</h3><p class="listing-sub">${safe(text(item.building,item.buildingEn))} · ${safe(item.city)} · ${safe(item.source)}</p><p class="listing-meta">${safe(item.area)}　${w.move}：${safe(text(item.startZh,item.startEn))}　${w.found}：${safe(text(item.foundZh,item.foundEn))}</p></div><div class="listing-status"><span class="status-pill ${item.status}"><i aria-hidden="true"></i>${w[item.status]}</span>${item.inferred?`<span class="inferred">${w.inferred}</span>`:''}</div><div class="price">${price}<small>${item.starting&&lang==='zh'?w.starting+' ':''}${w.monthly}</small></div></article>`;
  }
  function renderScenario(){
    const scenario=document.getElementById('scenario'), alert=document.getElementById('scenario-alert');
    if(!scenario||!alert)return;
    const paused=scenario.value==='paused', review=scenario.value==='review';
    document.getElementById('monitor-dot').classList.toggle('paused',paused);
    document.getElementById('monitor-state').textContent=paused?text('监控已暂停','Monitoring paused'):text('监控运行中','Monitoring active');
    alert.hidden=!(paused||review);
    if(paused)alert.innerHTML=`<strong>${text('监控已暂停','Monitoring is paused')}</strong><p>${text('暂停期间不会发现新房源。管理员可在监控页面查看状态。','New listings will not be discovered while monitoring is paused. An administrator can check the monitor page.')}</p>`;
    if(review)alert.innerHTML=`<strong>${text('预订结果需要核实','A booking needs your review')}</strong><p>${text('请先登录 Holland2Stay 核实订单或预留，再在用户设置中确认核实。核实前该账号的自动预订已暂停。','Check your order or reservation on Holland2Stay, then confirm the review in user settings. Auto-booking is paused for this account until then.')}</p>`;
  }
  function render(){
    const home=document.getElementById('home-listings');if(home)home.innerHTML=listings.slice(0,3).map(listingHTML).join('');
    const target=document.getElementById('all-listings');if(!target)return;
    const data=new FormData(filters), q=String(data.get('q')||'').trim().toLowerCase(), city=data.get('city'), source=data.get('source'), status=data.get('status'), limit=Number(data.get('rent'));
    const results=listings.filter(item=>(!q||[item.name,item.nameEn,item.building,item.buildingEn,item.city,item.source].some(v=>v.toLowerCase().includes(q)))&&(!city||item.city===city)&&(!source||item.source===source)&&(!status||item.status===status)&&(!limit||item.rent<=limit));
    target.innerHTML=results.map(listingHTML).join('');document.getElementById('results-count').textContent=words[lang].count(results.length);document.getElementById('empty-state').hidden=results.length>0;
  }
  function update(){
    root.lang=lang==='zh'?'zh-CN':'en';root.dataset.theme=theme;
    document.querySelectorAll('[data-zh][data-en]').forEach(el=>{el.textContent=el.dataset[lang]});
    document.querySelectorAll('[data-zh-placeholder]').forEach(el=>{el.placeholder=el.dataset[`${lang}Placeholder`]});
    document.querySelectorAll('[data-zh-label]').forEach(el=>{el.setAttribute('aria-label',el.dataset[`${lang}Label`] )});
    languageButton.textContent=lang==='zh'?'EN':'中文';languageButton.setAttribute('aria-label',lang==='zh'?'Switch to English':'切换为中文');
    themeButton.textContent=theme==='light'?words[lang].dark:words[lang].light;menu.setAttribute('aria-label',menu.getAttribute('aria-expanded')==='true'?words[lang].close:words[lang].open);
    document.title=`${text(document.body.dataset.page==='home'?'首页':'房源',document.body.dataset.page==='home'?'Home':'Listings')} · FlatRadar ${text('原型','prototype')}`;render();renderScenario();
  }
  function closeMenu(){sidebar.classList.remove('open');scrim.hidden=true;sidebar.inert=mobile.matches;menu.setAttribute('aria-expanded','false');menu.setAttribute('aria-label',words[lang].open)}
  menu.addEventListener('click',()=>{const open=menu.getAttribute('aria-expanded')!=='true';sidebar.classList.toggle('open',open);scrim.hidden=!open;sidebar.inert=!open&&mobile.matches;menu.setAttribute('aria-expanded',String(open));menu.setAttribute('aria-label',open?words[lang].close:words[lang].open);if(open)sidebar.querySelector('a').focus()});
  scrim.addEventListener('click',()=>{closeMenu();menu.focus()});document.addEventListener('keydown',e=>{if(e.key==='Escape'&&menu.getAttribute('aria-expanded')==='true'){closeMenu();menu.focus()}});
  mobile.addEventListener('change',closeMenu);
  themeButton.addEventListener('click',()=>{theme=theme==='light'?'dark':'light';localStorage.setItem('flatradar-prototype-theme',theme);update()});
  languageButton.addEventListener('click',()=>{lang=lang==='zh'?'en':'zh';localStorage.setItem('flatradar-prototype-language',lang);update()});
  document.getElementById('scenario')?.addEventListener('change',renderScenario);
  if(filters){filters.addEventListener('input',render);filters.addEventListener('change',render);filters.addEventListener('submit',e=>e.preventDefault());filters.addEventListener('reset',()=>setTimeout(render,0));document.getElementById('empty-reset').addEventListener('click',()=>filters.reset())}
  closeMenu();update();
})();
