// Loads data.json and draws the page. Plain JavaScript, no build step.
(function () {
  "use strict";

  const TZ = "Europe/Prague";
  const hourFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
  const dayFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, weekday: "short" });
  const dateFmt = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", weekday: "short", day: "numeric", month: "short" });

  const $ = (id) => document.getElementById(id);
  let data = null;
  let chart = null;

  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const fmtMm = (v) => (v === null || v === undefined ? "–" : v.toFixed(1) + " mm");

  function localParts(iso) {
    const d = new Date(iso);
    return { hour: Number(hourFmt.format(d).slice(0, 2)), time: hourFmt.format(d), day: dayFmt.format(d) };
  }

  function drawChart(loc) {
    if (typeof Chart === "undefined") {
      $("chart").replaceWith(Object.assign(document.createElement("p"), {
        textContent: "The chart library could not be loaded. The numbers below are still correct.",
      }));
      return;
    }
    const hours = loc.hourly;
    const labels = hours.map((h) => localParts(h.t));
    const text = cssVar("--muted"), grid = cssVar("--line");

    const config = {
      type: "bar",
      data: {
        labels: labels.map((l) => (l.hour === 0 ? l.day : l.time)),  // weekday marks midnight
        datasets: [
          { label: "Open-Meteo (mm)", data: hours.map((h) => h.om), backgroundColor: cssVar("--om"), yAxisID: "y", order: 2 },
          { label: "MET Norway (mm)", data: hours.map((h) => h.met), backgroundColor: cssVar("--met"), yAxisID: "y", order: 2 },
          { label: "Rain chance, Open-Meteo (%)", type: "line", data: hours.map((h) => h.prob), yAxisID: "y2",
            borderColor: text, borderDash: [4, 3], borderWidth: 1.5, pointRadius: 0, order: 1 },
        ],
      },
      options: {
        locale: "en-GB",  // decimal point, not the browser's decimal comma
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { color: text, boxWidth: 12, font: { size: 12 } } },
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
          y: { beginAtZero: true, title: { display: true, text: "mm per hour", color: text }, grid: { color: grid }, ticks: { color: text } },
          y2: { position: "right", min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { color: text, callback: (v) => v + "%" } },
        },
      },
    };
    if (chart) chart.destroy();
    chart = new Chart($("chart"), config);
  }

  function renderDaily(loc) {
    const tbody = $("daily").querySelector("tbody");
    tbody.replaceChildren();
    for (const d of loc.daily) {
      const tr = document.createElement("tr");
      const label = dateFmt.format(new Date(d.date + "T00:00:00Z")) + (d.hours < 24 ? " (" + d.hours + " h)" : "");
      for (const cell of [label, fmtMm(d.open_meteo_mm), fmtMm(d.met_no_mm)]) {
        const td = document.createElement("td");
        td.textContent = cell;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }

  function renderStatus(loc) {
    const missing = Object.entries(loc.sources).filter(([, v]) => !v).map(([k]) => (k === "met-no" ? "MET Norway" : "Open-Meteo"));
    $("status").textContent = missing.length
      ? "Currently no recent data from: " + missing.join(", ") + "."
      : "";
  }

  function render(name) {
    const loc = data.locations.find((l) => l.name === name);
    $("summary").textContent = loc.summary.text;
    $("ai-label").hidden = !loc.summary.ai_generated;  // only shown when an LLM wrote the text
    renderDaily(loc);
    renderStatus(loc);
    drawChart(loc);
    try { localStorage.setItem("location", name); } catch (e) { /* storage may be blocked */ }
  }

  async function init() {
    try {
      const resp = await fetch("data.json", { cache: "no-cache" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      data = await resp.json();
    } catch (err) {
      $("summary").textContent = "Could not load the forecast data. Please try again later.";
      return;
    }
    $("updated").textContent = data.generated_at_local + " (Europe/Prague)";

    const select = $("location");
    for (const l of data.locations) select.add(new Option(l.name, l.name));
    let initial = data.locations[0].name;
    try {
      const saved = localStorage.getItem("location");
      if (saved && data.locations.some((l) => l.name === saved)) initial = saved;
    } catch (e) { /* ignore */ }
    select.value = initial;
    select.addEventListener("change", () => render(select.value));
    // Redraw when the phone switches between light and dark mode (chart colours come from CSS).
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => render(select.value));
    render(initial);
  }

  init();
})();
