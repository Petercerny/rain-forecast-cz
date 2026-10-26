// Weather animation: a soft canvas overlay that shows the current weather as falling rain,
// drifting clouds, or warm light. One calm style for all of them (translucent, rounded, slow).
//
//   const layer = FX.create(containerElement, { contained: true });
//   layer.setCondition(3);   // rain level, same scale as RF.rainLevel (0 dry .. 3 rain); fractions blend
//
// It follows <html data-fx="on|off"> and <html data-theme="light|dark"> and pauses when the
// tab is hidden. With animations off nothing is drawn and no frames are scheduled.
const FX = (function () {
  "use strict";

  const html = document.documentElement;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const rand = (lo, hi) => lo + Math.random() * (hi - lo);

  // How much of each effect a rain level calls for (0..1 each); fractional levels blend smoothly.
  function targets(level) {
    if (level == null || level < 0) return { sun: 0, cloud: 0, rain: 0 };
    return {
      sun: clamp(1 - level, 0, 1),
      cloud: clamp(level, 0, 1) * (level >= 3 ? 0.9 : 1),
      rain: clamp((level - 1) / 2, 0, 1),
    };
  }

  const PALETTE = {
    light: { rain: "36, 84, 190", rainA: 0.55, cloud: "80, 100, 145", cloudA: 0.3, sun: "255, 176, 60", mote: "255, 190, 80" },
    dark: { rain: "164, 196, 255", rainA: 0.55, cloud: "170, 190, 235", cloudA: 0.16, sun: "150, 170, 255", mote: "220, 230, 255" },
    // On top of a saturated gradient (the city page hero) everything is white.
    onColor: { rain: "255, 255, 255", rainA: 0.55, cloud: "255, 255, 255", cloudA: 0.22, sun: "255, 236, 190", mote: "255, 255, 255" },
  };

  // A fluffy cloud drawn once into its own small canvas (overlapping soft circles), then reused.
  function cloudSprite(rgb, alpha) {
    const w = 420, h = 190, c = document.createElement("canvas");
    c.width = w; c.height = h;
    const g = c.getContext("2d");
    const puffs = [[90, 120, 70], [160, 85, 85], [250, 80, 80], [320, 118, 66], [210, 128, 88], [130, 138, 60]];
    for (const [x, y, r] of puffs) {
      const grad = g.createRadialGradient(x, y, 0, x, y, r);
      grad.addColorStop(0, `rgba(${rgb}, ${alpha})`);
      grad.addColorStop(1, `rgba(${rgb}, 0)`);
      g.fillStyle = grad;
      g.fillRect(x - r, y - r, r * 2, r * 2);
    }
    return c;
  }

  function create(container, options) {
    const contained = !!(options && options.contained);
    const onColor = !!(options && options.onColor);
    const isDark = () => !onColor && html.dataset.theme === "dark";
    const palette = () => (onColor ? PALETTE.onColor : PALETTE[isDark() ? "dark" : "light"]);
    const canvas = document.createElement("canvas");
    canvas.className = "fx-canvas";
    canvas.setAttribute("aria-hidden", "true");
    container.appendChild(canvas);
    const ctx = canvas.getContext("2d");

    let w = 0, h = 0, dpr = 1;
    let level = null;                          // requested rain level
    let amt = { sun: 0, cloud: 0, rain: 0 };   // what is currently shown (eases towards the target)
    let raf = 0, last = 0, t = 0, destroyed = false;
    let pal = palette();
    let sprite = cloudSprite(pal.cloud, pal.cloudA);

    const drops = [], rings = [], clouds = [], motes = [];

    function spawnDrop(initial) {
      const len = rand(10, 24);
      return {
        x: rand(-60, w + 60), y: initial ? rand(-h, h) : rand(-80, -10),
        len, speed: rand(520, 860) * (len / 24 + 0.5), a: rand(0.35, 1),
        land: rand(h * 0.3, h * 1.05),  // a few drops "land" on the map and make a ring
      };
    }
    function populate() {
      drops.length = clouds.length = motes.length = 0;
      const dropCount = Math.round(clamp((w * h) / 5200, 60, 260));
      for (let i = 0; i < dropCount; i++) drops.push(spawnDrop(true));
      for (let i = 0; i < 7; i++) {
        clouds.push({ x: rand(-200, w), y: rand(-20, h * 0.5), s: rand(0.7, 1.5), v: rand(5, 14), a: rand(0.55, 1) });
      }
      const moteCount = Math.round(clamp((w * h) / 24000, 14, 48));
      for (let i = 0; i < moteCount; i++) {
        motes.push({ x: rand(0, w), y: rand(0, h), r: rand(1.2, 3.4), v: rand(4, 14), p: rand(0, 6.28), f: rand(0.6, 1.6) });
      }
    }

    function resize() {
      const r = contained ? container.getBoundingClientRect() : { width: window.innerWidth, height: window.innerHeight };
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = Math.max(1, Math.round(r.width));
      h = Math.max(1, Math.round(r.height));
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      populate();
    }

    // ------------------------------------------------------------ drawing

    function drawSun() {
      const a = amt.sun;
      if (a < 0.01) return;
      const dark = isDark();
      const cx = w * 0.84, cy = -h * 0.04, R = Math.max(w, h) * 0.62;
      const pulse = 0.92 + 0.08 * Math.sin(t * 0.6);
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * pulse);
      g.addColorStop(0, `rgba(${pal.sun}, ${(dark ? 0.2 : 0.42) * a})`);
      g.addColorStop(0.35, `rgba(${pal.sun}, ${(dark ? 0.08 : 0.16) * a})`);
      g.addColorStop(1, `rgba(${pal.sun}, 0)`);
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);

      if (!dark) {  // slow-turning rays of light
        ctx.save();
        ctx.translate(cx, cy);
        ctx.rotate(t * 0.04);
        for (let i = 0; i < 9; i++) {
          ctx.rotate(Math.PI * 2 / 9);
          const ray = ctx.createLinearGradient(0, 0, R * 1.2, 0);
          ray.addColorStop(0, `rgba(${pal.sun}, ${0.13 * a})`);
          ray.addColorStop(1, `rgba(${pal.sun}, 0)`);
          ctx.fillStyle = ray;
          ctx.beginPath();
          ctx.moveTo(0, 0);
          ctx.lineTo(R * 1.2, -R * 0.07);
          ctx.lineTo(R * 1.2, R * 0.07);
          ctx.closePath();
          ctx.fill();
        }
        ctx.restore();
      }
    }

    function drawMotes(dt) {
      const a = amt.sun;
      if (a < 0.01) return;
      const dark = isDark();
      for (const m of motes) {
        m.y -= m.v * dt;
        m.x += Math.sin(t * 0.5 + m.p) * 6 * dt;
        if (m.y < -10) { m.y = h + 10; m.x = rand(0, w); }
        const tw = 0.5 + 0.5 * Math.sin(t * m.f + m.p);
        ctx.fillStyle = `rgba(${pal.mote}, ${(dark ? 0.75 : 0.55) * tw * a})`;
        ctx.beginPath();
        ctx.arc(m.x, m.y, m.r * (dark ? 0.7 : 1), 0, 6.283);
        ctx.fill();
      }
    }

    function drawClouds(dt) {
      const a = amt.cloud;
      if (a < 0.01) return;
      for (const c of clouds) {
        c.x += c.v * dt;
        if (c.x > w + 40) { c.x = -sprite.width * c.s; c.y = rand(-20, h * 0.5); }
        ctx.globalAlpha = a * c.a;
        ctx.drawImage(sprite, c.x, c.y, sprite.width * c.s, sprite.height * c.s);
      }
      ctx.globalAlpha = 1;
    }

    function drawRain(dt) {
      const a = amt.rain;
      if (a < 0.01) return;
      const active = Math.round(drops.length * (0.25 + 0.75 * a));
      const wind = 0.22 + 0.06 * Math.sin(t * 0.35);
      ctx.lineCap = "round";
      ctx.lineWidth = 1.5;
      for (let i = 0; i < active; i++) {
        const d = drops[i];
        d.y += d.speed * dt;
        d.x += d.speed * wind * dt;
        if (d.y > d.land) {
          if (rings.length < 36 && Math.random() < 0.12) rings.push({ x: d.x, y: d.y, r: 1, life: 1 });
          Object.assign(d, spawnDrop(false));
        }
        ctx.strokeStyle = `rgba(${pal.rain}, ${pal.rainA * d.a * Math.min(1, a * 1.4)})`;
        ctx.beginPath();
        ctx.moveTo(d.x, d.y);
        ctx.lineTo(d.x - d.len * wind, d.y - d.len);
        ctx.stroke();
      }
      ctx.lineWidth = 1.2;
      for (let i = rings.length - 1; i >= 0; i--) {
        const r = rings[i];
        r.life -= dt * 1.6;
        r.r += dt * 26;
        if (r.life <= 0) { rings.splice(i, 1); continue; }
        ctx.strokeStyle = `rgba(${pal.rain}, ${0.4 * r.life * a})`;
        ctx.beginPath();
        ctx.ellipse(r.x, r.y, r.r, r.r * 0.38, 0, 0, 6.283);
        ctx.stroke();
      }
    }

    function frame(now) {
      raf = 0;
      if (destroyed) return;
      const dt = Math.min(0.05, (now - last) / 1000 || 0.016);
      last = now;
      t += dt;

      // Ease what is shown towards what is wanted (about a second), so changes cross-fade.
      const want = html.dataset.fx === "off" ? { sun: 0, cloud: 0, rain: 0 } : targets(level);
      let moving = false;
      for (const k of ["sun", "cloud", "rain"]) {
        const diff = want[k] - amt[k];
        if (Math.abs(diff) > 0.004) { amt[k] += diff * Math.min(1, dt * 2.2); moving = true; } else { amt[k] = want[k]; }
      }

      ctx.clearRect(0, 0, w, h);
      drawSun();
      drawClouds(dt);
      drawMotes(dt);
      drawRain(dt);

      if (amt.sun + amt.cloud + amt.rain > 0.004 || moving) schedule();
    }

    function schedule() {
      if (!raf && !destroyed && !document.hidden) { last = performance.now(); raf = requestAnimationFrame(frame); }
    }

    // ------------------------------------------------------------ wiring

    const onTheme = () => {
      pal = palette();
      sprite = cloudSprite(pal.cloud, pal.cloudA);
      schedule();
    };
    const onVisible = () => { if (!document.hidden) schedule(); };
    document.addEventListener("rf:theme", onTheme);
    document.addEventListener("rf:fx", schedule);
    document.addEventListener("visibilitychange", onVisible);

    let ro = null;
    if (contained && "ResizeObserver" in window) { ro = new ResizeObserver(() => { resize(); schedule(); }); ro.observe(container); }
    else window.addEventListener("resize", () => { resize(); schedule(); });
    resize();

    const layer = {
      setCondition(l) { level = l; schedule(); },
      destroy() {
        destroyed = true;
        if (raf) cancelAnimationFrame(raf);
        if (ro) ro.disconnect();
        document.removeEventListener("rf:theme", onTheme);
        document.removeEventListener("rf:fx", schedule);
        document.removeEventListener("visibilitychange", onVisible);
        canvas.remove();
        layers.splice(layers.indexOf(layer), 1);
      },
    };
    layers.push(layer);
    return layer;
  }

  const layers = [];  // every live layer, handy for trying a condition from the browser console
  return { create, layers };
})();
