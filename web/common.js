// Shared by the map page and the city pages: data loading, formatting, theme and animation
// switches, weather icons, the chart. Plain JavaScript; everything is exposed through one global, RF.
const RF = (function () {
  "use strict";

  const TZ = "Europe/Prague";
  const hourFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
  const dayFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, weekday: "short" });
  const dateFmt = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", weekday: "short", day: "numeric", month: "short" });

  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const isNum = (v) => typeof v === "number";

  // "" on the map page, "../" on a city page (set with data-root on <body>).
  const root = document.body.dataset.root || "";

  async function loadData() {
    const resp = await fetch(root + "data.json", { cache: "no-cache" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return resp.json();
  }

  // ---------------------------------------------------------------- formatting

  const fmtMm = (v) => (isNum(v) ? v.toFixed(1) + " mm" : "–");
  const fmtTemp = (v) => (isNum(v) ? Math.round(v) + "°" : "–");          // short, for markers
  const fmtTempC = (v) => (isNum(v) ? v.toFixed(1) + " °C" : "–");        // precise, for tables
  const fmtPct = (v) => (isNum(v) ? Math.round(v) + "%" : "–");

  function localParts(iso) {
    const d = new Date(iso);
    return { hour: Number(hourFmt.format(d).slice(0, 2)), time: hourFmt.format(d), day: dayFmt.format(d) };
  }

  // ---------------------------------------------------------------- theme and animation switches

  // The <head> of every page sets data-theme-mode / data-theme / data-fx before the first paint.
  //   mode   auto | light | dark   what the visitor chose (auto = follow the real daylight)
  //   theme  light | dark          what is shown right now (all CSS keys off this)
  // The page tells us whether it is night where it looks (theme.followSun), which drives "auto".
  const html = document.documentElement;
  const store = {
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode: fine */ } },
  };
  const systemDark = () => window.matchMedia("(prefers-color-scheme: dark)").matches;
  let lastNight = null;  // null until a page has told us

  function setEffective(value) {
    if (html.dataset.theme === value) return;
    html.dataset.theme = value;
    document.dispatchEvent(new CustomEvent("rf:theme", { detail: value }));
  }
  const theme = {
    get: () => html.dataset.theme || "light",
    mode: () => html.dataset.themeMode || "auto",
    setMode(mode) {
      html.dataset.themeMode = mode;
      store.set("rf-theme", mode);
      setEffective(mode === "auto" ? (lastNight == null ? (systemDark() ? "dark" : "light") : lastNight ? "dark" : "light") : mode);
      document.dispatchEvent(new CustomEvent("rf:thememode", { detail: mode }));
    },
    cycle() { theme.setMode({ auto: "light", light: "dark", dark: "auto" }[theme.mode()]); },
    // Called by the page whenever the shown scene changes; only matters in auto mode.
    followSun(night) {
      lastNight = night;
      if (theme.mode() === "auto") setEffective(night ? "dark" : "light");
    },
  };
  const fx = {
    get: () => html.dataset.fx || "on",
    set(v) {
      if (html.dataset.fx === v) return;
      html.dataset.fx = v;
      store.set("rf-fx", v);
      document.dispatchEvent(new CustomEvent("rf:fx", { detail: v }));
    },
    toggle() { fx.set(fx.get() === "on" ? "off" : "on"); },
  };

  const ico = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
  const UI_ICONS = {
    sun: ico('<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.3M12 19.2v2.3M2.5 12h2.3M19.2 12h2.3M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/>'),
    moon: ico('<path d="M20.5 14.6A8.6 8.6 0 0 1 9.4 3.5a8.6 8.6 0 1 0 11.1 11.1z"/>'),
    auto: ico('<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 0 0 17z" fill="currentColor" fill-opacity=".85"/>'),
    fx: ico('<path d="M7.5 15a4.2 4.2 0 0 1-.6-8.35A5.7 5.7 0 0 1 18 7.6 3.7 3.7 0 0 1 17.5 15z"/><path d="M9 18.2l-.8 2M13 18.2l-.8 2M17 18.2l-.8 2"/>'),
    list: ico('<path d="M8.5 6.5H20M8.5 12H20M8.5 17.5H20"/><circle cx="4.2" cy="6.5" r=".6"/><circle cx="4.2" cy="12" r=".6"/><circle cx="4.2" cy="17.5" r=".6"/>'),
    info: ico('<circle cx="12" cy="12" r="9"/><path d="M12 11v5.2"/><circle cx="12" cy="7.8" r=".5"/>'),
    close: ico('<path d="M6 6l12 12M18 6L6 18"/>'),
    plus: ico('<path d="M12 5v14M5 12h14"/>'),
    minus: ico('<path d="M5 12h14"/>'),
    tilt: ico('<path d="M3.5 9.5L12 5l8.5 4.5L12 14z"/><path d="M3.5 14l8.5 4.5 8.5-4.5"/>'),
    home: ico('<path d="M4 11l8-6.5 8 6.5"/><path d="M6 9.8V19h4.2v-5h3.6v5H18V9.8"/>'),
    back: ico('<path d="M15 5l-7 7 7 7"/>'),
    drop: ico('<path d="M12 3.5c3.2 3.9 5.5 6.6 5.5 9.6a5.5 5.5 0 0 1-11 0c0-3 2.3-5.7 5.5-9.6z"/>'),
    arrow: ico('<path d="M5 12h14M13 6l6 6-6 6"/>'),
    // conditions grid
    wind: ico('<path d="M3 8h10.5a2.5 2.5 0 1 0-2.5-2.5M3 12h15a2.8 2.8 0 1 1-2.8 2.8M3 16h7"/>'),
    nav: ico('<path d="M12 3.5l5.5 15-5.5-3.4-5.5 3.4z" fill="currentColor" fill-opacity=".25"/>'),
    humidity: ico('<path d="M12 3.5c3.2 3.9 5.5 6.6 5.5 9.6a5.5 5.5 0 0 1-11 0c0-3 2.3-5.7 5.5-9.6z" fill="currentColor" fill-opacity=".25"/>'),
    eye: ico('<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>'),
    cloudy: ico('<path d="M7 18a4 4 0 0 1-.5-7.97A5.5 5.5 0 0 1 17 9.5a4.25 4.25 0 0 1 .5 8.5z" fill="currentColor" fill-opacity=".2"/>'),
    sunshine: ico('<circle cx="12" cy="12" r="4.2" fill="currentColor" fill-opacity=".25"/><path d="M12 2.5v2.3M12 19.2v2.3M2.5 12h2.3M19.2 12h2.3M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/>'),
    sunrise: ico('<path d="M3 18.5h18M6.5 18.5a5.5 5.5 0 0 1 11 0M12 6.5v4M12 6.5l-2 2M12 6.5l2 2"/>'),
    sunset: ico('<path d="M3 18.5h18M6.5 18.5a5.5 5.5 0 0 1 11 0M12 10.5v-4M12 10.5l-2-2M12 10.5l2-2"/>'),
    gust: ico('<path d="M3 9h9a2.5 2.5 0 1 0-2.5-2.5M3 13.5h13a2.5 2.5 0 1 1-2.5 2.5M3 18h5"/>'),
  };

  // Show a short message under the toolbar (used when a switch changes state).
  let hintTimer = 0;
  function hint(text) {
    let el = document.getElementById("hint");
    if (!el) {
      el = document.createElement("p");
      el.id = "hint";
      el.className = "hint glass";
      el.setAttribute("role", "status");
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => el.classList.remove("show"), 2200);
  }

  const THEME_TEXT = {
    auto: ["Auto", "follows the real daylight, with lit-up towns at night"],
    light: ["Light", "always light"],
    dark: ["Dark", "always dark"],
  };
  const NEXT_MODE = { auto: "light", light: "dark", dark: "auto" };

  // Mount the two switches (animations on/off, theme auto/light/dark) into a container. The buttons
  // keep themselves in sync with the page state.
  function mountSwitches(container) {
    const fxBtn = document.createElement("button");
    fxBtn.type = "button";
    fxBtn.className = "tool fx-switch";
    fxBtn.innerHTML = UI_ICONS.fx + '<span class="tool-slash"></span>';
    const themeBtn = document.createElement("button");
    themeBtn.type = "button";
    themeBtn.className = "tool theme-switch";
    themeBtn.innerHTML = '<span class="swap swap-auto">' + UI_ICONS.auto + '</span><span class="swap swap-sun">' + UI_ICONS.sun +
      '</span><span class="swap swap-moon">' + UI_ICONS.moon + "</span>";

    function sync() {
      const fxOn = fx.get() === "on", mode = theme.mode();
      fxBtn.setAttribute("aria-pressed", String(fxOn));
      fxBtn.setAttribute("aria-label", "Weather animations");
      fxBtn.title = fxOn ? "Turn weather animations off" : "Turn weather animations on";
      themeBtn.setAttribute("aria-label", "Theme: " + THEME_TEXT[mode][0] + ". Click to change");
      themeBtn.title = "Theme: " + THEME_TEXT[mode][0] + " (click for " + THEME_TEXT[NEXT_MODE[mode]][0] + ")";
    }
    fxBtn.addEventListener("click", () => { fx.toggle(); hint(fx.get() === "on" ? "Weather animations on" : "Weather animations off"); });
    themeBtn.addEventListener("click", () => { theme.cycle(); hint(THEME_TEXT[theme.mode()][0] + ": " + THEME_TEXT[theme.mode()][1]); });
    document.addEventListener("rf:fx", sync);
    document.addEventListener("rf:thememode", sync);
    sync();
    container.append(fxBtn, themeBtn);
  }

  // ---------------------------------------------------------------- weather icons

  // Level from the rain chance (Open-Meteo): 0 sun (<20 %), 1 cloud (20-49), 2 light rain (50-79), 3 rain (80+).
  // If a source only gave rain amounts, fall back to those. -1 = no data at all. (Used when the newer
  // sky fields are missing from data.json; see scene.js.)
  function rainLevel(period) {
    if (!period) return -1;
    if (isNum(period.prob)) return period.prob < 20 ? 0 : period.prob < 50 ? 1 : period.prob < 80 ? 2 : 3;
    if (isNum(period.rain_mm)) return period.rain_mm < 0.1 ? 0 : period.rain_mm < 1 ? 2 : 3;
    return -1;
  }

  // Soft two-tone icons: a translucent fill plus a rounded outline, drawn with currentColor.
  // The classes (ic-rays, ic-cloud, ic-drop, ...) are what the CSS animates when animations are on.
  const SVG = (cls, inner) =>
    `<svg class="ic ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ` +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + inner + "</svg>";
  const CLOUD = (y) => `<path class="ic-cloud" fill="currentColor" fill-opacity=".18" d="M7 ${y + 3}a4 4 0 0 1-.5-7.97A5.5 5.5 0 0 1 17 ${y - 5.5}a4.25 4.25 0 0 1 .5 8.5z"/>`;
  const DROP = (x, y, n) => `<path class="ic-drop d${n}" d="M${x} ${y}l-1 2.4"/>`;
  const RAYS = '<g class="ic-rays"><path d="M12 2.5v2M12 19.5v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M2.5 12h2M19.5 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4"/></g>';
  // The keys are the ones Scene gives in scene.icon
  const ICONS = {
    none: SVG("ic-none", '<circle cx="12" cy="12" r="8"/><path d="M9 12h6"/>'),
    sun: SVG("ic-sun", RAYS + '<circle class="ic-core" cx="12" cy="12" r="4.4" fill="currentColor" fill-opacity=".28"/>'),
    moon: SVG("ic-moon", '<path class="ic-core" fill="currentColor" fill-opacity=".28" d="M19.5 14.3A7.6 7.6 0 0 1 9.7 4.5a7.6 7.6 0 1 0 9.8 9.8z"/>' +
      '<path class="ic-star s1" d="M18.5 4v2M17.5 5h2"/><path class="ic-star s2" d="M4.5 16.5v1.6M3.7 17.3h1.6"/>'),
    "partly-day": SVG("ic-partly", '<g class="ic-rays"><path d="M8.5 2v1.8M2.6 4.6l1.3 1.3M14.4 4.6l-1.3 1.3M1.5 10h1.8"/></g>' +
      '<circle class="ic-core" cx="8.5" cy="9.3" r="3.2" fill="currentColor" fill-opacity=".28"/>' + CLOUD(16)),
    "partly-night": SVG("ic-partly", '<path class="ic-core" fill="currentColor" fill-opacity=".28" d="M14 8.2A5.2 5.2 0 0 1 7.8 2.6 5.2 5.2 0 1 0 14 8.2z"/>' + CLOUD(16)),
    cloud: SVG("ic-clouds", CLOUD(14)),
    fog: SVG("ic-fog", CLOUD(9) + '<path class="ic-fogline f1" d="M5 15h14"/><path class="ic-fogline f2" d="M7 18.5h12"/><path class="ic-fogline f3" d="M4 22h9"/>'),
    rain: SVG("ic-rainy", CLOUD(11) + DROP(9.5, 17.2, 1) + DROP(14.5, 17.2, 2)),
    heavy: SVG("ic-rainy ic-heavy", CLOUD(10) + DROP(8, 16.2, 1) + DROP(12, 16.2, 2) + DROP(16, 16.2, 3) + DROP(10, 20, 4) + DROP(14, 20, 5)),
    snow: SVG("ic-snowy", CLOUD(10) + '<g class="ic-flake d1"><path d="M8 17v2.4M6.9 18.2h2.2"/></g><g class="ic-flake d2"><path d="M12 18.4v2.4M10.9 19.6h2.2"/></g><g class="ic-flake d3"><path d="M16 17v2.4M14.9 18.2h2.2"/></g>'),
    thunder: SVG("ic-storm", CLOUD(9) + '<path class="ic-bolt" fill="currentColor" fill-opacity=".35" d="M12.8 14l-3 4.4h3l-1.2 4.1 4.2-5.6h-3z"/>'),
  };

  const compass = (deg) => ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round((((deg % 360) + 360) % 360) / 45) % 8];

  // ---------------------------------------------------------------- conditions grid

  const km = (m) => (m == null ? "–" : m >= 10000 ? "10+ km" : (m / 1000).toFixed(m < 1000 ? 2 : 1) + " km");

  // The small tiles with wind, gusts, cloud cover, humidity, visibility, sunshine and sunrise / sunset for a Scene.
  //   o.max       true for a whole day: wind and visibility are then the worst of the day
  //   o.sunshine  hours of sunshine (a whole day); otherwise the share of the hour is shown
  //   o.sun       "06:42 / 16:49" or null
  function conditionsHtml(s, o) {
    o = o || {};
    const cells = [];
    const cell = (icon, label, value, extra) => cells.push(`<div class="cond"><dt>${UI_ICONS[icon]}${label}</dt><dd>${value}</dd>${extra || ""}</div>`);
    const arrow = `<span class="arrow" style="--rot:${Math.round(s.dir + 180)}deg">${UI_ICONS.nav}</span>`;
    cell("wind", o.max ? "Wind (max)" : "Wind", `${arrow}${s.wind.toFixed(1)} <small>m/s ${compass(s.dir)}</small>`);
    cell("gust", o.max ? "Gusts (max)" : "Gusts", `${s.gust.toFixed(1)} <small>m/s</small>`);
    cell("cloudy", "Cloud cover", `${Math.round(s.cloud)}<small>%</small>`,
      `<div class="layers" title="Low, middle and high clouds"><i style="--v:${Math.round(s.low)}%"></i><i style="--v:${Math.round(s.mid)}%"></i><i style="--v:${Math.round(s.high)}%"></i></div>`);
    cell("humidity", "Humidity", s.hum == null ? "–" : `${Math.round(s.hum)}<small>%</small>`);
    cell("eye", o.max ? "Visibility (min)" : "Visibility", km(s.vis));
    if (o.sunshine != null) cell("sunshine", "Sunshine", `${o.sunshine} <small>h</small>`);
    else cell("sunshine", "Sunshine", s.night ? "–" : `${Math.round(s.sunshine * 100)}<small>%</small>`);
    if (o.sun) cell("sunrise", "Sunrise / sunset", o.sun);
    return cells.join("");
  }

  // ---------------------------------------------------------------- chart

  // Rain bars (mm, left axis) and temperature lines (deg C, right axis) for both sources.
  // The Open-Meteo rain chance is in the legend but hidden until you click it.
  function drawChart(canvas, loc, opts) {
    const compact = !!(opts && opts.compact);
    if (typeof Chart === "undefined") {
      canvas.replaceWith(Object.assign(document.createElement("p"), {
        textContent: "The chart library could not be loaded. The numbers on this page are still correct.",
      }));
      return null;
    }
    const hours = loc.hourly;
    const labels = hours.map((h) => localParts(h.t));
    const text = cssVar("--muted"), grid = cssVar("--line");
    const om = cssVar("--om"), met = cssVar("--met");
    const tipBg = cssVar("--tip-bg"), tipFg = cssVar("--tip-fg"), cloudLine = cssVar("--accent");

    Chart.defaults.font.family = cssVar("--font") || "system-ui, sans-serif";

    // A vertical fade under the temperature line / on the bars (needs the chart area, so it is a function).
    const fade = (color, from, to) => (ctx) => {
      const a = ctx.chart.chartArea;
      if (!a) return color;
      const g = ctx.chart.ctx.createLinearGradient(0, a.top, 0, a.bottom);
      g.addColorStop(0, color + from);
      g.addColorStop(1, color + to);
      return g;
    };

    // A thin vertical line at the hour that is shown on the map (set chart.$hour, then chart.draw()).
    const hourLine = {
      id: "hourLine",
      afterDatasetsDraw(c) {
        if (c.$hour == null || !c.scales.x) return;
        const x = c.scales.x.getPixelForValue(c.$hour), a = c.chartArea;
        const g = c.ctx;
        g.save();
        g.strokeStyle = cssVar("--accent");
        g.lineWidth = 2;
        g.setLineDash([4, 4]);
        g.beginPath();
        g.moveTo(x, a.top);
        g.lineTo(x, a.bottom);
        g.stroke();
        g.restore();
      },
    };

    const chart = new Chart(canvas, {
      type: "bar",
      plugins: [hourLine],
      data: {
        labels: labels.map((l) => (l.hour === 0 ? l.day : l.time)),  // weekday marks midnight
        datasets: [
          { label: "Open-Meteo rain (mm)", data: hours.map((h) => h.om), backgroundColor: fade(om, "", "99"), yAxisID: "y",
            borderRadius: 4, borderSkipped: false, order: 3 },
          { label: "MET Norway rain (mm)", data: hours.map((h) => h.met), backgroundColor: fade(met, "", "99"), yAxisID: "y",
            borderRadius: 4, borderSkipped: false, order: 3 },
          { label: "Open-Meteo temp (°C)", type: "line", data: hours.map((h) => h.om_temp), yAxisID: "y2",
            borderColor: om, borderWidth: 2.5, pointRadius: 0, pointHoverRadius: 5, tension: 0.4, order: 1 },
          { label: "MET Norway temp (°C)", type: "line", data: hours.map((h) => h.met_temp), yAxisID: "y2",
            borderColor: met, borderWidth: 2.5, pointRadius: 0, pointHoverRadius: 5, tension: 0.4, borderDash: [6, 4], order: 1 },
          { label: "Rain chance (%)", type: "line", data: hours.map((h) => h.prob), yAxisID: "y3", hidden: true,
            borderColor: text, borderWidth: 1.5, pointRadius: 0, tension: 0.3, order: 2 },
          { label: "Cloud cover (%)", type: "line", data: hours.map((h) => (h.cloud == null ? null : h.cloud)), yAxisID: "y3", hidden: true,
            borderColor: cloudLine, borderWidth: 1.5, borderDash: [2, 3], pointRadius: 0, tension: 0.3, order: 2 },
        ],
      },
      options: {
        locale: "en-GB",  // decimal point, not the browser's decimal comma
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        animation: html.dataset.fx === "off" ? false : { duration: 700, easing: "easeOutQuart" },
        plugins: {
          legend: { labels: { color: text, usePointStyle: true, pointStyle: "rectRounded", boxWidth: 10, boxHeight: 10, padding: 14, font: { size: compact ? 10 : 12, weight: "500" } } },
          tooltip: {
            backgroundColor: tipBg, titleColor: tipFg, bodyColor: tipFg, cornerRadius: 12, padding: 12,
            boxPadding: 4, usePointStyle: true, borderColor: grid, borderWidth: 1,
            callbacks: {
              title: (items) => {
                const l = labels[items[0].dataIndex];
                return l.day + " " + l.time + " (hour starting)";
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            border: { display: false },
            ticks: {
              color: text, maxRotation: 0, autoSkip: false, font: { size: 11 },
              // The bars are hourly, but only some get a label: every 12th hour on a phone,
              // every 6th on a wider screen, so the labels never overlap.
              callback: function (value, index) {
                const step = window.innerWidth < 600 ? 12 : 6;
                return labels[index].hour % step === 0 ? this.getLabelForValue(value) : "";
              },
            },
          },
          y: { beginAtZero: true, title: { display: !compact, text: "rain, mm per hour", color: text },
               grid: { color: grid }, border: { display: false }, ticks: { color: text } },
          y2: { position: "right", title: { display: !compact, text: "temperature, °C", color: text },
                grid: { drawOnChartArea: false }, border: { display: false }, ticks: { color: text } },
          y3: { display: false, min: 0, max: 100 },
        },
      },
    });
    return chart;
  }

  // Warn visitors when the data is old (for example the hourly job stopped). Runs in the browser,
  // so it works even when no new site is being published.
  function staleNotice(data) {
    const el = document.getElementById("stale");
    if (!el) return;
    const hours = (Date.now() - new Date(data.generated_at).getTime()) / 3600000;
    if (hours > data.stale_banner_after_hours) {
      el.textContent = "This forecast is about " + Math.floor(hours) + " hours old. The hourly update may have stopped, so check the time under 'Last updated'.";
      el.hidden = false;
    }
  }

  return {
    root, loadData, staleNotice, fmtMm, fmtTemp, fmtTempC, fmtPct, localParts, dateFmt,
    rainLevel, ICONS, UI_ICONS, drawChart, cssVar, theme, fx, mountSwitches, hint, compass, conditionsHtml,
  };
})();
