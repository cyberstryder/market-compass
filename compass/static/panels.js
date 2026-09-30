/* Collapsible panels inside each dashboard section.
   Click a panel's heading to collapse/expand it; choices persist in
   localStorage. Each section also gets Collapse-all / Expand-all tools.
   Panels are re-initialized after every render so dynamically created
   panels (exposure grids, vendor matrices) behave the same. */
(()=>{
const KEY='mc-panels-v1';
let collapsed=new Set();
try{collapsed=new Set(JSON.parse(localStorage.getItem(KEY)||'[]'));}catch(e){}
const save=()=>{try{localStorage.setItem(KEY,JSON.stringify([...collapsed].slice(0,800)));}catch(e){}};

function headEl(panel){
  return panel.querySelector(':scope > .panel-head, :scope > h2, :scope > h3');
}
function headText(panel){
  const h=panel.querySelector(':scope > .panel-head h2, :scope > .panel-head h3, :scope > h2, :scope > h3');
  if(h)return h.textContent.trim().toLowerCase().replace(/\s+/g,' ').slice(0,80);
  const sec=panel.closest('.acc-section');
  const idx=sec?[...sec.querySelectorAll('article.panel')].indexOf(panel):-1;
  return 'panel-'+idx;
}
function panelKey(panel){
  const sec=panel.closest('.acc-section');
  return (sec?sec.id:'?')+'|'+headText(panel);
}
function apply(panel){
  panel.classList.toggle('collapsed',collapsed.has(panelKey(panel)));
}
function initPanel(panel){
  if(panel.dataset.pinit)return;
  panel.dataset.pinit='1';
  const head=headEl(panel);
  if(!head)return; /* no heading row: leave it alone */
  const chev=document.createElement('span');
  chev.className='pchev';chev.setAttribute('aria-hidden','true');chev.textContent='▾';
  head.appendChild(chev);
  apply(panel);
}
function toggle(panel){
  const k=panelKey(panel);
  if(collapsed.has(k))collapsed.delete(k);else collapsed.add(k);
  save();apply(panel);
}
function addTools(){
  document.querySelectorAll('.acc-section').forEach(sec=>{
    const body=sec.querySelector(':scope > .acc-body');
    if(!body||body.querySelector(':scope > .panel-tools'))return;
    const div=document.createElement('div');
    div.className='panel-tools';
    div.innerHTML='<button type="button" class="quiet" data-act="collapse">Collapse all panels</button><span class="muted"> · </span><button type="button" class="quiet" data-act="expand">Expand all</button>';
    body.prepend(div);
  });
}
document.addEventListener('click',e=>{
  const tool=e.target.closest('.panel-tools button');
  if(tool){
    const sec=tool.closest('.acc-section');
    if(!sec)return;
    sec.querySelectorAll('article.panel').forEach(p=>{
      if(!headEl(p))return;
      const k=panelKey(p);
      if(tool.dataset.act==='collapse')collapsed.add(k);else collapsed.delete(k);
      apply(p);
    });
    save();
    return;
  }
  const head=e.target.closest('article.panel > .panel-head, article.panel > h2, article.panel > h3');
  if(!head)return;
  if(e.target.closest('a,button,input,select,textarea,label,summary'))return;
  const panel=head.closest('article.panel');
  if(panel)toggle(panel);
});
function applyAll(){
  addTools();
  document.querySelectorAll('article.panel').forEach(initPanel);
}
window.__applyPanels=applyAll;
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',applyAll);
else applyAll();
})();
