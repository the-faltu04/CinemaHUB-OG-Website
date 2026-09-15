(() => {
  const $ = (s, p = document) => p.querySelector(s);
  const $$ = (s, p = document) => [...p.querySelectorAll(s)];
  const state = { category: 'All', year: 'All', q: '' };
  const grid=$('#grid'), search=$('#search'), cats=$('#categories'), years=$('#years'), section=$('#sectionTitle'), count=$('#resultCount'), suggestions=$('#suggestions'), bar=$('#searchBar');
  const esc = s => String(s ?? '').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const fallback=t=>`/poster-fallback?title=${encodeURIComponent(t)}`;
  const card=(x,i)=>`<a class="movie-card" href="${esc(x.url)}" style="--d:${Math.min(i,18)*35}ms">
    <div class="poster-frame">
      <img src="${esc(x.poster)}" alt="${esc(x.title)} poster" loading="lazy" referrerpolicy="no-referrer" onerror="this.onerror=null;this.src='${fallback(x.title)}'">
      <div class="poster-shade"></div><div class="poster-sheen"></div>
      <div class="poster-top"><span>${esc(x.category||'CINEMA')}</span>${x.rating?`<b>★ ${Number(x.rating).toFixed(1)}</b>`:''}</div>
      <div class="poster-bottom"><strong>OPEN TITLE ↗</strong><span>${esc(x.year||'LIVE INDEX')}</span></div>
      <div class="quality-badge">${esc(x.quality||x.season||'CINEMA')}</div>
    </div>
    <div class="card-info"><div><h3>${esc(x.title)}</h3><p>${esc([x.year,x.language,x.size].filter(Boolean).join(' · ')||'Indexed title')}</p></div><span class="card-arrow">↗</span></div>
  </a>`;

  async function api(url){const r=await fetch(url,{headers:{Accept:'application/json'},cache:'no-store'});if(!r.ok)throw new Error(await r.text());return r.json();}
  function setActive(c, sel, val){$$(sel,c).forEach(b=>b.classList.toggle('active',b.dataset.value===val));}
  async function loadFilters(){
    const y=await api('/api/years').catch(()=>({years:[]}));
    years.innerHTML=['All',...(y.years||[])].map(v=>`<button class="chip year-chip ${v==='All'?'active':''}" data-value="${esc(v)}">${esc(v)}</button>`).join('');
    $$('.year-chip',years).forEach(b=>b.onclick=()=>{state.year=b.dataset.value;setActive(years,'.year-chip',state.year);render();});
    const values=['All','Movies','Web Series','Anime','K-Drama','Serials','Cartoons','WWE'];
    cats.innerHTML=values.map(v=>`<button class="chip cat-chip ${v==='All'?'active':''}" data-value="${esc(v)}">${esc(v)}</button>`).join('');
    $$('.cat-chip',cats).forEach(b=>b.onclick=()=>{state.category=b.dataset.value;setActive(cats,'.cat-chip',state.category);render();});
  }
  async function render(){
    grid.classList.add('loading');
    try{
      const path=state.q?`/api/search?q=${encodeURIComponent(state.q)}&category=${encodeURIComponent(state.category)}&year=${encodeURIComponent(state.year)}&limit=36`:`/api/catalog?category=${encodeURIComponent(state.category)}&year=${encodeURIComponent(state.year)}&limit=36`;
      const data=await api(path), results=data.results||[];
      section.textContent=state.q?`SEARCH / ${state.q}`:(state.category==='All'?'FEATURED TITLES':state.category.toUpperCase());
      count.textContent=`${results.length} TITLE${results.length===1?'':'S'}`;
      grid.innerHTML=results.length?results.map(card).join(''):`<div class="empty-state"><span>NO MATCH</span><h3>No title landed in this slice.</h3><p>Try another title, category or year.</p><button id="clearSearch" class="ghost-btn">CLEAR SEARCH</button></div>`;
      const clear=$('#clearSearch'); if(clear)clear.onclick=()=>{search.value='';state.q='';render();search.focus();};
    }catch(e){grid.innerHTML=`<div class="empty-state"><span>INDEX OFFLINE</span><h3>The live catalogue could not respond.</h3><p>Check MongoDB / Render health, then retry.</p><button class="ghost-btn" id="retry">RETRY</button></div>`;const retry=$('#retry');if(retry)retry.onclick=render;}
    finally{grid.classList.remove('loading');}
  }
  let renderTimer, suggestTimer;
  async function quickSuggest(q){
    if(q.length<2){suggestions.hidden=true;suggestions.innerHTML='';return;}
    try{
      const d=await api(`/api/search?q=${encodeURIComponent(q)}&category=${encodeURIComponent(state.category)}&year=All&limit=6`);
      const items=d.results||[];
      suggestions.innerHTML=items.length?items.map(x=>`<a class="suggestion" href="${esc(x.url)}"><img src="${esc(x.poster)}" onerror="this.src='${fallback(x.title)}'" alt=""><span><b>${esc(x.title)}</b><small>${esc([x.year,x.category].filter(Boolean).join(' · '))}</small></span><i>↗</i></a>`).join(''):`<div class="suggest-empty">No indexed match</div>`;
      suggestions.hidden=false;
    }catch(_){suggestions.hidden=true;}
  }
  search.addEventListener('input',()=>{clearTimeout(renderTimer);clearTimeout(suggestTimer);state.q=search.value.trim();suggestTimer=setTimeout(()=>quickSuggest(state.q),180);renderTimer=setTimeout(render,320);});
  search.addEventListener('focus',()=>{if(search.value.trim().length>=2)quickSuggest(search.value.trim());});
  document.addEventListener('click',e=>{if(!bar.contains(e.target))suggestions.hidden=true;});
  $('#focusSearch').onclick=()=>{search.focus();search.scrollIntoView({behavior:'smooth',block:'center'});};
  window.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();search.focus();}if(e.key==='Escape'){suggestions.hidden=true;}});
  $('#resetFilters').onclick=()=>{state.category='All';state.year='All';state.q='';search.value='';suggestions.hidden=true;setActive(cats,'.cat-chip','All');setActive(years,'.year-chip','All');render();};
  loadFilters().then(render);
})();
