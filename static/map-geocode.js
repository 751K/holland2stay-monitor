/* The existing admin-only geocoding job remains separate from map reads. */
(function () {
  'use strict';
  const button=document.getElementById('map-geocode');
  if(!button) return;
  const status=document.getElementById('map-geocode-status');
  const zh=document.documentElement.lang==='zh';
  const label=(cn,en) => zh?cn:en;
  async function request(url,options={}) {
    const controller=new AbortController();
    const timer=setTimeout(() => controller.abort(),15000);
    try {
      const response=await fetch(url,{...options,credentials:'same-origin',cache:'no-store',signal:controller.signal});
      if(!response.ok || response.redirected) throw new Error('Geocoding request failed');
      return await response.json();
    } finally {clearTimeout(timer);}
  }
  function display(data) {
    status.textContent=label('地址定位：','Address lookup: ')+data.done+'/'+data.total+
      (data.failed?label(' · 失败 ',' · Failed ')+data.failed:'');
    if(!data.running) {
      button.disabled=false;
      if(!data.total) status.textContent=label('近期记录没有需要定位的缺失地址。','No missing addresses in recent records.');
      document.dispatchEvent(new CustomEvent('map:coordinates-updated'));
    }
    return data.running;
  }
  async function poll() {
    try {
      const data=await request(button.dataset.statusUrl);
      if(display(data)) setTimeout(poll,2000);
    } catch {
      button.disabled=false;
      status.textContent=label('无法获取进度。再次点击可查询或继续任务。','Progress is unavailable. Click again to check or continue the task.');
    }
  }
  button.addEventListener('click',async () => {
    button.disabled=true;status.textContent=label('正在检查地址…','Checking addresses…');
    try {
      const data=await request(button.dataset.startUrl,{method:'POST',headers:{'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content}});
      if(!data.ok) throw new Error('Geocoding did not start');
      if(data.running) {status.textContent=label('正在定位近期记录的地址…','Locating recent addresses…');setTimeout(poll,1000);}
      else display(data);
    } catch {
      button.disabled=false;
      status.textContent=label('任务状态未能确认，请重试查询。','The task status could not be confirmed. Try again to check.');
    }
  });
})();
