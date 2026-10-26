// The homepage: a full-screen MapLibre map with one marker per place, a day switcher and an hour slider,
// a side panel (bottom sheet on phones) with the details, and a weather animation layer.
// What is drawn comes from the Scene of each place (scene.js): real cloud, rain, fog, wind and daylight.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const html = document.documentElement;
  const CZ_BOUNDS = [[12.09, 48.55], [18.87, 51.06]];  // [lng, lat] south-west and north-east corner of Czechia
  const STYLE = "https://tiles.openfreemap.org/styles/positron";
  const isPhone = () => window.innerWidth < 720;
  const TILT = 35;                      // the map opens tilted by this many degrees
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  // The base map is one style that is re-painted from the "day" palette towards the "night" one (mix 0..1),
  // so dusk fades in smoothly while you move the hour slider, instead of swapping styles.
  const TINT = {
    day: { bg: "#eaf0fa", water: "#b7d3f4", green: "#d6ebe0", road: "#ffffff", casing: "#d4dff0", building: "#dde6f3",
           border: "#8f86dc", label: "#34476e", halo: "#f3f7fe", muted: "#7488ad", waterLabel: "#4a73b8" },
    night: { bg: "#0b1224", water: "#17325e", green: "#102a30", road: "#1f2a4a", casing: "#0b1224", building: "#101a33",
             border: "#6a63c8", label: "#a9b8dc", halo: "#0b1224", muted: "#667aa5", waterLabel: "#4d74b8" },
  };
  const hex = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
  const lerpColor = (a, b, k) => {
    const x = hex(a), y = hex(b);
    return "#" + x.map((v, i) => Math.round(v + (y[i] - v) * k).toString(16).padStart(2, "0")).join("");
  };

  let data = null;
  let map = null;
  let fxLayer = null;                   // the flat canvas animation: only the fallback when the WebGL weather is unavailable
  let weather = null;                   // the map-anchored WebGL weather (weather-gl.js)
  let sheetChart = null;
  let currentLoc = null;
  let lastFocus = null;
  let hours = 48;                       // how many hours the slider covers
  // what the map shows: a day tab (period) or one hour picked with the slider
  const state = { mode: "period", period: "now", hour: 0 };
  let scenes = {};                      // slug -> Scene for the current state
  let shown = null;                     // the Scene that drives the animation, theme and night look
  const markers = {};                   // slug -> { loc, wrap, btn }
  const ambient = { mix: 0, lights: 0, mixTarget: 0, lightsTarget: 0, raf: 0 };
  let tintPlan = null;

  const mountIcon = (id, name) => { $(id).innerHTML = RF.UI_ICONS[name]; };

  // ---------------------------------------------------------------- what is shown for the current state

  const dayKey = () => (state.period === "now" ? null : Number(state.period.slice(1)));  // d0 -> 0 ...

  function sceneFor(loc) {
    if (state.mode === "hour") return Scene.atIndex(loc, Math.min(state.hour, loc.hourly.length - 1));
    const k = dayKey();
    return k == null ? Scene.atIndex(loc, 0) : Scene.fromDay(loc, k);
  }

  // temperature, rain chance and rain amount for a marker
  function valuesFor(loc) {
    if (state.mode === "hour") {
      const h = loc.hourly[Math.min(state.hour, loc.hourly.length - 1)];
      if (!h) return null;
      const mm = [h.om, h.met].filter((v) => typeof v === "number");
      return { prob: h.prob, temp: h.temp, rain_mm: mm.length ? Math.max(...mm) : null };
    }
    return loc.periods[state.period];
  }

  // The hour the slider sits on for the current state.
  function sliderHour() {
    if (state.mode === "hour") return state.hour;
    const k = dayKey();
    return k == null ? 0 : Scene.middayIndex(data.locations[0], k);
  }

  function timeLabel() {
    const tab = data.periods.find((p) => p.key === state.period);
    if (state.mode === "period") return tab ? tab.label + (state.period === "now" ? "" : " · whole day") : "";
    const p = RF.localParts(data.locations[0].hourly[state.hour].t);
    return state.hour === 0 ? "Now · " + p.time : p.day + " " + p.time;
  }

  // ---------------------------------------------------------------- the base map

  const homePadding = () => (isPhone()
    ? { top: 76, bottom: 200, left: 8, right: 8 }
    : { top: 84, bottom: 150, left: 48, right: 48 });

  // Work out once which layers get which colour, so changing the mix is just a loop.
  function planTint() {
    const plan = [];
    const add = (id, prop, key) => plan.push({ id, prop, key });
    for (const layer of map.getStyle().layers) {
      const id = layer.id, src = layer["source-layer"];
      if (id.startsWith("rf-")) continue;
      if (layer.type === "background") add(id, "background-color", "bg");
      else if (layer.type === "fill") {
        if (id === "water") add(id, "fill-color", "water");
        else if (/park|wood|grass|landcover/.test(id) && !/ice|glacier/.test(id)) add(id, "fill-color", "green");
        else if (id === "building") add(id, "fill-color", "building");
        else add(id, "fill-color", "bg");  // everything else blends into the land
      } else if (layer.type === "line") {
        if (src === "waterway") add(id, "line-color", "water");
        else if (src === "boundary") add(id, "line-color", "border");
        else if (src === "transportation") {
          if (/rail|path|transit|service/.test(id)) map.setLayoutProperty(id, "visibility", "none");
          else add(id, "line-color", /casing/.test(id) ? "casing" : "road");
        } else if (src === "aeroway") add(id, "line-color", "casing");
      } else if (layer.type === "symbol") {
        if (/shield|oneway|highway.name|airport/.test(id)) { map.setLayoutProperty(id, "visibility", "none"); continue; }
        const key = /water/.test(id) ? "waterLabel" : src === "place" ? "label" : "muted";
        add(id, "text-color", key);
        add(id, "text-halo-color", "halo");
      }
    }
    return plan;
  }

  function applyTint(mix) {
    if (!tintPlan) return;
    for (const { id, prop, key } of tintPlan) {
      try { map.setPaintProperty(id, prop, lerpColor(TINT.day[key], TINT.night[key], mix)); } catch (e) { /* layer without that property */ }
    }
  }

  // Warm lights for the night: glowing towns and villages, the big roads, and built-up areas. They are
  // added under the labels and only shown when it is really night (see applyLights).
  function addLights() {
    const firstSymbol = map.getStyle().layers.find((l) => l.type === "symbol");
    const before = firstSymbol && firstSymbol.id;
    const zoomSize = (city, town, village) => ["match", ["get", "class"], "city", city, "town", town, village];
    const layers = [
      { id: "rf-lights-areas", type: "fill", "source-layer": "landuse", minzoom: 6,
        filter: ["in", ["get", "class"], ["literal", ["residential", "suburb", "neighbourhood", "commercial", "retail", "industrial"]]],
        paint: { "fill-color": "#ffb54d", "fill-opacity": 0 } },
      { id: "rf-lights-areas-glow", type: "line", "source-layer": "landuse", minzoom: 8.5,
        filter: ["in", ["get", "class"], ["literal", ["residential", "suburb", "neighbourhood", "commercial", "retail"]]],
        paint: { "line-color": "#ffc86b", "line-width": ["interpolate", ["linear"], ["zoom"], 6, 1.5, 11, 5], "line-blur": 4, "line-opacity": 0 } },
      { id: "rf-lights-roads", type: "line", "source-layer": "transportation", minzoom: 6,
        filter: ["in", ["get", "class"], ["literal", ["motorway", "trunk", "primary"]]],
        layout: { "line-cap": "round" },
        paint: { "line-color": "#ffcf7a", "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.8, 10, 2.2, 12, 4], "line-blur": 1.5, "line-opacity": 0 } },
      { id: "rf-lights-roads-minor", type: "line", "source-layer": "transportation", minzoom: 9,
        filter: ["in", ["get", "class"], ["literal", ["secondary", "tertiary"]]],
        paint: { "line-color": "#ffc46b", "line-width": ["interpolate", ["linear"], ["zoom"], 9, 0.5, 12, 1.8], "line-blur": 1, "line-opacity": 0 } },
      { id: "rf-lights-halo", type: "circle", "source-layer": "place", minzoom: 5,
        filter: ["in", ["get", "class"], ["literal", ["city", "town", "village"]]],
        paint: {
          "circle-color": "#ffb54d", "circle-blur": 1, "circle-opacity": 0,
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, zoomSize(9, 5, 0), 8, zoomSize(26, 13, 6), 11, zoomSize(52, 28, 14)],
        } },
      { id: "rf-lights-core", type: "circle", "source-layer": "place", minzoom: 5,
        filter: ["in", ["get", "class"], ["literal", ["city", "town", "village"]]],
        paint: {
          "circle-color": "#fff1c9", "circle-blur": 0.6, "circle-opacity": 0,
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, zoomSize(2.5, 1.5, 0), 8, zoomSize(5, 3, 1.5), 11, zoomSize(9, 5.5, 3)],
        } },
    ];
    for (const l of layers) {
      if (map.getLayer(l.id)) continue;
      map.addLayer(Object.assign({ source: "openmaptiles" }, l), before);
    }
  }

  const LIGHT_OPACITY = {
    "rf-lights-areas": ["fill-opacity", 0.22], "rf-lights-areas-glow": ["line-opacity", 0.22], "rf-lights-roads": ["line-opacity", 0.6],
    "rf-lights-roads-minor": ["line-opacity", 0.4], "rf-lights-halo": ["circle-opacity", 0.5], "rf-lights-core": ["circle-opacity", 0.95],
  };
  function applyLights(k) {
    for (const [id, [prop, max]] of Object.entries(LIGHT_OPACITY)) {
      if (map.getLayer(id)) map.setPaintProperty(id, prop, k * max);
    }
  }

  // Ease the palette and the lights towards their targets.
  function tickAmbient() {
    ambient.raf = 0;
    const dm = ambient.mixTarget - ambient.mix, dl = ambient.lightsTarget - ambient.lights;
    ambient.mix += Math.abs(dm) < 0.004 ? dm : dm * 0.12;
    ambient.lights += Math.abs(dl) < 0.004 ? dl : dl * 0.12;
    applyTint(ambient.mix);
    applyLights(ambient.lights);
    if (Math.abs(dm) >= 0.004 || Math.abs(dl) >= 0.004) ambient.raf = requestAnimationFrame(tickAmbient);
  }

  // Set what the map should look like for the shown scene: palette by daylight (auto) or by the chosen theme,
  // lights only when it is night, and dimmer in fog, low cloud and rain.
  function setAmbientTargets() {
    if (!shown) return;
    const dark = RF.theme.get() === "dark";
    ambient.mixTarget = RF.theme.mode() === "auto" ? clamp(1 - shown.daylight, 0, 1) : dark ? 1 : 0;
    const obscure = clamp(1 - 0.7 * shown.fog - 0.3 * (shown.low / 100) - 0.25 * shown.precip, 0.2, 1);
    ambient.lightsTarget = clamp(1 - shown.daylight, 0, 1) * clamp((ambient.mixTarget - 0.3) / 0.7, 0, 1) * obscure;
    if (!ambient.seen) { ambient.seen = true; ambient.mix = ambient.mixTarget; ambient.lights = ambient.lightsTarget; }  // first time: no fade from day
    if (!ambient.raf) ambient.raf = requestAnimationFrame(tickAmbient);
  }

  // Where "all of Czechia" is for a given tilt. fitBounds ignores the tilt, so zoom out a little when tilted
  // (the near edge of a tilted map is more zoomed in than the middle).
  function homeCamera(pitch) {
    const cam = map.cameraForBounds(CZ_BOUNDS, { padding: homePadding() });
    if (!cam) return { center: [15.45, 49.8], zoom: isPhone() ? 5.8 : 6.5, pitch, bearing: 0 };
    return { center: cam.center, zoom: cam.zoom - (pitch > 5 ? (isPhone() ? 0.25 : 0.35) : 0), pitch, bearing: 0 };
  }

  // The weather: map-anchored WebGL when possible, otherwise the flat canvas animation.
  function useCanvasFx() {
    if (fxLayer) return;
    weather = null;
    fxLayer = FX.create($("fx-layer"));
    fxLayer.setScene(shown);
  }
  function attachWeather() {
    if (weather || fxLayer) return;
    if (new URLSearchParams(window.location.search).get("fx") === "canvas") { useCanvasFx(); return; }
    const firstLabel = map.getStyle().layers.find((l) => l.type === "symbol");
    try {
      weather = WeatherGL.attach(map, { before: firstLabel && firstLabel.id, onFail: useCanvasFx });
    } catch (err) {
      console.warn("Weather layer unavailable:", err);
    }
    if (!weather) { useCanvasFx(); return; }
    RF.weather = weather;  // handy in the browser console (RF.weather.info())
    weather.setEnabled(RF.fx.get() === "on");
    pushWeather();
  }
  // Hand the scene of every place (and the shown time, light and wind) to the weather.
  function pushWeather() {
    if (weather) weather.setScene(data.locations.map((l) => ({ lon: l.lon, lat: l.lat, scene: scenes[l.slug] })), shown);
    if (fxLayer) fxLayer.setScene(shown);
  }

  function updateTiltButton() {
    const b = $("zoom-tilt");
    if (b) b.setAttribute("aria-pressed", String(map.getPitch() > 5));
  }

  function makeMap() {
    map = new maplibregl.Map({
      container: "map",
      style: STYLE,
      center: [15.45, 49.8],
      zoom: isPhone() ? 5.8 : 6.5,
      pitch: TILT,
      maxPitch: 60,
      minZoom: 5.5,
      maxZoom: 12,
      maxBounds: [[8.2, 46.2], [22.8, 53.4]],
      attributionControl: false,
      renderWorldCopies: false,
    });
    RF.map = map;  // handy in the browser console
    map.jumpTo(homeCamera(TILT));
    map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
    map.on("style.load", () => {
      tintPlan = planTint();
      addLights();
      applyTint(ambient.mix);
      applyLights(ambient.lights);
      attachWeather();
    });
    map.on("pitch", updateTiltButton);
    // The base style references a few icons we do not need; give them an empty image so MapLibre stays quiet.
    map.on("styleimagemissing", (e) => { if (!map.hasImage(e.id)) map.addImage(e.id, { width: 1, height: 1, data: new Uint8Array(4) }); });
    map.on("click", () => { if ($("sheet").classList.contains("open")) closeSheet(); });
    map.on("zoom", onZoom);
    document.addEventListener("rf:theme", () => { setAmbientTargets(); if (currentLoc) drawSheetChart(); });
    window.addEventListener("resize", () => { layoutTabs(); onZoom(); });
  }

  // City names appear once the map is zoomed in enough that they do not collide.
  function onZoom() {
    const z = map.getZoom();
    $("map").classList.toggle("show-names", z >= (isPhone() ? 7.6 : 6.9));
    $("zoom-in").disabled = z >= map.getMaxZoom() - 0.05;
    $("zoom-out").disabled = z <= map.getMinZoom() + 0.05;
  }

  function mountControls() {
    const ctl = $("map-ctl");
    const mk = (id, icon, label, fn) => {
      const b = document.createElement("button");
      b.type = "button"; b.id = id; b.className = "tool";
      b.setAttribute("aria-label", label); b.title = label;
      b.innerHTML = RF.UI_ICONS[icon];
      b.addEventListener("click", fn);
      return b;
    };
    ctl.append(
      mk("zoom-in", "plus", "Zoom in", () => map.zoomIn()),
      mk("zoom-out", "minus", "Zoom out", () => map.zoomOut()),
      mk("zoom-tilt", "tilt", "Tilt the map", () => map.easeTo({ pitch: map.getPitch() > 5 ? 0 : TILT, duration: 800 })),
      mk("zoom-home", "home", "Show all of Czechia", () => map.easeTo(Object.assign(homeCamera(TILT), { duration: 1000 }))),
    );
    updateTiltButton();
  }

  // ---------------------------------------------------------------- markers

  function markerLabel(loc) {
    const p = valuesFor(loc), s = scenes[loc.slug];
    return `${loc.name}: ${s.label.toLowerCase()}` + (p ? `, rain chance ${RF.fmtPct(p.prob)}, ${RF.fmtTemp(p.temp)}` : "");
  }

  function fillMarker(m) {
    const p = valuesFor(m.loc), s = scenes[m.loc.slug];
    m.btn.className = "mk " + s.cls + (s.night ? " night" : "") + (currentLoc === m.loc ? " selected" : "");
    m.btn.setAttribute("aria-label", markerLabel(m.loc));
    m.btn.innerHTML =
      `<span class="mk-badge">${RF.ICONS[s.icon]}</span>` +
      `<span class="mk-val"><b class="mk-temp">${p ? RF.fmtTemp(p.temp) : "–"}</b>` +
      `<span class="mk-rain">${RF.UI_ICONS.drop}${p ? RF.fmtPct(p.prob) : "–"}</span></span>` +
      `<span class="mk-name">${m.loc.name}</span>`;
  }

  function addMarkers() {
    data.locations.forEach((loc, i) => {
      const wrap = document.createElement("div");
      wrap.className = "mk-wrap";
      wrap.style.zIndex = String(Math.round((60 - loc.lat) * 10));  // southern markers in front
      const btn = document.createElement("button");
      btn.type = "button";
      btn.style.setProperty("--i", i);
      wrap.appendChild(btn);
      const m = { loc, wrap, btn };
      fillMarker(m);
      btn.addEventListener("click", (e) => { e.stopPropagation(); openSheet(loc, btn); });
      new maplibregl.Marker({ element: wrap, anchor: "center" }).setLngLat([loc.lon, loc.lat]).addTo(map);
      markers[loc.slug] = m;
    });
  }

  // ---------------------------------------------------------------- day switcher, hour slider, legend, list

  function layoutTabs() {
    const tabs = $("tabs"), on = tabs.querySelector('[aria-selected="true"]');
    tabs.classList.toggle("seg-off", !on);
    if (!on) return;
    tabs.style.setProperty("--x", on.offsetLeft + "px");
    tabs.style.setProperty("--w", on.offsetWidth + "px");
  }

  function renderTabs() {
    const tabs = $("tabs");
    tabs.replaceChildren();
    const ind = document.createElement("span");
    ind.className = "seg-ind";
    tabs.appendChild(ind);
    for (const p of data.periods) {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "tab");
      b.dataset.key = p.key;
      b.textContent = p.label;
      b.setAttribute("aria-selected", "false");
      b.addEventListener("click", () => { state.mode = "period"; state.period = p.key; refresh(); });
      tabs.appendChild(b);
    }
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(layoutTabs);
  }

  // The slider track shows the next hours as day (warm) and night (deep blue), from the real sunrise and sunset.
  function paintBands() {
    const loc = data.locations[0];
    const stops = [];
    for (let i = 0; i < hours; i++) {
      const ms = Date.parse(loc.hourly[i].t) + 1800000;
      const d = Scene.daylight(loc.sun, ms, loc.hourly[i].day);
      const c = lerpColor("#2b3875", "#ffd27f", d);
      const x0 = (100 * i) / hours, x1 = (100 * (i + 1)) / hours;
      stops.push(`${c} ${x0.toFixed(2)}%`, `${c} ${x1.toFixed(2)}%`);
    }
    $("time-track").style.setProperty("--bands", `linear-gradient(90deg, ${stops.join(", ")})`);
  }

  function initSlider() {
    const range = $("time-range");
    hours = Math.min(...data.locations.map((l) => l.hourly.length));
    range.max = String(hours - 1);
    paintBands();
    let pending = 0;
    range.addEventListener("input", () => {
      state.mode = "hour";
      state.hour = Number(range.value);
      if (!pending) pending = requestAnimationFrame(() => { pending = 0; refresh(true); });
    });
  }

  function renderLegend() {
    const items = [
      ["wx-clear", "sun", "Sunny"], ["wx-partly", "partly-day", "Partly cloudy"], ["wx-cloudy", "cloud", "Cloudy"],
      ["wx-fog", "fog", "Fog"], ["wx-rain", "rain", "Rain"], ["wx-heavy", "heavy", "Heavy rain"],
      ["wx-snow", "snow", "Snow"], ["wx-thunder", "thunder", "Thunderstorm"], ["wx-night", "moon", "Clear night"],
    ];
    $("legend-list").innerHTML = items.map(([cls, icon, text]) => `<li><span class="lg ${cls}">${RF.ICONS[icon]}</span>${text}</li>`).join("");
    $("legend").open = window.innerWidth >= 1100;
  }

  function renderList() {
    const ul = $("place-list");
    ul.replaceChildren();
    for (const loc of data.locations) {
      const p = valuesFor(loc), s = scenes[loc.slug];
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.className = "place " + s.cls;
      b.innerHTML = `<span class="mk-badge">${RF.ICONS[s.icon]}</span><span class="place-name">${loc.name}</span>` +
        `<span class="place-val"><b>${p ? RF.fmtTemp(p.temp) : "–"}</b><span>${RF.UI_ICONS.drop}${p ? RF.fmtPct(p.prob) : "–"}</span></span>`;
      b.setAttribute("aria-label", markerLabel(loc));
      b.addEventListener("click", () => { closeDrawer(); openSheet(loc, b, true); });
      li.appendChild(b);
      ul.appendChild(li);
    }
  }

  // ---------------------------------------------------------------- panels (details sheet, places list, about)

  function setOpen(el, open) {
    el.classList.toggle("open", open);
    el.inert = !open;
    document.body.classList.toggle("panel-open", $("sheet").classList.contains("open") || $("drawer").classList.contains("open"));
    document.body.classList.toggle("sheet-open", $("sheet").classList.contains("open"));
    document.body.classList.toggle("drawer-open", $("drawer").classList.contains("open"));
  }

  function dailyFor(loc) {
    const k = state.mode === "hour" ? null : dayKey();
    const i = k == null ? Math.min(state.hour, loc.hourly.length - 1) : Scene.dayStart(loc, k);
    if (i < 0) return null;
    return loc.daily.find((d) => d.date === Scene.pragueDate(Date.parse(loc.hourly[i].t))) || null;
  }

  function sunTimes(loc) {
    const i = state.mode === "hour" ? Math.min(state.hour, loc.hourly.length - 1) : (dayKey() == null ? 0 : Scene.middayIndex(loc, dayKey()));
    const date = Scene.pragueDate(Date.parse(loc.hourly[i].t));
    const e = (loc.sun || []).find((x) => Scene.pragueDate(Date.parse(x.sunrise)) === date);
    return e ? `${e.sunrise_local} / ${e.sunset_local}` : null;
  }

  function fillSheet(loc) {
    const p = valuesFor(loc), s = scenes[loc.slug];
    $("sheet-hero").className = "sheet-hero " + s.cls;
    $("sheet-period").textContent = timeLabel();
    $("sheet-title").textContent = loc.name;
    $("sheet-icon").innerHTML = RF.ICONS[s.icon];
    $("sheet-temp").textContent = p ? RF.fmtTemp(p.temp) : "–";
    $("sheet-cond").textContent = s.label;
    const chips = [];
    if (p) {
      chips.push(`${RF.UI_ICONS.drop} Rain chance ${RF.fmtPct(p.prob)}`);
      chips.push(`${RF.fmtMm(p.rain_mm)} rain`);
      chips.push(state.mode === "hour" ? RF.fmtTempC(p.temp) : state.period === "now" ? "Right now" : "Day's high");
    } else {
      chips.push("No data for this period");
    }
    $("sheet-chips").innerHTML = chips.map((c) => `<span class="chip">${c}</span>`).join("");
    const day = state.mode === "period" && dayKey() != null;
    const d = day ? dailyFor(loc) : null;
    $("sheet-cond-grid").innerHTML = RF.conditionsHtml(s, { max: day, sunshine: d && d.sunshine_h != null ? d.sunshine_h : null, sun: sunTimes(loc) });
    $("sheet-summary").textContent = loc.summary.text;
    $("sheet-ai").hidden = !loc.summary.ai_generated;  // only shown when an LLM wrote the text
    $("sheet-link").href = loc.slug + "/";
    $("sheet-link").setAttribute("aria-label", `Full details for ${loc.name}`);
    for (const m of Object.values(markers)) m.btn.classList.toggle("selected", m.loc === loc);
  }

  function drawSheetChart() {
    if (sheetChart) sheetChart.destroy();
    sheetChart = currentLoc ? RF.drawChart($("sheet-chart"), currentLoc, { compact: true }) : null;
    markChartHour();
  }

  function markChartHour() {
    if (!sheetChart) return;
    sheetChart.$hour = sliderHour();
    sheetChart.update("none");
  }

  function openSheet(loc, opener, focus) {
    lastFocus = opener || null;
    currentLoc = loc;
    closeDrawer();
    setOpen($("sheet"), true);
    refresh();
    drawSheetChart();
    map.flyTo({
      center: [loc.lon, loc.lat],
      zoom: Math.max(map.getZoom(), isPhone() ? 7.4 : 7.8),
      padding: isPhone() ? { bottom: Math.round(window.innerHeight * 0.45) } : { right: 440 },
      speed: 0.9,
      essential: true,
    });
    if (focus !== false) $("sheet-close").focus({ preventScroll: true });
  }

  function closeSheet() {
    if (!$("sheet").classList.contains("open")) return;
    setOpen($("sheet"), false);
    currentLoc = null;
    if (sheetChart) { sheetChart.destroy(); sheetChart = null; }
    map.easeTo({ padding: { top: 0, right: 0, bottom: 0, left: 0 }, duration: 600 });
    refresh();
    if (lastFocus && document.contains(lastFocus)) lastFocus.focus({ preventScroll: true });
  }

  function openDrawer() {
    closeSheet();
    setOpen($("drawer"), true);
    $("open-places").setAttribute("aria-expanded", "true");
    $("drawer-close").focus({ preventScroll: true });
  }
  function closeDrawer() {
    if (!$("drawer").classList.contains("open")) return;
    setOpen($("drawer"), false);
    $("open-places").setAttribute("aria-expanded", "false");
  }

  function initPanels() {
    mountIcon("sheet-close", "close");
    mountIcon("drawer-close", "close");
    mountIcon("about-close", "close");
    mountIcon("sheet-link-ic", "arrow");
    mountIcon("open-about", "info");
    $("open-places").innerHTML = RF.UI_ICONS.list + "<span>Places</span>";
    RF.mountSwitches($("toolbar"));

    $("sheet-close").addEventListener("click", closeSheet);
    $("drawer-close").addEventListener("click", closeDrawer);
    $("open-places").addEventListener("click", () => ($("drawer").classList.contains("open") ? closeDrawer() : openDrawer()));
    const about = $("about");
    $("open-about").addEventListener("click", () => about.showModal());
    $("about-close").addEventListener("click", () => about.close());
    about.addEventListener("click", (e) => { if (e.target === about) about.close(); });  // the backdrop is the dialog itself
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || about.open) return;
      if ($("sheet").classList.contains("open")) closeSheet(); else closeDrawer();
    });
    document.addEventListener("rf:thememode", setAmbientTargets);
    document.addEventListener("rf:fx", () => { if (weather) weather.setEnabled(RF.fx.get() === "on"); });
  }

  // ---------------------------------------------------------------- put the current state on screen

  // Recompute every scene for the current state (day tab or hour) and update everything that shows it.
  // `dragging` = the slider is being moved: skip the heavy parts that do not need to follow every step.
  function refresh(dragging) {
    scenes = {};
    for (const loc of data.locations) scenes[loc.slug] = sceneFor(loc);
    const region = state.mode === "hour" ? Scene.regionAt(data.locations, state.hour)
      : dayKey() == null ? Scene.regionAt(data.locations, 0) : Scene.regionDay(data.locations, dayKey());
    shown = currentLoc ? scenes[currentLoc.slug] : region;

    // tabs and slider
    for (const t of $("tabs").querySelectorAll("button")) t.setAttribute("aria-selected", String(state.mode === "period" && t.dataset.key === state.period));
    layoutTabs();
    if (!dragging || state.mode !== "hour") $("time-range").value = String(sliderHour());
    $("time-label").textContent = timeLabel();
    $("time-hint").textContent = state.mode === "hour" ? "Tap a day to see its overview" : "Drag to see later hours";

    for (const m of Object.values(markers)) {
      fillMarker(m);
      if (!dragging) { m.btn.style.animation = "none"; void m.btn.offsetWidth; m.btn.style.animation = ""; }  // replay the pop-in
    }
    if (!dragging || !$("drawer").classList.contains("open")) renderList();
    if (currentLoc) { fillSheet(currentLoc); markChartHour(); }

    RF.theme.followSun(shown.night);
    setAmbientTargets();
    pushWeather();
  }

  async function init() {
    initPanels();
    try {
      data = await RF.loadData();
    } catch (err) {
      $("updated-short").textContent = "Could not load the forecast data. Please try again later.";
      return;
    }
    const time = data.generated_at_local.split(", ")[1] || data.generated_at_local;
    $("updated").textContent = data.generated_at_local + " (Europe/Prague)";
    $("updated-short").textContent = "Next 48 h · updated " + time;
    RF.staleNotice(data);

    makeMap();
    mountControls();
    renderTabs();
    renderLegend();
    initSlider();
    // (the weather layer is added as soon as the map style has loaded, see attachWeather)
    // Markers do not need the base map, so they appear even while the tiles are still loading.
    scenes = {};
    for (const loc of data.locations) scenes[loc.slug] = sceneFor(loc);
    addMarkers();
    refresh();
    onZoom();
    // The attribution starts expanded; keep it as a small button until someone opens it.
    map.once("idle", () => {
      const attr = document.querySelector(".maplibregl-ctrl-attrib");
      if (attr) { attr.classList.remove("maplibregl-compact-show"); attr.removeAttribute("open"); }
    });
    // A link like /#praha opens that place's details straight away.
    const linked = data.locations.find((l) => l.slug === window.location.hash.slice(1));
    if (linked) openSheet(linked, null, false);

    const missing = data.locations.filter((l) => !l.sources["open-meteo"] || !l.sources["met-no"]).length;
    $("status").textContent = missing ? `${missing} place(s) are missing recent data from one of the sources.` : "";
  }

  init();
})();
