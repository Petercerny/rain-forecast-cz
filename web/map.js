// The homepage: a Leaflet map with one marker per place, day tabs, and a modal with the details.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const CZ_BOUNDS = [[48.55, 12.09], [51.06, 18.87]];  // south-west, north-east corner of Czechia
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

  let data = null;
  let map = null;
  let tileLayer = null;
  let period = "now";
  let modalChart = null;
  const markers = {};  // slug -> Leaflet marker

  // ---------------------------------------------------------------- map tiles

  function makeTileLayer() {
    const key = data.map.carto_key;
    if (key) {
      // CARTO basemaps need a (free) API key outside their own platform. Light and dark variants.
      const style = darkQuery.matches ? "dark_all" : "light_all";
      return L.tileLayer(`https://{s}.basemaps.cartocdn.com/rastertiles/${style}/{z}/{x}/{y}.png?key=${encodeURIComponent(key)}`, {
        maxZoom: 12,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
      });
    }
    // No key configured (for example when running locally): plain OpenStreetMap tiles.
    // CSS darkens them in dark mode (see .osm-tiles in style.css).
    return L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 12,
      className: "osm-tiles",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
    });
  }

  function swapTiles() {
    if (tileLayer) tileLayer.remove();
    tileLayer = makeTileLayer().addTo(map);
  }

  // ---------------------------------------------------------------- markers

  function markerHtml(loc) {
    const p = loc.periods[period];
    const lvl = RF.rainLevel(p);
    return `<div class="mk lvl-${lvl}">${RF.ICONS[lvl]}` +
      `<span class="mk-val"><b>${p ? RF.fmtPct(p.prob) : "–"}</b><span>${p ? RF.fmtTemp(p.temp) : "–"}</span></span>` +
      `<span class="mk-name">${loc.name}</span></div>`;
  }

  function markerLabel(loc) {
    const p = loc.periods[period];
    const lvl = RF.rainLevel(p);
    return `${loc.name}: ${RF.LEVEL_TEXT[lvl]}` + (p ? `, rain chance ${RF.fmtPct(p.prob)}, ${RF.fmtTemp(p.temp)}` : "");
  }

  function makeIcon(loc) {
    return L.divIcon({ className: "mk-wrap", html: markerHtml(loc), iconSize: [0, 0] });
  }

  function addMarkers() {
    for (const loc of data.locations) {
      const m = L.marker([loc.lat, loc.lon], { icon: makeIcon(loc), title: markerLabel(loc), alt: markerLabel(loc), keyboard: true });
      m.on("click", () => openModal(loc));
      m.addTo(map);
      markers[loc.slug] = m;
    }
  }

  function refreshMarkers() {
    for (const loc of data.locations) {
      markers[loc.slug].setIcon(makeIcon(loc));
      const el = markers[loc.slug].getElement();
      if (el) { el.title = markerLabel(loc); el.setAttribute("alt", markerLabel(loc)); }
    }
    renderList();
  }

  // ---------------------------------------------------------------- tabs, legend, list

  function renderTabs() {
    const tabs = $("tabs");
    tabs.replaceChildren();
    for (const p of data.periods) {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "tab");
      b.textContent = p.label;
      b.setAttribute("aria-selected", String(p.key === period));
      b.addEventListener("click", () => {
        period = p.key;
        for (const t of tabs.children) t.setAttribute("aria-selected", String(t === b));
        refreshMarkers();
        if ($("modal").open) fillModal(currentLoc);
      });
      tabs.appendChild(b);
    }
  }

  function renderLegend() {
    for (const el of document.querySelectorAll(".legend .lg")) el.innerHTML = RF.ICONS[el.dataset.icon];
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
      b.innerHTML = `${RF.ICONS[lvl]}<span class="place-name">${loc.name}</span>` +
        `<span class="place-val"><b>${p ? RF.fmtPct(p.prob) : "–"}</b> ${p ? RF.fmtTemp(p.temp) : "–"}</span>`;
      b.setAttribute("aria-label", markerLabel(loc));
      b.addEventListener("click", () => openModal(loc));
      li.appendChild(b);
      ul.appendChild(li);
    }
  }

  // ---------------------------------------------------------------- modal

  let currentLoc = null;

  function fillModal(loc) {
    const p = loc.periods[period];
    const label = data.periods.find((x) => x.key === period).label;
    $("modal-title").textContent = loc.name;
    $("modal-stats").textContent = p
      ? `${label}: rain chance ${RF.fmtPct(p.prob)}, ${period === "now" ? "" : "high "}${RF.fmtTempC(p.temp)}`
      : `${label}: no data`;
    $("modal-summary").textContent = loc.summary.text;
    $("modal-ai").hidden = !loc.summary.ai_generated;  // only shown when an LLM wrote the text
    $("modal-link").href = loc.slug + "/";
  }

  function openModal(loc) {
    currentLoc = loc;
    fillModal(loc);
    const dialog = $("modal");
    if (!dialog.open) dialog.showModal();
    if (modalChart) modalChart.destroy();
    modalChart = RF.drawChart($("modal-chart"), loc, { compact: true });
  }

  function closeModal() {
    $("modal").close();
  }

  function initModal() {
    const dialog = $("modal");
    $("modal-close").addEventListener("click", closeModal);
    // A click on the dark backdrop lands on the <dialog> itself, not on its content.
    dialog.addEventListener("click", (e) => { if (e.target === dialog) closeModal(); });
    dialog.addEventListener("close", () => { if (modalChart) { modalChart.destroy(); modalChart = null; } });
  }

  // ---------------------------------------------------------------- start

  async function init() {
    try {
      data = await RF.loadData();
    } catch (err) {
      $("status").textContent = "Could not load the forecast data. Please try again later.";
      return;
    }
    $("updated").textContent = data.generated_at_local + " (Europe/Prague)";
    RF.staleNotice(data);

    map = L.map("map", { minZoom: 6, maxZoom: 12, zoomSnap: 0.25, maxBounds: L.latLngBounds(CZ_BOUNDS).pad(0.35), maxBoundsViscosity: 0.8 });
    map.fitBounds(CZ_BOUNDS, { padding: [6, 6] });
    // Do not hijack page scrolling: the wheel zooms only after the map was clicked.
    map.scrollWheelZoom.disable();
    map.on("focus", () => map.scrollWheelZoom.enable());
    map.on("blur", () => map.scrollWheelZoom.disable());
    swapTiles();
    darkQuery.addEventListener("change", () => { swapTiles(); if (modalChart && currentLoc) openModal(currentLoc); });

    renderTabs();
    renderLegend();
    addMarkers();
    renderList();
    initModal();

    // A link like /#praha opens that place's quick view straight away.
    const linked = data.locations.find((l) => l.slug === window.location.hash.slice(1));
    if (linked) openModal(linked);

    const missing = data.locations.filter((l) => !l.sources["open-meteo"] || !l.sources["met-no"]).length;
    $("status").textContent = missing ? `${missing} place(s) are missing recent data from one of the sources.` : "";
  }

  init();
})();
