// Weather animation: a soft canvas overlay that draws the sky described by a Scene (see scene.js):
// the sun or moon travelling across the sky, stars, three layers of cloud, fog, drizzle / rain /
// showers / snow, and lightning. One calm style for all of it (translucent, rounded, slow).
//
//   const layer = FX.create(containerElement, { contained: true });
//   layer.setScene(Scene.atIndex(loc, 0));
//
// Every number eases towards its new target (about a second), so changing the scene cross-fades.
// It follows <html data-fx="on|off"> and <html data-theme="light|dark">, and pauses when the tab is
// hidden. With animations off nothing is drawn and no frames are scheduled.
const FX = (function () {
  "use strict";

  const html = document.documentElement;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const rand = (lo, hi) => lo + Math.random() * (hi - lo);
  const TAU = Math.PI * 2;

  const PALETTE = {
    light: {
      low: ["68, 86, 124", 0.3], mid: ["96, 116, 156", 0.24], high: ["130, 150, 192", 0.17], fog: "212, 220, 232",
      rain: "36, 84, 190", rainA: 0.7, snow: "255, 255, 255", snowEdge: "110, 140, 200", sun: "255, 176, 60", moon: "74, 92, 150",
      star: "70, 90, 150", veil: "74, 88, 116", flash: "255, 255, 255",
    },
    dark: {
      low: ["118, 134, 176", 0.22], mid: ["150, 168, 212", 0.17], high: ["182, 196, 236", 0.11], fog: "142, 158, 190",
      rain: "164, 196, 255", rainA: 0.55, snow: "255, 255, 255", snowEdge: "180, 200, 255", sun: "255, 190, 110", moon: "226, 232, 255",
      star: "232, 238, 255", veil: "8, 12, 28", flash: "225, 235, 255",
    },
    // On top of a saturated gradient (the city page hero) everything is white.
    onColor: {
      low: ["255, 255, 255", 0.26], mid: ["255, 255, 255", 0.2], high: ["255, 255, 255", 0.14], fog: "255, 255, 255",
      rain: "255, 255, 255", rainA: 0.6, snow: "255, 255, 255", snowEdge: "255, 255, 255", sun: "255, 236, 190", moon: "255, 255, 255",
      star: "255, 255, 255", veil: "40, 50, 90", flash: "255, 255, 255",
    },
  };

  // ---------------------------------------------------------------- sprites (drawn once, reused)

  // A soft blob made of overlapping radial gradients.
  function blob(w, h, puffs, rgb, alpha) {
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    const g = c.getContext("2d");
    for (const [x, y, r, k] of puffs) {
      // flatten the gradient so it never runs into the top or bottom edge of the sprite (that would leave hard lines)
      const R = r * w, sy = Math.min(1, Math.min(y * h, (1 - y) * h) / R);
      g.save();
      g.translate(x * w, y * h);
      g.scale(1, sy);
      const grad = g.createRadialGradient(0, 0, 0, 0, 0, R);
      grad.addColorStop(0, `rgba(${rgb}, ${alpha * (k || 1)})`);
      grad.addColorStop(1, `rgba(${rgb}, 0)`);
      g.fillStyle = grad;
      g.fillRect(-R, -R / sy, R * 2, (R * 2) / sy);
      g.restore();
    }
    return c;
  }
  const LOW_PUFFS = [[0.2, 0.62, 0.17], [0.38, 0.45, 0.2], [0.6, 0.42, 0.2], [0.8, 0.6, 0.16], [0.5, 0.66, 0.22], [0.3, 0.72, 0.14]];
  const MID_PUFFS = [[0.21, 0.62, 0.17], [0.38, 0.45, 0.2], [0.6, 0.42, 0.19], [0.78, 0.62, 0.15], [0.5, 0.68, 0.2]];
  const HIGH_PUFFS = [[0.18, 0.5, 0.2], [0.4, 0.5, 0.24], [0.62, 0.5, 0.24], [0.84, 0.5, 0.2]];  // a long thin veil
  // The moon: a pale disc with a bite taken out, drawn once.
  function moonSprite(rgb) {
    const c = document.createElement("canvas");
    c.width = c.height = 80;
    const g = c.getContext("2d");
    g.fillStyle = `rgb(${rgb})`;
    g.beginPath(); g.arc(40, 40, 30, 0, TAU); g.fill();
    g.globalCompositeOperation = "destination-out";
    g.beginPath(); g.arc(54, 33, 27, 0, TAU); g.fill();
    return c;
  }
  function makeSprites(p) {
    return {
      moon: moonSprite(p.moon),
      low: blob(520, 230, LOW_PUFFS, p.low[0], p.low[1]),
      mid: blob(420, 190, MID_PUFFS, p.mid[0], p.mid[1]),
      high: blob(560, 90, HIGH_PUFFS, p.high[0], p.high[1]),
      fog: blob(640, 170, [[0.25, 0.5, 0.26], [0.5, 0.5, 0.32], [0.75, 0.5, 0.26]], p.fog, 0.5),
    };
  }

  // ---------------------------------------------------------------- scene -> target numbers

  const ZERO = { sun: 0, moon: 0, stars: 0, low: 0, mid: 0, high: 0, veil: 0, fog: 0, rain: 0, drizzle: 0, snow: 0, thunder: 0, wind: 0, flowX: 0, flowY: 0, sunPos: 0.5, moonPos: 0.5, showery: 0 };

  function targets(s, dark) {
    if (!s || !s.kind || s.kind === "none") return Object.assign({}, ZERO);
    const cloud = s.cloud / 100;
    const blocked = clamp(cloud * 0.9 + s.fog * 0.6 + s.precip * 0.8, 0, 1);
    const night = 1 - s.daylight;
    const rad = (s.dir * Math.PI) / 180;
    const wind = clamp(s.wind, 0, 20);
    return {
      sun: s.daylight * (1 - blocked) * (0.35 + 0.65 * clamp(s.sunshine * 1.15, 0, 1)),
      moon: night * (1 - blocked) * (dark ? 1 : 0.4),
      stars: night * (1 - clamp(cloud * 1.1 + s.fog + s.precip, 0, 1)) * (dark ? 1 : 0.25),
      low: s.low / 100, mid: s.mid / 100, high: s.high / 100,
      veil: clamp((s.cloud - 55) / 45, 0, 1) * 0.55 + s.precip * 0.35 + s.thunder * 0.3,
      fog: clamp(s.fog + s.haze * 0.35, 0, 1),
      rain: s.rain, drizzle: s.drizzle ? 1 : 0, showery: s.showery ? 1 : 0, snow: s.snow, thunder: s.thunder ? s.rain : 0,
      wind: wind,
      // the wind blows from s.dir, so the air moves towards the opposite side
      flowX: -Math.sin(rad), flowY: -Math.cos(rad),
      sunPos: s.sunPos, moonPos: s.moonPos,
    };
  }

  // A made-up scene from a 0..3 rain level (for trying things from the console, and as a fallback).
  function sceneFromLevel(level) {
    const s = Scene.blank();
    if (level == null || level < 0) return s;
    s.cloud = level >= 3 ? 95 : level >= 2 ? 85 : level >= 1 ? 60 : 8;
    s.low = s.cloud * 0.6; s.mid = s.cloud * 0.7; s.high = s.cloud * 0.5;
    s.rain = clamp((level - 1) / 2, 0, 1);
    s.sunshine = clamp(1 - level / 2, 0, 1);
    s.wind = 2 + level;
    s.drizzle = false;
    return Scene.finish(s);
  }

  // ---------------------------------------------------------------- one layer

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
    let scene = null;
    let cur = Object.assign({}, ZERO);      // what is shown (eases towards the target)
    let raf = 0, last = 0, t = 0, destroyed = false;
    let pal = palette();
    let sprites = makeSprites(pal);

    const drops = [], rings = [], flakes = [], stars = [], streaks = [], banks = [];
    const clouds = { low: [], mid: [], high: [] };
    let flash = 0, bolt = null, nextBolt = 4;

    // ------------------------------------------------------------ pools

    function spawnDrop(initial) {
      const len = rand(12, 26);
      return { x: rand(-120, w + 120), y: initial ? rand(-h, h) : rand(-90, -10), len, k: rand(0.7, 1.2), a: rand(0.4, 1), land: rand(h * 0.3, h * 1.05), u: Math.random() };
    }
    function spawnFlake(initial) {
      return { x: rand(-40, w + 40), y: initial ? rand(-h, h) : rand(-30, -5), r: rand(1.8, 4.4), v: rand(0.6, 1.2), p: rand(0, TAU), amp: rand(10, 30), u: Math.random() };
    }
    function populate() {
      const area = w * h;
      drops.length = flakes.length = stars.length = streaks.length = banks.length = 0;
      for (let i = 0; i < Math.round(clamp(area / 3800, 80, 380)); i++) drops.push(spawnDrop(true));
      for (let i = 0; i < Math.round(clamp(area / 5500, 60, 220)); i++) flakes.push(spawnFlake(true));
      for (let i = 0; i < Math.round(clamp(area / 7000, 30, 110)); i++) stars.push({ x: rand(0, w), y: rand(0, h * 0.7), r: rand(0.5, 1.7), p: rand(0, TAU), f: rand(0.5, 1.8) });
      for (let i = 0; i < 14; i++) streaks.push({ x: rand(0, w), y: rand(0, h), len: rand(60, 200), v: rand(0.7, 1.3), a: rand(0.3, 1) });
      for (let i = 0; i < 6; i++) banks.push({ x: rand(-300, w), y: rand(h * 0.15, h * 0.95), s: rand(0.9, 1.9), v: rand(0.5, 1.2) });
      const scale = clamp(Math.min(w, 900) / 900, 0.55, 1.2);
      for (const [layer, n, y0, y1, s0, s1] of [["low", 6, 0.1, 0.75, 1.0, 1.7], ["mid", 6, 0, 0.6, 0.7, 1.3], ["high", 6, 0, 0.45, 0.8, 1.5]]) {
        clouds[layer] = [];
        for (let i = 0; i < n; i++) clouds[layer].push({ x: rand(-300, w), y: rand(y0, y1) * h, s: rand(s0, s1) * scale, v: rand(0.6, 1.4), i });
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

    const glow = (x, y, r, rgb, a) => {
      const g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, `rgba(${rgb}, ${a})`);
      g.addColorStop(0.4, `rgba(${rgb}, ${a * 0.3})`);
      g.addColorStop(1, `rgba(${rgb}, 0)`);
      ctx.fillStyle = g;
      ctx.fillRect(x - r, y - r, r * 2, r * 2);
    };

    function drawSun() {
      const a = cur.sun;
      if (a < 0.01) return;
      const dark = isDark();
      const p = cur.sunPos, base = Math.max(w, h);
      const cx = w * (0.08 + 0.84 * p), cy = h * (0.22 - 0.36 * Math.sin(Math.PI * p));
      const R = base * 0.62 * (0.94 + 0.06 * Math.sin(t * 0.6));
      glow(cx, cy, R, pal.sun, (dark ? 0.2 : 0.42) * a);
      if (!dark) {  // slow-turning rays of light
        ctx.save();
        ctx.translate(cx, cy);
        ctx.rotate(t * 0.04);
        for (let i = 0; i < 9; i++) {
          ctx.rotate(TAU / 9);
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

    function drawMoon() {
      const a = cur.moon;
      if (a < 0.01) return;
      const p = cur.moonPos;
      const cx = w * (0.1 + 0.8 * p), cy = h * (0.26 - 0.16 * Math.sin(Math.PI * p));
      const r = clamp(Math.min(w, h) * 0.035, 14, 26);
      glow(cx, cy, r * 7, pal.moon, 0.22 * a);
      ctx.globalAlpha = 0.95 * a;
      ctx.drawImage(sprites.moon, cx - r * 1.34, cy - r * 1.34, r * 2.67, r * 2.67);
      ctx.globalAlpha = 1;
    }

    function drawStars() {
      const a = cur.stars;
      if (a < 0.01) return;
      for (const s of stars) {
        const tw = 0.45 + 0.55 * Math.sin(t * s.f + s.p);
        ctx.fillStyle = `rgba(${pal.star}, ${0.85 * tw * a})`;
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, TAU);
        ctx.fill();
      }
    }

    function drawVeil() {
      const a = cur.veil;
      if (a < 0.01) return;
      ctx.fillStyle = `rgba(${pal.veil}, ${a * (isDark() ? 0.3 : 0.16)})`;
      ctx.fillRect(0, 0, w, h);
    }

    // Clouds drift with the real wind: faster when it blows harder, towards where it blows.
    function drawCloudLayer(layer, sprite, amt, speedK, dt) {
      if (amt < 0.02) return;
      const list = clouds[layer], n = list.length;
      const sp = (4 + cur.wind * 3.2) * speedK;
      for (const c of list) {
        c.x += cur.flowX * sp * c.v * dt;
        c.y += cur.flowY * sp * c.v * 0.25 * dt;
        const sw = sprite.width * c.s, sh = sprite.height * c.s;
        if (c.x > w + 40) c.x = -sw;
        else if (c.x < -sw - 40) c.x = w;
        if (c.y > h + 20) c.y = -sh * 0.5;
        else if (c.y < -sh) c.y = h * 0.8;
        // clouds appear one after the other as the cover grows
        const alpha = clamp(amt * n - c.i * 0.9, 0, 1);
        if (alpha <= 0.01) continue;
        ctx.globalAlpha = alpha;
        ctx.drawImage(sprite, c.x, c.y, sw, sh);
      }
      ctx.globalAlpha = 1;
    }

    function drawFog(dt) {
      const a = cur.fog;
      if (a < 0.02) return;
      ctx.fillStyle = `rgba(${pal.fog}, ${a * 0.3})`;
      ctx.fillRect(0, 0, w, h);
      for (const b of banks) {
        b.x += (6 + cur.wind * 2) * b.v * (cur.flowX >= 0 ? 1 : -1) * dt;
        const sw = sprites.fog.width * b.s;
        if (b.x > w + 40) b.x = -sw; else if (b.x < -sw - 40) b.x = w;
        ctx.globalAlpha = clamp(a * 1.1, 0, 1);
        ctx.drawImage(sprites.fog, b.x, b.y, sw, sprites.fog.height * b.s);
      }
      ctx.globalAlpha = 1;
    }

    function drawWindStreaks(dt) {
      const a = clamp((cur.wind - 7) / 8, 0, 1);  // only in a proper wind
      if (a < 0.02) return;
      ctx.lineCap = "round";
      ctx.lineWidth = 1;
      const dir = cur.flowX >= 0 ? 1 : -1;
      for (const s of streaks) {
        s.x += dir * (160 + cur.wind * 40) * s.v * dt;
        if (s.x > w + s.len) s.x = -s.len; else if (s.x < -s.len) s.x = w + s.len;
        ctx.strokeStyle = `rgba(${pal.rain}, ${0.18 * a * s.a})`;
        ctx.beginPath();
        ctx.moveTo(s.x, s.y);
        ctx.lineTo(s.x - dir * s.len, s.y + 3);
        ctx.stroke();
      }
    }

    function drawRain(dt) {
      let a = cur.rain;
      if (a < 0.02) return;
      // showers come in waves: the amount swells and fades
      if (cur.showery > 0.05) a *= 1 - cur.showery * (0.55 - 0.55 * Math.pow(Math.abs(Math.sin(t * 0.32)), 0.7));
      const dz = cur.drizzle;                              // 0 rain .. 1 drizzle
      const active = Math.round(drops.length * (0.2 + 0.8 * a) * (1 - 0.45 * dz));
      const slant = 0.08 + cur.flowX * clamp(cur.wind / 14, 0, 1) * 0.6 + Math.sin(t * 0.7) * clamp(cur.wind / 40, 0, 0.12);
      ctx.lineCap = "round";
      for (let i = 0; i < active; i++) {
        const d = drops[i];
        const len = d.len * (1 - 0.55 * dz) * (0.7 + 0.5 * a), speed = (480 + 420 * a) * (1 - 0.45 * dz) * d.k * (0.55 + len / 30);
        d.y += speed * dt;
        d.x += speed * slant * dt;
        if (d.y > d.land) {
          if (dz < 0.5 && rings.length < 36 && Math.random() < 0.12) rings.push({ x: d.x, y: d.y, r: 1, life: 1 });
          Object.assign(d, spawnDrop(false));
        }
        ctx.lineWidth = dz > 0.5 ? 1 : 1.5;
        ctx.strokeStyle = `rgba(${pal.rain}, ${pal.rainA * d.a * Math.min(1, a * 1.5) * (1 - 0.3 * dz)})`;
        ctx.beginPath();
        ctx.moveTo(d.x, d.y);
        ctx.lineTo(d.x - len * slant, d.y - len);
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
        ctx.ellipse(r.x, r.y, r.r, r.r * 0.38, 0, 0, TAU);
        ctx.stroke();
      }
    }

    function drawSnow(dt) {
      const a = cur.snow;
      if (a < 0.02) return;
      const active = Math.round(flakes.length * (0.2 + 0.8 * a));
      const drift = cur.flowX * cur.wind * 7;
      ctx.lineWidth = 0.8;
      for (let i = 0; i < active; i++) {
        const f = flakes[i];
        f.y += (34 + 40 * a) * f.v * dt;
        f.x += (drift + Math.sin(t * 0.9 + f.p) * f.amp) * dt;
        if (f.y > h + 10) Object.assign(f, spawnFlake(false));
        else if (f.x > w + 40) f.x = -30; else if (f.x < -40) f.x = w + 30;
        ctx.fillStyle = `rgba(${pal.snow}, ${Math.min(1, a * 1.8) * 0.92})`;
        ctx.strokeStyle = `rgba(${pal.snowEdge}, ${Math.min(1, a * 1.8) * 0.65})`;
        ctx.beginPath();
        ctx.arc(f.x, f.y, f.r, 0, TAU);
        ctx.fill();
        ctx.stroke();
      }
    }

    // Lightning: now and then a bright flash, and for a moment a jagged bolt.
    function makeBolt() {
      const pts = [];
      let x = rand(w * 0.15, w * 0.85), y = -10;
      const end = rand(h * 0.45, h * 0.8);
      while (y < end) { pts.push([x, y]); y += rand(18, 42); x += rand(-26, 26); }
      const fork = pts.length > 4 ? pts.slice(Math.floor(pts.length / 2)).map(([px, py], i) => [px + i * 14, py + i * 10]) : [];
      return { pts, fork: fork.slice(0, 4), life: 1 };
    }
    function drawLightning(dt) {
      if (cur.thunder > 0.3 && !destroyed) {
        nextBolt -= dt;
        if (nextBolt <= 0) { bolt = makeBolt(); flash = 1; nextBolt = rand(3.5, 9) / cur.thunder; }
      }
      if (flash > 0.01) {
        ctx.fillStyle = `rgba(${pal.flash}, ${flash * 0.28})`;
        ctx.fillRect(0, 0, w, h);
        flash *= Math.exp(-dt * 6);
      }
      if (bolt) {
        bolt.life -= dt * 4.5;
        if (bolt.life <= 0) bolt = null;
        else {
          ctx.save();
          ctx.strokeStyle = `rgba(${pal.flash}, ${bolt.life})`;
          ctx.shadowColor = `rgba(${pal.flash}, 0.9)`;
          ctx.shadowBlur = 16;
          ctx.lineWidth = 2.4;
          ctx.lineJoin = "round";
          for (const line of [bolt.pts, bolt.fork]) {
            if (line.length < 2) continue;
            ctx.beginPath();
            ctx.moveTo(line[0][0], line[0][1]);
            for (const [px, py] of line.slice(1)) ctx.lineTo(px, py);
            ctx.stroke();
            ctx.lineWidth = 1.4;
          }
          ctx.restore();
        }
      }
    }

    function frame(now) {
      raf = 0;
      if (destroyed) return;
      const dt = Math.min(0.05, (now - last) / 1000 || 0.016);
      last = now;
      t += dt;

      // Ease what is shown towards what is wanted, so changes cross-fade (and the sun glides).
      // (animations off: fade everything out, but let the sun / wind stay where they are so nothing jumps)
      const want = html.dataset.fx === "off"
        ? Object.assign({}, ZERO, { flowX: cur.flowX, flowY: cur.flowY, wind: cur.wind, sunPos: cur.sunPos, moonPos: cur.moonPos })
        : targets(scene, isDark());
      let moving = false;
      const k = Math.min(1, dt * 2.2);
      for (const key of Object.keys(ZERO)) {
        const diff = want[key] - cur[key];
        if (Math.abs(diff) > 0.003) { cur[key] += diff * k; moving = true; } else cur[key] = want[key];
      }

      ctx.clearRect(0, 0, w, h);
      drawSun();
      drawMoon();
      drawStars();
      drawVeil();
      drawCloudLayer("high", sprites.high, cur.high, 0.6, dt);
      drawCloudLayer("mid", sprites.mid, cur.mid, 1, dt);
      drawCloudLayer("low", sprites.low, cur.low, 1.5, dt);
      drawFog(dt);
      drawWindStreaks(dt);
      drawRain(dt);
      drawSnow(dt);
      drawLightning(dt);

      const shown = cur.sun + cur.moon + cur.stars + cur.low + cur.mid + cur.high + cur.fog + cur.rain + cur.snow + cur.veil;
      if (shown > 0.004 || moving) schedule();
    }

    function schedule() {
      if (!raf && !destroyed && !document.hidden) { last = performance.now(); raf = requestAnimationFrame(frame); }
    }

    // ------------------------------------------------------------ wiring

    const onTheme = () => { pal = palette(); sprites = makeSprites(pal); schedule(); };
    const onVisible = () => { if (!document.hidden) schedule(); };
    document.addEventListener("rf:theme", onTheme);
    document.addEventListener("rf:fx", schedule);
    document.addEventListener("visibilitychange", onVisible);

    let ro = null;
    const onWinResize = () => { resize(); schedule(); };
    if (contained && "ResizeObserver" in window) { ro = new ResizeObserver(onWinResize); ro.observe(container); }
    else window.addEventListener("resize", onWinResize);
    resize();

    const layer = {
      setScene(s) { scene = s; schedule(); },
      setCondition(level) { scene = sceneFromLevel(level); schedule(); },
      destroy() {
        destroyed = true;
        if (raf) cancelAnimationFrame(raf);
        if (ro) ro.disconnect(); else window.removeEventListener("resize", onWinResize);
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

  const layers = [];  // every live layer, handy for trying a scene from the browser console
  return { create, layers, sceneFromLevel };
})();
