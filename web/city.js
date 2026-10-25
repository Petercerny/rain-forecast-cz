// A city page (site/<slug>/index.html): summary, chart and per-day table for one place.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // Two lines in one table cell, one per source, each with a coloured dot.
  function sourceCell(td, omText, metText) {
    for (const [cls, text] of [["om", omText], ["met", metText]]) {
      const line = document.createElement("div");
      const dot = document.createElement("span");
      dot.className = "dot " + cls;
      line.append(dot, " " + text);
      td.appendChild(line);
    }
  }

  const range = (lo, hi) => (typeof lo === "number" && typeof hi === "number" ? `${lo.toFixed(1)} to ${hi.toFixed(1)} °C` : "–");

  function renderDaily(loc) {
    const tbody = $("daily").querySelector("tbody");
    tbody.replaceChildren();
    for (const d of loc.daily) {
      const tr = document.createElement("tr");
      const day = document.createElement("td");
      day.textContent = RF.dateFmt.format(new Date(d.date + "T00:00:00Z")) + (d.hours < 24 ? " (" + d.hours + " h)" : "");
      const rain = document.createElement("td");
      sourceCell(rain, RF.fmtMm(d.open_meteo_mm), RF.fmtMm(d.met_no_mm));
      const temp = document.createElement("td");
      sourceCell(temp, range(d.om_tmin_c, d.om_tmax_c), range(d.met_tmin_c, d.met_tmax_c));
      tr.append(day, rain, temp);
      tbody.appendChild(tr);
    }
  }

  async function init() {
    let data;
    try {
      data = await RF.loadData();
    } catch (err) {
      $("summary").textContent = "Could not load the forecast data. Please try again later.";
      return;
    }
    const loc = data.locations.find((l) => l.name === document.body.dataset.city);
    if (!loc) { $("summary").textContent = "No forecast data for this place."; return; }

    $("updated").textContent = data.generated_at_local + " (Europe/Prague)";
    $("summary").textContent = loc.summary.text;
    $("ai-label").hidden = !loc.summary.ai_generated;  // only shown when an LLM wrote the text

    const now = loc.periods.now;
    $("now").textContent = now ? `Now: ${RF.fmtTempC(now.temp)}, rain chance ${RF.fmtPct(now.prob)}` : "";

    const select = $("location");
    for (const l of data.locations) select.add(new Option(l.name, l.slug));
    select.value = loc.slug;
    select.addEventListener("change", () => { window.location.href = "../" + select.value + "/"; });

    renderDaily(loc);
    const missing = Object.entries(loc.sources).filter(([, v]) => !v).map(([k]) => (k === "met-no" ? "MET Norway" : "Open-Meteo"));
    $("status").textContent = missing.length ? "Currently no recent data from: " + missing.join(", ") + "." : "";

    let chart = RF.drawChart($("chart"), loc);
    // Chart colours come from CSS, so redraw when the phone switches between light and dark mode.
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (chart) chart.destroy();
      chart = RF.drawChart($("chart"), loc);
    });
  }

  init();
})();
