'use strict';
// A small, read-only snapshot of listing fields, not live availability.
const items = [
  {name:'Jeroen Boschlaan 142L',city:'Eindhoven',source:'Holland2Stay',status:'reserved',label:'已预留',inferred:false,price:'€1185',area:'21.7 m²',available:'—',found:'11 月 2 日 10:20'},
  {name:'Frederik van Eedenplein 172',city:'Eindhoven',source:'Holland2Stay',status:'reserved',label:'已预留',inferred:false,price:'€2150',area:'80.15 m²',available:'—',found:'11 月 2 日 10:20'},
  {name:'Frederik van Eedenplein 150',city:'Eindhoven',source:'Holland2Stay',status:'available',label:'可预订',inferred:false,price:'€1849',area:'64.28 m²',available:'2026-12-07',found:'11 月 2 日 10:20'},
  {name:'Leiden — 724',city:'Leiden',source:'Student Experience',status:'occupied',label:'已入住',inferred:true,price:'€1.025',area:'21 m²',available:'2026-11-24',found:'10 月 31 日 00:43'},
  {name:'Amsterdam NDSM — 63 P4',city:'Amsterdam',source:'Student Experience',status:'occupied',label:'已入住',inferred:true,price:'€1.046',area:'24.5 m²',available:'2026-12-02',found:'10 月 31 日 00:43'},
  {name:'Amsterdam NDSM — 63 E10',city:'Amsterdam',source:'Student Experience',status:'occupied',label:'已入住',inferred:true,price:'€1.032',area:'24.9 m²',available:'2026-11-23',found:'10 月 31 日 00:43'},
  {name:'G.J. Leonard Ankersmitlaan 61 F59, Deventer',city:'Deventer',source:'Plaza',status:'occupied',label:'已入住',inferred:true,price:'€723,75',area:'18 m²',available:'2026-10-20',found:'9 月 30 日 14:31'},
  {name:'G.J. Leonard Ankersmitlaan 61 F53, Deventer',city:'Deventer',source:'Plaza',status:'occupied',label:'已入住',inferred:true,price:'€704,75',area:'18 m²',available:'2026-09-08',found:'9 月 2 日 11:12'}
];
const search = document.querySelector('#search'), source = document.querySelector('#source'), status = document.querySelector('#status');
function render(){
  const query=search.value.trim().toLowerCase();
  const result=items.filter(x=>(!query||[x.name,x.city,x.source].some(v=>v.toLowerCase().includes(query)))&&(!source.value||x.source===source.value)&&(!status.value||x.status===status.value));
  document.querySelector('#rows').innerHTML=result.map(x=>`<tr><td class="listing"><strong>${x.name}</strong><span>${x.city}</span></td><td class="platform">${x.source}</td><td><span class="status ${x.status}"><i></i>${x.label}</span>${x.inferred?'<span class="inferred">系统推测</span>':''}</td><td class="numeric price">${x.price}</td><td class="numeric number">${x.area}</td><td class="date">${x.available}</td><td class="date last">${x.found}</td></tr>`).join('');
  document.querySelector('#empty').hidden=!!result.length;
  document.querySelector('#result-count').textContent=`显示 ${result.length} 条房源`;
}
[search,source,status].forEach(x=>{x.addEventListener('input',render);x.addEventListener('change',render)});render();
