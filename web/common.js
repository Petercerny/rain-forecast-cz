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

  // The <head> of every page sets data-theme / data-fx before the first paint. These helpers
  // change them later, remember the choice, and tell the rest of the page (rf:theme, rf:fx).
  const html = document.documentElement;
  const store = {
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode: fine */ } },
  };

  function setAttr(name, value, event) {
    if (html.dataset[name] === value) return;
    html.dataset[name] = value;
    store.set("rf-" + name, value);
    document.dispatchEvent(new CustomEvent(event, { detail: value }));
  }
  const theme = {
    get: () => html.dataset.theme || "light",
    set: (v) => setAttr("theme", v, "rf:theme"),
    toggle() { theme.set(theme.get() === "dark" ? "light" : "dark"); },
  };
  const fx = {
    get: () => html.dataset.fx || "on",
    set: (v) => setAttr("fx", v, "rf:fx"),
    toggle() { fx.set(fx.get() === "on" ? "off" : "on"); },
  };

  const UI_ICONS = {
    sun: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.3M12 19.2v2.3M2.5 12h2.3M19.2 12h2.3M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/></svg>',
    moon: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 14.6A8.6 8.6 0 0 1 9.4 3.5a8.6 8.6 0 1 0 11.1 11.1z"/></svg>',
    fx: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7.5 15a4.2 4.2 0 0 1-.6-8.35A5.7 5.7 0 0 1 18 7.6 3.7 3.7 0 0 1 17.5 15z"/><path d="M9 18.2l-.8 2M13 18.2l-.8 2M17 18.2l-.8 2"/></svg>',
    list: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8.5 6.5H20M8.5 12H20M8.5 17.5H20"/><circle cx="4.2" cy="6.5" r=".6"/><circle cx="4.2" cy="12" r=".6"/><circle cx="4.2" cy="17.5" r=".6"/></svg>',
    info: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.2"/><circle cx="12" cy="7.8" r=".5"/></svg>',
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    plus: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
    minus: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14"/></svg>',
    home: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 11l8-6.5 8 6.5"/><path d="M6 9.8V19h4.2v-5h3.6v5H18V9.8"/></svg>',
    back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg>',
    drop: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5c3.2 3.9 5.5 6.6 5.5 9.6a5.5 5.5 0 0 1-11 0c0-3 2.3-5.7 5.5-9.6z"/></svg>',
    arrow: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  };

  // Mount the two switches (animations on/off, sun/moon) into a container. Returns nothing;
  // the buttons keep themselves in sync with the page state.
  function mountSwitches(container) {
    const fxBtn = document.createElement("button");
    fxBtn.type = "button";
    fxBtn.className = "tool fx-switch";
    fxBtn.innerHTML = UI_ICONS.fx + '<span class="tool-slash"></span>';
    const themeBtn = document.createElement("button");
    themeBtn.type = "button";
    themeBtn.className = "tool theme-switch";
    themeBtn.innerHTML = '<span class="swap swap-sun">' + UI_ICONS.sun + '</span><span class="swap swap-moon">' + UI_ICONS.moon + "</span>";

    function sync() {
      const fxOn = fx.get() === "on", dark = theme.get() === "dark";
      fxBtn.setAttribute("aria-pressed", String(fxOn));
      fxBtn.setAttribute("aria-label", "Weather animations");
      fxBtn.title = fxOn ? "Turn weather animations off" : "Turn weather animations on";
      themeBtn.setAttribute("aria-pressed", String(dark));
      themeBtn.setAttribute("aria-label", "Dark mode");
      themeBtn.title = dark ? "Switch to light mode" : "Switch to dark mode";
    }
    fxBtn.addEventListener("click", fx.toggle);
    themeBtn.addEventListener("click", theme.toggle);
    document.addEventListener("rf:fx", sync);
    document.addEventListener("rf:theme", sync);
    sync();
    container.append(fxBtn, themeBtn);
  }

  // ---------------------------------------------------------------- weather icons

  // Level from the rain chance (Open-Meteo): 0 sun (<20 %), 1 cloud (20-49), 2 light rain (50-79), 3 rain (80+).
  // If a source only gave rain amounts, fall back to those. -1 = no data at all.
  function rainLevel(period) {
    if (!period) return -1;
    if (isNum(period.prob)) return period.prob < 20 ? 0 : period.prob < 50 ? 1 : period.prob < 80 ? 2 : 3;
    if (isNum(period.rain_mm)) return period.rain_mm < 0.1 ? 0 : period.rain_mm < 1 ? 2 : 3;
    return -1;
  }

  // Soft two-tone icons: a translucent fill plus a rounded outline, drawn with currentColor.
  // The classes (ic-rays, ic-cloud, ic-drop) are what the CSS animates when animations are on.
  const SVG = (cls, inner) =>
    `<svg class="ic ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ` +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + inner + "</svg>";
  const CLOUD = (y) => `<path class="ic-cloud" fill="currentColor" fill-opacity=".18" d="M7 ${y + 3}a4 4 0 0 1-.5-7.97A5.5 5.5 0 0 1 17 ${y - 5.5}a4.25 4.25 0 0 1 .5 8.5z"/>`;
  const DROP = (x, y, n) => `<path class="ic-drop d${n}" d="M${x} ${y}l-1 2.4"/>`;
  const ICONS = {
    "-1": SVG("ic-none", '<circle cx="12" cy="12" r="8"/><path d="M9 12h6"/>'),
    0: SVG("ic-sun", '<g class="ic-rays"><path d="M12 2.5v2M12 19.5v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M2.5 12h2M19.5 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4"/></g>' +
      '<circle class="ic-core" cx="12" cy="12" r="4.4" fill="currentColor" fill-opacity=".28"/>'),
    1: SVG("ic-clouds", CLOUD(14)),
    2: SVG("ic-rainy", CLOUD(11) + DROP(9.5, 17.2, 1) + DROP(14.5, 17.2, 2)),
    3: SVG("ic-rainy ic-heavy", CLOUD(10) + DROP(8, 16.2, 1) + DROP(12, 16.2, 2) + DROP(16, 16.2, 3) + DROP(10, 20, 4) + DROP(14, 20, 5)),
  };
  const LEVEL_TEXT = { "-1": "no data", 0: "dry", 1: "low chance of rain", 2: "likely light rain", 3: "likely rain" };
  const LEVEL_SHORT = { "-1": "No data", 0: "Dry", 1: "Cloudy", 2: "Light rain", 3: "Rain" };

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
    const tipBg = cssVar("--tip-bg"), tipFg = cssVar("--tip-fg");

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

    const chart = new Chart(canvas, {
      type: "bar",
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
    rainLevel, ICONS, UI_ICONS, LEVEL_TEXT, LEVEL_SHORT, drawChart, cssVar, theme, fx, mountSwitches,
  };
})();
