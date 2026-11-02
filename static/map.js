/* Keep the Google Maps canvas alive while scoped listing fragments change. */
(function () {
  'use strict';
  const canvas = document.getElementById('listing-map');
  if (!canvas) return;
  const zh = document.documentElement.lang === 'zh';
  const label = (cn,en) => zh ? cn : en;
  const initial = JSON.parse(document.getElementById('map-data').textContent);
  const status = document.getElementById('map-load-status');
  const mapError = document.getElementById('map-tile-error');
  let map = null, layer = null, markerData = [], markers = new Map();
  let latestMarkers = initial.markers;
  let sequence = 0, pending = null, renderedURL = location.href;

  function activeID() {
    const query = new URL(location.href).searchParams;
    return query.get('selected') || query.get('location') || query.get('focus');
  }
  // Dark theme: a muted warm map under the dark workspace, redrawn when the theme
  // toggles (raster map, so plain styles apply without a map ID)
  const isDark=() => document.documentElement.getAttribute('data-theme')==='dark';
  const DARK_STYLES=[
    {elementType:'geometry',stylers:[{color:'#1f1e1d'}]},
    {elementType:'labels.icon',stylers:[{visibility:'off'}]},
    {elementType:'labels.text.fill',stylers:[{color:'#9a928b'}]},
    {elementType:'labels.text.stroke',stylers:[{color:'#1a1918'}]},
    {featureType:'administrative',elementType:'geometry.stroke',stylers:[{color:'#3d3834'}]},
    {featureType:'administrative.locality',elementType:'labels.text.fill',stylers:[{color:'#c2b9b1'}]},
    {featureType:'poi',elementType:'geometry',stylers:[{color:'#232120'}]},
    {featureType:'poi.park',elementType:'geometry',stylers:[{color:'#1f2420'}]},
    {featureType:'road',elementType:'geometry',stylers:[{color:'#2d2a28'}]},
    {featureType:'road',elementType:'geometry.stroke',stylers:[{color:'#1a1918'}]},
    {featureType:'road.highway',elementType:'geometry',stylers:[{color:'#3a3531'}]},
    {featureType:'road.highway',elementType:'labels.text.fill',stylers:[{color:'#b2aaa3'}]},
    {featureType:'transit',elementType:'geometry',stylers:[{color:'#292624'}]},
    {featureType:'water',elementType:'geometry',stylers:[{color:'#141a1d'}]},
    {featureType:'water',elementType:'labels.text.fill',stylers:[{color:'#5d6a70'}]},
  ];
  function icon(text,{active=false,cluster=false}={}) {
    const caption = String(text).slice(0,28);
    const escaped = caption.replace(/[&<>"']/g,c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));
    const width = Math.max(66,caption.length*7+24), height=34;
    const dark=isDark();
    const bg=active?'#98443a':cluster?(dark?'#5a4a40':'#453a33'):(dark?'#3b3430':'#ffffff');
    const fg=active||cluster?'#ffffff':(dark?'#ece9e6':'#37312d');
    const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="${width+12}" height="${height+12}"><defs><filter id="shadow" x="-30%" y="-30%" width="160%" height="170%"><feDropShadow dx="0" dy="2" stdDeviation="3" flood-opacity=".16"/></filter></defs><rect x="6" y="4" width="${width}" height="${height}" rx="6" fill="${bg}" filter="url(#shadow)"/><text x="${width/2+6}" y="25" text-anchor="middle" font-family="Arial,sans-serif" font-size="12" font-weight="600" fill="${fg}">${escaped}</text></svg>`;
    return {url:'data:image/svg+xml;charset=UTF-8,'+encodeURIComponent(svg),anchor:new google.maps.Point(width/2+6,height/2+4)};
  }
  function highlight() {
    const id = activeID();
    markerData.forEach(item => {
      const marker = markers.get(item.id), active=item.ids.includes(id);
      marker?.setIcon(icon(item.label,{active}));
      marker?.setZIndex(active?1000:1);
    });
  }
  function fit() {
    if (!map) return;
    if (!markerData.length) {map.setCenter({lat:52.15,lng:5.3});map.setZoom(7);return;}
    const bounds=new google.maps.LatLngBounds();
    markerData.forEach(item => bounds.extend({lat:item.lat,lng:item.lng}));
    map.fitBounds(bounds,50);
    google.maps.event.addListenerOnce(map,'idle',() => {if(map.getZoom()>15) map.setZoom(15);});
  }
  function showMarkers(data,{fitBounds=false}={}) {
    latestMarkers = data;
    if (!map) return;
    if (JSON.stringify(data)!==JSON.stringify(markerData)) {
      layer?.clearMarkers();
      markers.forEach(marker => {marker.setMap(null);google.maps.event.clearInstanceListeners(marker);});
      markers.clear(); markerData=data;
      const next=data.map(item => {
        const marker=new google.maps.Marker({position:{lat:item.lat,lng:item.lng},icon:icon(item.label),title:item.label,optimized:false});
        marker.flatradarCount=item.count;
        marker.addListener('click',() => {
          const url=new URL(location.href);
          ['page','selected','focus'].forEach(key => url.searchParams.delete(key));
          url.searchParams.set('positions','located');url.searchParams.set('location',item.id);
          if(item.count===1) url.searchParams.set('selected',item.id);
          navigate(url.href,{focusSide:true});
        });
        markers.set(item.id,marker);return marker;
      });
      if(layer) layer.addMarkers(next);
      else next.forEach(marker => marker.setMap(map));
    }
    highlight();
    if(fitBounds) fit();
  }
  function mapFailure(message) {
    mapError.textContent=message || label('Google Maps 暂时无法加载，房源列表仍可使用。','Google Maps could not load. Listing details remain available.');
    mapError.hidden=false;
  }
  window.gm_authFailure=() => mapFailure();
  window.initFlatRadarMap=() => {
    clearTimeout(loadTimer);
    mapError.hidden=true;
    map=new google.maps.Map(canvas,{center:{lat:52.15,lng:5.3},zoom:7,maxZoom:19,mapTypeControl:false,fullscreenControl:false,streetViewControl:false,clickableIcons:false,gestureHandling:'cooperative',styles:isDark()?DARK_STYLES:null,backgroundColor:isDark()?'#1f1e1d':undefined,
      // Google's own controls (camera, zoom) follow this; it can only be set at creation
      colorScheme:isDark()?'DARK':'LIGHT'});
    new MutationObserver(() => {map.setOptions({styles:isDark()?DARK_STYLES:null});highlight();})
      .observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
    if(window.markerClusterer?.MarkerClusterer) {
      layer=new markerClusterer.MarkerClusterer({map,markers:[],renderer:{render:cluster => {
        const count=cluster.markers.reduce((sum,marker) => sum+(marker.flatradarCount || 1),0);
        const title=label(count+' 条房源',count+' listings');
        return new google.maps.Marker({position:cluster.position,icon:icon(title,{cluster:true}),title,zIndex:100+count});
      }}});
    }
    showMarkers(latestMarkers,{fitBounds:true});
    const selected=markerData.find(item => item.ids.includes(activeID()));
    if(selected) {map.setCenter({lat:selected.lat,lng:selected.lng});map.setZoom(15);}
  };
  let loadTimer=null;
  if(initial.mapsKey) {
    const script=document.createElement('script');
    const params=new URLSearchParams({key:initial.mapsKey,loading:'async',callback:'initFlatRadarMap',language:zh?'zh-CN':'en',region:'NL'});
    script.src='https://maps.googleapis.com/maps/api/js?'+params;
    script.async=true;script.onerror=() => {clearTimeout(loadTimer);mapFailure();};
    loadTimer=setTimeout(() => mapFailure(),15000);
    document.head.append(script);
  } else mapFailure(label('未配置 Google Maps 地图密钥，房源列表仍可使用。','Google Maps is not configured. Listing details remain available.'));
  document.getElementById('map-fit').addEventListener('click',fit);

  function updateContext(selection) {
    document.querySelectorAll('#mail-filter form,.mail-search').forEach(form => {
      form.querySelectorAll('input[type="hidden"]').forEach(input => {
        if (['history','positions','location','focus'].includes(input.name)) input.remove();
      });
      Object.entries(selection).forEach(([key,value]) => {
        if (value == null || key === 'page' || key === 'selected') return;
        const input = document.createElement('input');
        input.type='hidden'; input.name=key; input.value=value; form.append(input);
      });
    });
    document.querySelectorAll('.sidebar a[href],.mail-tools a[href]').forEach(link => {
      const url=new URL(link.href,location.href);
      if (url.origin!==location.origin || url.pathname!==location.pathname) return;
      ['location','selected','focus','page','history','positions'].forEach(key => url.searchParams.delete(key));
      if (selection.history) url.searchParams.set('history',selection.history);
      url.searchParams.set('positions',selection.positions); link.href=url.href;
    });
  }
  async function navigate(href,{push=true,focusSide=false}={}) {
    const url = new URL(href,location.href);
    if (url.origin!==location.origin || url.pathname!==location.pathname) return;
    const current = ++sequence;
    pending?.abort();
    const controller = new AbortController(); pending = controller;
    const timer = setTimeout(() => controller.abort(),15000);
    document.querySelector('.map-workspace').setAttribute('aria-busy','true');
    status.hidden=true; status.replaceChildren();
    try {
      const response=await fetch(url.href,{headers:{Accept:'application/json'},credentials:'same-origin',cache:'no-store',signal:controller.signal});
      if (!response.ok || response.redirected || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Map request failed');
      const payload=await response.json();
      const fragment=new DOMParser().parseFromString(payload.html,'text/html').querySelector('.map-side');
      if (!fragment || !Array.isArray(payload.items) || !Array.isArray(payload.markers) || !payload.selection) throw new Error('Invalid map fragment');
      if (current!==sequence) return;
      document.querySelector('.map-side').replaceWith(fragment);
      const notice=document.getElementById('map-notice');
      notice.textContent=payload.notice || ''; notice.hidden=!payload.notice;
      url.searchParams.delete('focus');
      Object.entries(payload.selection).forEach(([key,value]) => {
        if (value==null) url.searchParams.delete(key); else url.searchParams.set(key,value);
      });
      if (push && url.href!==location.href) history.pushState(null,'',url);
      else if (!push) history.replaceState(null,'',url);
      renderedURL=url.href;
      updateContext(payload.selection);
      showMarkers(payload.markers);
      const unread=document.getElementById('inbox-unread-count');
      if (unread && Number.isInteger(payload.unread_count)) unread.textContent=Math.min(Number(unread.textContent),payload.unread_count);
      document.dispatchEvent(new CustomEvent('inbox:items-updated',{detail:{items:payload.items}}));
      if (focusSide) {
        const target=fragment.querySelector('[data-close-detail]') || fragment.querySelector('#map-list');
        target.tabIndex=-1; target.focus({preventScroll:true});
        if (window.matchMedia('(max-width:750px)').matches) fragment.scrollIntoView({block:'start'});
      }
    } catch {
      if (current!==sequence) return;
      if (!push) history.replaceState(null,'',renderedURL);
      status.hidden=false;
      status.append(document.createTextNode(label('地图房源加载失败，当前内容已保留。','Map listings could not load. Your current view is preserved.')));
      const retry=document.createElement('button'); retry.type='button'; retry.textContent=label('重试','Retry');
      retry.addEventListener('click',() => navigate(href,{push,focusSide})); status.append(retry);
    } finally {
      clearTimeout(timer);
      if (current===sequence) document.querySelector('.map-workspace').removeAttribute('aria-busy');
    }
  }
  document.addEventListener('click',event => {
    const link=event.target.closest('a[data-map-nav]');
    if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); navigate(link.href);
  });
  document.addEventListener('map:coordinates-updated',() => navigate(location.href,{push:false}));
  window.addEventListener('popstate',() => navigate(location.href,{push:false}));
  document.addEventListener('inbox:listing-selected',() => {
    sequence++; pending?.abort(); document.querySelector('.map-workspace').removeAttribute('aria-busy');
    renderedURL=location.href;
    document.getElementById('map-list').hidden=true;
    const detail=document.getElementById('mail-detail'); detail.hidden=false;
    detail.querySelector('[data-close-detail]')?.focus({preventScroll:true});
    const selected=markerData.find(item => item.ids.includes(activeID()));
    if (selected && map) map.panTo({lat:selected.lat,lng:selected.lng});
    highlight();
  });
  document.addEventListener('inbox:detail-closed',() => {
    renderedURL=location.href;
    document.getElementById('mail-detail').hidden=true; document.getElementById('map-list').hidden=false;
    document.querySelector('#map-list [data-listing-id][aria-current="true"]')?.focus({preventScroll:true});
    highlight();
  });
})();
