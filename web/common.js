// Shared by the map page and the city pages: data loading, formatting, rain icons, the chart.
// Plain JavaScript; everything is exposed through one global object, RF.
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

  // ---------------------------------------------------------------- rain icons

  // Level from the rain chance (Open-Meteo): 0 sun (<20 %), 1 cloud (20-49), 2 light rain (50-79), 3 rain (80+).
  // If a source only gave rain amounts, fall back to those. -1 = no data at all.
  function rainLevel(period) {
    if (!period) return -1;
    if (isNum(period.prob)) return period.prob < 20 ? 0 : period.prob < 50 ? 1 : period.prob < 80 ? 2 : 3;
    if (isNum(period.rain_mm)) return period.rain_mm < 0.1 ? 0 : period.rain_mm < 1 ? 2 : 3;
    return -1;
  }

  const SVG = (inner) =>
    '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + inner + "</svg>";
  const CLOUD = '<path d="M7 17a4 4 0 0 1-.5-7.97A5.5 5.5 0 0 1 17 8.5a4.25 4.25 0 0 1 .5 8.5z"/>';
  const ICONS = {
    "-1": SVG('<circle cx="12" cy="12" r="8"/><path d="M9 12h6"/>'),
    0: SVG('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
    1: SVG(CLOUD),
    2: SVG('<path d="M7 14a4 4 0 0 1-.5-7.97A5.5 5.5 0 0 1 17 5.5a4.25 4.25 0 0 1 .5 8.5z"/><path d="M9 18l-1 2M14 18l-1 2"/>'),
    3: SVG('<path d="M7 13a4 4 0 0 1-.5-7.97A5.5 5.5 0 0 1 17 4.5a4.25 4.25 0 0 1 .5 8.5z"/><path d="M8 16l-1.2 3M12 16l-1.2 3M16 16l-1.2 3"/>'),
  };
  const LEVEL_TEXT = { "-1": "no data", 0: "dry", 1: "low chance of rain", 2: "likely light rain", 3: "likely rain" };

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

    const chart = new Chart(canvas, {
      type: "bar",
      data: {
        labels: labels.map((l) => (l.hour === 0 ? l.day : l.time)),  // weekday marks midnight
        datasets: [
          { label: "Open-Meteo rain (mm)", data: hours.map((h) => h.om), backgroundColor: om, yAxisID: "y", order: 3 },
          { label: "MET Norway rain (mm)", data: hours.map((h) => h.met), backgroundColor: met, yAxisID: "y", order: 3 },
          { label: "Open-Meteo temp (°C)", type: "line", data: hours.map((h) => h.om_temp), yAxisID: "y2",
            borderColor: om, borderWidth: 2, pointRadius: 0, tension: 0.3, order: 1 },
          { label: "MET Norway temp (°C)", type: "line", data: hours.map((h) => h.met_temp), yAxisID: "y2",
            borderColor: met, borderWidth: 2, pointRadius: 0, tension: 0.3, borderDash: [5, 3], order: 1 },
          { label: "Rain chance (%)", type: "line", data: hours.map((h) => h.prob), yAxisID: "y3", hidden: true,
            borderColor: text, borderWidth: 1.5, pointRadius: 0, order: 2 },
        ],
      },
      options: {
        locale: "en-GB",  // decimal point, not the browser's decimal comma
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { color: text, boxWidth: 12, font: { size: compact ? 10 : 12 } } },
          tooltip: {
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
               grid: { color: grid }, ticks: { color: text } },
          y2: { position: "right", title: { display: !compact, text: "temperature, °C", color: text },
                grid: { drawOnChartArea: false }, ticks: { color: text } },
          y3: { display: false, min: 0, max: 100 },
        },
      },
    });
    return chart;
  }

  return { root, loadData, fmtMm, fmtTemp, fmtTempC, fmtPct, localParts, dateFmt, rainLevel, ICONS, LEVEL_TEXT, drawChart, cssVar };
})();
