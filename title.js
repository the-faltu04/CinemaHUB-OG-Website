(() => {
  const root=document.getElementById('titleRoot'), slug=window.CINEMA_SLUG;
  const esc=s=>String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const fallback=t=>`/poster-fallback?title=${encodeURIComponent(t)}`;
  async function run(){
    try{
      const r=await fetch(`/api/title/${encodeURIComponent(slug)}`,{headers:{Accept:'application/json'},cache:'no-store'});
      if(!r.ok)throw new Error('not found');
      const d=await r.json(), x=d.title, versions=d.versions||[];
      const genres=(x.genres||[]).slice(0,5).join(' / ');
      const backdrop=esc(x.backdrop||x.poster);
      const primary=x.telegram_url||'';
      root.innerHTML=`<section class="title-hero" style="--backdrop:url('${backdrop}')">
        <div class="title-vignette"></div><div class="title-content">
          <div class="poster-large"><img src="${esc(x.poster)}" alt="${esc(x.title)} poster" referrerpolicy="no-referrer" onerror="this.onerror=null;this.src='${fallback(x.title)}'"></div>
          <div class="title-copy"><div class="micro">${esc(x.category||'CINEMA')} · ${esc(x.metadata_provider||'LOCAL INDEX')}</div><h1>${esc(x.title)}</h1>
          <div class="title-meta"><span>${esc(x.year||'—')}</span>${x.rating?`<span>★ ${Number(x.rating).toFixed(1)}</span>`:''}${x.language?`<span>${esc(x.language)}</span>`:''}${genres?`<span>${esc(genres)}</span>`:''}</div>
          <p>${esc(x.overview||'A title from the live Cinema HUB OG catalogue. Choose a version below to continue into Telegram verification and delivery.')}</p>
          <div class="hero-actions">${primary?`<a class="gold-btn magnetic" href="${esc(primary)}" target="_blank" rel="noopener">GET ON TELEGRAM ↗</a>`:`<span class="gold-btn disabled">TELEGRAM NOT CONFIGURED</span>`}<a class="outline-btn" href="/cinema">BACK TO CINEMA</a></div>
          <div class="handoff"><span class="pulse"></span> WEBSITE → BOT → F-SUBSCRIBE → LINKPAYS → DELIVERY</div>
          </div></div></section>
        <section class="versions"><div class="section-head"><div><span class="micro">AVAILABLE FILES</span><h2>Choose your version.</h2></div><span>${versions.length} OPTION${versions.length===1?'':'S'}</span></div>
        <div class="version-grid">${versions.map((v,i)=>`<article class="version-card"><div class="version-index">${String(i+1).padStart(2,'0')}</div><div class="version-main"><strong>${esc(v.quality||v.season||'SOURCE')}</strong><span>${esc([v.language,v.season,v.size].filter(Boolean).join(' · ')||'Indexed file')}</span></div>${v.telegram_url?`<a href="${esc(v.telegram_url)}" target="_blank" rel="noopener">CONTINUE ↗</a>`:`<span class="muted">BOT OFFLINE</span>`}</article>`).join('')}</div></section>`;
    }catch(_){root.innerHTML=`<section class="title-error"><div>404</div><h1>This title isn't in the index.</h1><a href="/cinema">RETURN TO THE CINEMA</a></section>`;}
  }
  run();
})();
