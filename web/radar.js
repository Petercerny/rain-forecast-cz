// Rain radar on the map: the last hour of ČHMÚ radar frames (one every 5 minutes) as a looping overlay.
//   Radar.attach(map, { before: layerId, onToggle: (on) => {} })
// The frames come from our own server (/api/radar/frames and /api/radar/frame/<time>.png, see src/api_radar.py)
// because ČHMÚ's server does not allow browsers to use its images directly (no CORS). Every frame is its own
// MapLibre image source + raster layer, all loaded up front, so the loop only flips opacities and never flickers.
// When the radar service is not there (a plain static preview), the button just reports "Radar unavailable".
const Radar = (function () {
  "use strict";

  const API = (typeof RF !== "undefined" && RF.root ? RF.root : "") + "api/radar/frames";
  const REFRESH_MS = 5 * 60 * 1000;
  const FRAME_MS = 650;
  const LAST_FRAME_MS = 1800;
  const PREFIX = "rf-radar-";
  const timeFmt = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Prague", hour: "2-digit", minute: "2-digit", hour12: false });

  let map = null, opts = {};
  let frames = [];            // [{ t: ISO string, id: layer / source id }] oldest first
  let index = 0;              // frame on screen
  let on = false, playing = false, timer = 0, refreshTimer = 0, loading = null;
  let opacity = 0.75;
  let ui = {};

  const ICON =
    '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5"/>' +
    '<path d="M12 12 18.4 5.6"/><circle cx="12" cy="12" r="0.8" fill="currentColor"/></svg>';
  const PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5z" fill="currentColor"/></svg>';
  const PAUSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13M16 5.5v13"/></svg>';

  const CSS = `
.radar-bar { position: fixed; z-index: 5; top: 12px; left: 50%; transform: translateX(-50%); display: flex; align-items: center;
  gap: 10px; padding: 6px 16px 6px 6px; border-radius: 999px; max-width: calc(100vw - 24px); font-size: 0.85rem; }
.radar-bar[hidden] { display: none; }
.radar-bar .radar-time { font-weight: 800; font-variant-numeric: tabular-nums; white-space: nowrap; }
.radar-bar .radar-age { color: var(--muted); font-weight: 500; white-space: nowrap; }
.radar-bar input[type=range] { accent-color: var(--accent); margin: 0; min-width: 0; }
.radar-bar .radar-scrub { width: clamp(70px, 14vw, 150px); }
.radar-bar .radar-opacity { width: 60px; }
.radar-bar .radar-error { color: var(--muted); }
.radar-bar .tool { height: 36px; min-width: 36px; }
.radar-bar .tool svg { width: 18px; height: 18px; }
.tool[aria-pressed="true"].radar-switch { color: var(--accent); }
@media (max-width: 719px) {
  .radar-bar { top: 66px; gap: 8px; padding-right: 12px; }
  .radar-bar .radar-opacity, .radar-bar .radar-age { display: none; }
}
@media (prefers-reduced-motion: reduce) { .radar-bar * { transition: none !important; } }
`;

  // ---------------------------------------------------------------- map layers

  function layerOf(frame) { return PREFIX + frame.id; }

  function addFrame(frame, coordinates) {
    map.addSource(layerOf(frame), { type: "image", url: frame.url, coordinates });
    map.addLayer({
      id: layerOf(frame), type: "raster", source: layerOf(frame),
      paint: { "raster-opacity": 0, "raster-fade-duration": 0, "raster-resampling": "linear" },
    }, opts.before);
  }

  function removeFrames() {
    for (const f of frames) {
      if (map.getLayer(layerOf(f))) map.removeLayer(layerOf(f));
      if (map.getSource(layerOf(f))) map.removeSource(layerOf(f));
    }
    frames = [];
  }

  // Show frame i: only that layer is visible.
  function show(i) {
    if (!frames.length) return;
    index = ((i % frames.length) + frames.length) % frames.length;
    frames.forEach((f, k) => {
      if (map.getLayer(layerOf(f))) map.setPaintProperty(layerOf(f), "raster-opacity", k === index ? opacity : 0);
    });
    paintUi();
  }

  // ---------------------------------------------------------------- loading the frame list

  async function load() {
    if (loading) return loading;
    loading = (async () => {
      try {
        const resp = await fetch(API, { cache: "no-cache" });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const body = await resp.json();
        if (!body.frames.length) throw new Error("no frames");
        const had = frames.length ? frames[index].t : null;
        removeFrames();
        frames = body.frames.map((f) => ({ t: f.t, url: f.url, id: f.t.replace(/\D/g, "") }));
        for (const f of frames) addFrame(f, body.coordinates);
        const keep = had ? frames.findIndex((f) => f.t === had) : -1;
        show(keep >= 0 && !playing ? keep : frames.length - 1);
        ui.error = null;
      } catch (err) {
        ui.error = "Radar unavailable";
        console.warn("Radar:", err);
        paintUi();
      } finally {
        loading = null;
      }
    })();
    return loading;
  }

  // ---------------------------------------------------------------- the loop

  function tick() {
    timer = 0;
    if (!playing || !frames.length) return;
    show(index + 1);
    timer = setTimeout(tick, index === frames.length - 1 ? LAST_FRAME_MS : FRAME_MS);
  }

  function play(yes) {
    playing = yes && frames.length > 1;
    clearTimeout(timer);
    timer = 0;
    if (playing) timer = setTimeout(tick, FRAME_MS);
    paintUi();
  }

  // ---------------------------------------------------------------- the panel and the toggle button

  function paintUi() {
    if (!ui.bar) return;
    ui.bar.hidden = !on;
    ui.button.setAttribute("aria-pressed", String(on));
    if (!on) return;
    ui.time.className = ui.error ? "radar-time radar-error" : "radar-time";
    ui.time.textContent = ui.error || (frames.length ? "Radar " + timeFmt.format(new Date(frames[index].t)) : "Loading radar…");
    const ago = frames.length ? Math.max(0, Math.round((Date.now() - Date.parse(frames[index].t)) / 60000)) : null;
    ui.age.textContent = ago == null || ui.error ? "" : ago < 1 ? "just now" : ago + " min ago";
    ui.playBtn.innerHTML = playing ? PAUSE : PLAY;
    ui.playBtn.setAttribute("aria-label", playing ? "Pause the radar loop" : "Play the radar loop");
    ui.scrub.max = String(Math.max(0, frames.length - 1));
    ui.scrub.value = String(index);
    ui.scrub.disabled = !frames.length;
    ui.playBtn.disabled = frames.length < 2;
  }

  function mountUi() {
    const style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);

    const button = document.createElement("button");
    button.type = "button";
    button.className = "tool radar-switch";
    button.id = "radar-toggle";
    button.title = "Rain radar (ČHMÚ)";
    button.setAttribute("aria-label", "Rain radar");
    button.setAttribute("aria-pressed", "false");
    button.innerHTML = ICON;
    button.addEventListener("click", () => toggle(!on));
    const ctl = document.getElementById("map-ctl");
    ctl.insertBefore(button, ctl.firstChild);

    const bar = document.createElement("div");
    bar.className = "radar-bar glass";
    bar.hidden = true;
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Rain radar");
    const playBtn = Object.assign(document.createElement("button"), { type: "button", className: "tool" });
    const time = Object.assign(document.createElement("span"), { className: "radar-time", textContent: "Loading radar…" });
    time.setAttribute("aria-live", "polite");
    const age = Object.assign(document.createElement("span"), { className: "radar-age" });
    const scrub = Object.assign(document.createElement("input"), { type: "range", className: "radar-scrub", min: "0", max: "0", step: "1", value: "0" });
    scrub.setAttribute("aria-label", "Radar frame");
    const op = Object.assign(document.createElement("input"), { type: "range", className: "radar-opacity", min: "25", max: "100", step: "5", value: String(opacity * 100) });
    op.setAttribute("aria-label", "Radar opacity");
    op.title = "Opacity";
    playBtn.addEventListener("click", () => play(!playing));
    scrub.addEventListener("input", () => { play(false); show(Number(scrub.value)); });
    op.addEventListener("input", () => { opacity = Number(op.value) / 100; show(index); });
    bar.append(playBtn, time, age, scrub, op);
    document.body.appendChild(bar);
    ui = { bar, button, playBtn, time, age, scrub, error: null };
    paintUi();
  }

  async function toggle(yes) {
    on = yes;
    paintUi();
    if (opts.onToggle) opts.onToggle(on);
    clearInterval(refreshTimer);
    if (!on) {
      play(false);
      frames.forEach((f) => map.getLayer(layerOf(f)) && map.setPaintProperty(layerOf(f), "raster-opacity", 0));
      return;
    }
    await load();
    show(frames.length - 1);
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) play(true);
    refreshTimer = setInterval(() => { if (!document.hidden) load().then(paintUi); }, REFRESH_MS);
  }

  // ---------------------------------------------------------------- public

  function attach(theMap, options) {
    if (map) return;
    map = theMap;
    opts = options || {};
    mountUi();
  }

  return { attach, toggle, isOn: () => on, state: () => ({ on, playing, index, frames: frames.map((f) => f.t) }) };
})();
