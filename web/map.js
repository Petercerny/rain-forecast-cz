// The homepage: a full-screen MapLibre map with one marker per place, a day switcher,
// a side panel (bottom sheet on phones) with the details, and a weather animation layer.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const html = document.documentElement;
  const CZ_BOUNDS = [[12.09, 48.55], [18.87, 51.06]];  // [lng, lat] south-west and north-east corner of Czechia
  const STYLES = {
    light: "https://tiles.openfreemap.org/styles/positron",
    dark: "https://tiles.openfreemap.org/styles/dark",
  };
  // Colours the base map is re-painted with once its style has loaded, so it matches the page.
  const TINT = {
    light: { bg: "#eaf0fa", water: "#b7d3f4", green: "#d6ebe0", road: "#ffffff", casing: "#d4dff0", building: "#dde6f3",
             border: "#8f86dc", label: "#34476e", halo: "#f3f7fe", muted: "#7488ad", waterLabel: "#4a73b8" },
    dark: { bg: "#0b1224", water: "#17325e", green: "#102a30", road: "#1f2a4a", casing: "#0b1224", building: "#101a33",
            border: "#6a63c8", label: "#a9b8dc", halo: "#0b1224", muted: "#667aa5", waterLabel: "#4d74b8" },
  };
  const isPhone = () => window.innerWidth < 720;

  let data = null;
  let map = null;
  let period = "now";
  let sheetChart = null;
  let currentLoc = null;
  let lastFocus = null;
  let fxLayer = null;
  const markers = {};  // slug -> { loc, wrap, btn }

  // ---------------------------------------------------------------- icons in the static markup

  function mountIcon(id, name) { $(id).innerHTML = RF.UI_ICONS[name]; }

  // ---------------------------------------------------------------- map

  const homePadding = () => (isPhone()
    ? { top: 76, bottom: 120, left: 8, right: 8 }
    : { top: 84, bottom: 96, left: 48, right: 48 });

  function tint() {
    const c = TINT[RF.theme.get()];
    const set = (id, prop, value) => { try { map.setPaintProperty(id, prop, value); } catch (e) { /* layer without that property */ } };
    for (const layer of map.getStyle().layers) {
      const id = layer.id, src = layer["source-layer"];
      if (layer.type === "background") set(id, "background-color", c.bg);
      else if (layer.type === "fill") {
        if (id === "water") set(id, "fill-color", c.water);
        else if (/park|wood|grass|landcover/.test(id) && !/ice|glacier/.test(id)) set(id, "fill-color", c.green);
        else if (id === "building") set(id, "fill-color", c.building);
        else if (src === "landuse") set(id, "fill-color", c.bg);
      } else if (layer.type === "line") {
        if (src === "waterway") set(id, "line-color", c.water);
        else if (src === "boundary") { set(id, "line-color", c.border); set(id, "line-opacity", 0.55); }
        else if (src === "transportation") {
          if (/rail|path|transit|service/.test(id)) map.setLayoutProperty(id, "visibility", "none");
          else set(id, "line-color", /casing/.test(id) ? c.casing : c.road);
        }
      } else if (layer.type === "symbol") {
        if (/shield|oneway|highway.name|airport/.test(id)) { map.setLayoutProperty(id, "visibility", "none"); continue; }
        const color = /water/.test(id) ? c.waterLabel : src === "place" ? c.label : c.muted;
        set(id, "text-color", color);
        set(id, "text-halo-color", c.halo);
        set(id, "text-halo-width", 1.4);
      }
    }
  }

  function makeMap() {
    map = new maplibregl.Map({
      container: "map",
      style: STYLES[RF.theme.get()],
      bounds: CZ_BOUNDS,
      fitBoundsOptions: { padding: homePadding() },
      minZoom: 5.5,
      maxZoom: 12,
      maxBounds: [[8.2, 46.2], [22.8, 53.4]],
      attributionControl: false,
      dragRotate: false,
      pitchWithRotate: false,
      renderWorldCopies: false,
    });
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
    map.on("style.load", tint);
    // The base styles reference a few icons we do not need; give them an empty image so MapLibre stays quiet.
    map.on("styleimagemissing", (e) => { if (!map.hasImage(e.id)) map.addImage(e.id, { width: 1, height: 1, data: new Uint8Array(4) }); });
    map.on("click", () => { if ($("sheet").classList.contains("open")) closeSheet(); });
    map.on("zoom", onZoom);
    document.addEventListener("rf:theme", () => map.setStyle(STYLES[RF.theme.get()]));
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
      mk("zoom-home", "home", "Show all of Czechia", () => map.fitBounds(CZ_BOUNDS, { padding: homePadding(), duration: 900 })),
    );
  }

  // ---------------------------------------------------------------- markers

  function markerLabel(loc) {
    const p = loc.periods[period];
    const lvl = RF.rainLevel(p);
    return `${loc.name}: ${RF.LEVEL_TEXT[lvl]}` + (p ? `, rain chance ${RF.fmtPct(p.prob)}, ${RF.fmtTemp(p.temp)}` : "");
  }

  function fillMarker(m) {
    const p = m.loc.periods[period];
    const lvl = RF.rainLevel(p);
    m.btn.className = "mk lvl-" + lvl + (currentLoc === m.loc ? " selected" : "");
    m.btn.setAttribute("aria-label", markerLabel(m.loc));
    m.btn.innerHTML =
      `<span class="mk-badge">${RF.ICONS[lvl]}</span>` +
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

  function refreshMarkers() {
    for (const m of Object.values(markers)) {
      fillMarker(m);
      m.btn.style.animation = "none"; void m.btn.offsetWidth; m.btn.style.animation = "";  // replay the pop-in
    }
    renderList();
  }

  // ---------------------------------------------------------------- day switcher, legend, list

  function layoutTabs() {
    const tabs = $("tabs"), on = tabs.querySelector('[aria-selected="true"]');
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
      b.textContent = p.label;
      b.setAttribute("aria-selected", String(p.key === period));
      b.addEventListener("click", () => {
        period = p.key;
        for (const t of tabs.querySelectorAll("button")) t.setAttribute("aria-selected", String(t === b));
        layoutTabs();
        refreshMarkers();
        if (currentLoc) fillSheet(currentLoc);
        updateFx();
      });
      tabs.appendChild(b);
    }
    layoutTabs();
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(layoutTabs);
  }

  function renderLegend() {
    for (const el of document.querySelectorAll(".legend .lg")) el.innerHTML = RF.ICONS[el.dataset.icon];
    $("legend").open = !isPhone();
  }

  function renderList() {
    const ul = $("place-list");
    ul.replaceChildren();
    for (const loc of data.locations) {
      const p = loc.periods[period];
      const lvl = RF.rainLevel(p);
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.className = "place lvl-" + lvl;
      b.innerHTML = `<span class="mk-badge">${RF.ICONS[lvl]}</span><span class="place-name">${loc.name}</span>` +
        `<span class="place-val"><b>${p ? RF.fmtTemp(p.temp) : "–"}</b><span>${RF.UI_ICONS.drop}${p ? RF.fmtPct(p.prob) : "–"}</span></span>`;
      b.setAttribute("aria-label", markerLabel(loc));
      b.addEventListener("click", () => { closeDrawer(); openSheet(loc, b, true); });
      li.appendChild(b);
      ul.appendChild(li);
    }
  }

  // ---------------------------------------------------------------- weather animation

  // The whole screen shows the weather of the selected place, or the country-wide mix when none is open.
  function updateFx() {
    if (!fxLayer) return;
    if (currentLoc) { fxLayer.setCondition(RF.rainLevel(currentLoc.periods[period])); return; }
    const levels = data.locations.map((l) => RF.rainLevel(l.periods[period])).filter((l) => l >= 0);
    fxLayer.setCondition(levels.length ? levels.reduce((a, b) => a + b, 0) / levels.length : -1);
  }

  // ---------------------------------------------------------------- panels (details sheet, places list, about)

  function setOpen(el, open) {
    el.classList.toggle("open", open);
    el.inert = !open;
    document.body.classList.toggle("panel-open", $("sheet").classList.contains("open") || $("drawer").classList.contains("open"));
    document.body.classList.toggle("sheet-open", $("sheet").classList.contains("open"));
    document.body.classList.toggle("drawer-open", $("drawer").classList.contains("open"));
  }

  function fillSheet(loc) {
    const p = loc.periods[period];
    const lvl = RF.rainLevel(p);
    const label = data.periods.find((x) => x.key === period).label;
    $("sheet-hero").className = "sheet-hero lvl-" + lvl;
    $("sheet-period").textContent = label;
    $("sheet-title").textContent = loc.name;
    $("sheet-icon").innerHTML = RF.ICONS[lvl];
    $("sheet-temp").textContent = p ? RF.fmtTemp(p.temp) : "–";
    $("sheet-cond").textContent = RF.LEVEL_SHORT[lvl];
    const chips = [];
    if (p) {
      chips.push(`${RF.UI_ICONS.drop} Rain chance ${RF.fmtPct(p.prob)}`);
      chips.push(`${RF.fmtMm(p.rain_mm)} rain`);
      chips.push(period === "now" ? "Right now" : "Day's high");
    } else {
      chips.push("No data for this period");
    }
    $("sheet-chips").innerHTML = chips.map((c) => `<span class="chip">${c}</span>`).join("");
    $("sheet-summary").textContent = loc.summary.text;
    $("sheet-ai").hidden = !loc.summary.ai_generated;  // only shown when an LLM wrote the text
    $("sheet-link").href = loc.slug + "/";
    $("sheet-link").setAttribute("aria-label", `Full details for ${loc.name}`);
    for (const m of Object.values(markers)) m.btn.classList.toggle("selected", m.loc === loc);
  }

  function drawSheetChart() {
    if (sheetChart) sheetChart.destroy();
    const old = $("sheet-chart");
    sheetChart = currentLoc ? RF.drawChart(old, currentLoc, { compact: true }) : null;
  }

  function openSheet(loc, opener, focus) {
    lastFocus = opener || null;
    currentLoc = loc;
    fillSheet(loc);
    closeDrawer();
    setOpen($("sheet"), true);
    drawSheetChart();
    updateFx();
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
    for (const m of Object.values(markers)) m.btn.classList.remove("selected");
    currentLoc = null;
    if (sheetChart) { sheetChart.destroy(); sheetChart = null; }
    map.easeTo({ padding: { top: 0, right: 0, bottom: 0, left: 0 }, duration: 600 });
    updateFx();
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
    // The chart reads its colours from CSS, so it is redrawn when the theme changes.
    document.addEventListener("rf:theme", () => { if (currentLoc) drawSheetChart(); });
  }

  // ---------------------------------------------------------------- start

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
    fxLayer = FX.create($("fx-layer"));
    // Markers do not need the base map, so they appear even while the tiles are still loading.
    addMarkers();
    renderList();
    onZoom();
    updateFx();
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
