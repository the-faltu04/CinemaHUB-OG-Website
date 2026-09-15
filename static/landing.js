(() => {
  const canvas = document.getElementById('hero-canvas');
  const stage = document.getElementById('heroStage');
  if (!canvas || !stage) return;
  const ctx = canvas.getContext('2d', { alpha: true });
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  let w = 0, h = 0, t = 0, px = 0, py = 0;
  const particles = Array.from({ length: reduced ? 75 : 180 }, () => ({
    x: Math.random() * 2 - 1, y: Math.random() * 2 - 1, z: Math.random(), r: Math.random() * 1.6 + .25,
    speed: .002 + Math.random() * .008, phase: Math.random() * Math.PI * 2
  }));
  const stars = Array.from({ length: reduced ? 30 : 70 }, () => ({ x: Math.random(), y: Math.random(), a: Math.random()*.7+.1 }));

  function resize() {
    w = window.innerWidth; h = window.innerHeight;
    canvas.width = Math.floor(w * dpr); canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function project(x, y, z, scale = 1) {
    const perspective = 1 / (0.7 + z * 1.7);
    return { x: w*.69 + x*w*.24*perspective*scale, y: h*.50 + y*h*.29*perspective*scale, p: perspective };
  }
  function torusPoint(a, b, R, r) {
    const x = (R + r*Math.cos(b))*Math.cos(a);
    const y = (R + r*Math.cos(b))*Math.sin(a);
    const z = r*Math.sin(b);
    return {x,y,z};
  }
  function rotate3d(p, ax, ay) {
    let x=p.x, y=p.y, z=p.z;
    const cy=Math.cos(ay), sy=Math.sin(ay); const cz=Math.cos(ax), sz=Math.sin(ax);
    const x1=x*cy+z*sy, z1=-x*sy+z*cy;
    return {x:x1, y:y*cz-z1*sz, z:y*sz+z1*cz};
  }
  function drawTorus(R, r, rot) {
    ctx.save(); ctx.translate(w*.69 + px*10, h*.50 + py*8); ctx.rotate(rot*.25);
    for (let a=0;a<Math.PI*2;a+=Math.PI/28) {
      ctx.beginPath();
      for (let b=0;b<Math.PI*2;b+=Math.PI/22) {
        const q=rotate3d(torusPoint(a,b,R,r), .65+rot*.05, rot);
        const persp=1/(2.7-q.z);
        const x=q.x*130*persp, y=q.y*98*persp;
        if (b===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
      }
      ctx.strokeStyle='rgba(145,178,255,.13)'; ctx.lineWidth=.7; ctx.stroke();
    }
    ctx.restore();
  }
  function draw() {
    t += reduced ? .0014 : .004;
    ctx.clearRect(0,0,w,h);
    const cx=w*.69+px*12, cy=h*.50+py*10;
    const halo=ctx.createRadialGradient(cx,cy,0,cx,cy,Math.max(w,h)*.42);
    halo.addColorStop(0,'rgba(102,140,255,.16)'); halo.addColorStop(.45,'rgba(80,82,190,.06)'); halo.addColorStop(1,'rgba(0,0,0,0)');
    ctx.fillStyle=halo; ctx.fillRect(0,0,w,h);

    stars.forEach(s=>{ctx.beginPath();ctx.arc(s.x*w,s.y*h,Math.max(.3,1.2*s.a),0,Math.PI*2);ctx.fillStyle=`rgba(220,230,255,${s.a*.34})`;ctx.fill();});

    drawTorus(1,.38,t);
    drawTorus(.78,.24,-t*1.25);

    for (const p of particles) {
      p.z = (p.z + p.speed) % 1;
      const q=project(p.x, p.y, p.z, 1);
      const tw=.55+.45*Math.sin(t*3+p.phase);
      const radius=p.r*(.4+q.p*2.2);
      ctx.beginPath();ctx.arc(q.x,q.y,radius,0,Math.PI*2);ctx.fillStyle=`rgba(205,220,255,${.10+tw*.42*q.p})`;ctx.fill();
    }

    const core=ctx.createRadialGradient(cx,cy,0,cx,cy,75);
    core.addColorStop(0,'rgba(255,255,255,.7)'); core.addColorStop(.1,'rgba(150,185,255,.45)'); core.addColorStop(.45,'rgba(83,112,230,.12)'); core.addColorStop(1,'rgba(0,0,0,0)');
    ctx.fillStyle=core; ctx.beginPath(); ctx.arc(cx,cy,78,0,Math.PI*2); ctx.fill();
    if (!reduced) requestAnimationFrame(draw);
  }
  function pointer(e){
    px=(e.clientX/window.innerWidth-.5)*2; py=(e.clientY/window.innerHeight-.5)*2;
    stage.style.setProperty('--mx', `${px*8}px`); stage.style.setProperty('--my', `${py*8}px`);
  }
  window.addEventListener('resize', resize, {passive:true});
  window.addEventListener('pointermove', pointer, {passive:true});
  resize(); draw();
})();
