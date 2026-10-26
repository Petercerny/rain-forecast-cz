// A city page (site/<slug>/index.html): hero, summary, chart and per-day cards for one place.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // One line per source, each with a coloured dot.
  function line(cls, text) {
    const row = document.createElement("div");
    row.className = "day-line";
    const dot = document.createElement("span");
    dot.className = "dot " + cls;
    const label = document.createElement("span");
    label.textContent = cls === "om" ? "Open-Meteo" : "MET Norway";
    const value = document.createElement("b");
    value.textContent = text;
    row.append(dot, label, value);
    return row;
  }

  const range = (lo, hi) => (typeof lo === "number" && typeof hi === "number" ? `${lo.toFixed(1)} to ${hi.toFixed(1)} °C` : "–");
  const isNum = (v) => typeof v === "number";

  // A thin bar: where this source's low-to-high range sits between the coldest and warmest value of all days.
  function rangeBar(cls, lo, hi, min, max) {
    const bar = document.createElement("div");
    bar.className = "range " + cls;
    if (isNum(lo) && isNum(hi) && max > min) {
      const fill = document.createElement("i");
      fill.style.setProperty("--l", (100 * (lo - min) / (max - min)).toFixed(1) + "%");
      fill.style.setProperty("--w", Math.max(4, 100 * (hi - lo) / (max - min)).toFixed(1) + "%");
      bar.appendChild(fill);
    }
    return bar;
  }

  // A thin bar: this source's rain for the day compared with the wettest day.
  function rainBar(cls, mm, maxMm) {
    const bar = document.createElement("div");
    bar.className = "rain-bar " + cls;
    if (isNum(mm) && maxMm > 0) {
      const fill = document.createElement("i");
      fill.style.setProperty("--w", Math.max(mm > 0 ? 3 : 0, 100 * mm / maxMm).toFixed(1) + "%");
      bar.appendChild(fill);
    }
    return bar;
  }

  function renderDaily(loc) {
    const grid = $("daily");
    grid.replaceChildren();
    const temps = loc.daily.flatMap((d) => [d.om_tmin_c, d.om_tmax_c, d.met_tmin_c, d.met_tmax_c]).filter(isNum);
    const min = Math.min(...temps), max = Math.max(...temps);
    const maxMm = Math.max(0, ...loc.daily.flatMap((d) => [d.open_meteo_mm, d.met_no_mm]).filter(isNum));

    loc.daily.forEach((d, i) => {
      const card = document.createElement("article");
      card.className = "day-card rise";
      card.style.setProperty("--i", i + 4);

      const head = document.createElement("div");
      head.className = "day-head";
      const title = document.createElement("h3");
      title.textContent = RF.dateFmt.format(new Date(d.date + "T00:00:00Z"));
      head.appendChild(title);
      if (d.hours < 24) {
        const part = document.createElement("span");
        part.textContent = d.hours + " h";
        head.appendChild(part);
      }

      const rain = document.createElement("div");
      rain.className = "day-row";
      rain.innerHTML = '<p class="day-label">Rain</p>';
      rain.append(
        line("om", RF.fmtMm(d.open_meteo_mm)), rainBar("om", d.open_meteo_mm, maxMm),
        line("met", RF.fmtMm(d.met_no_mm)), rainBar("met", d.met_no_mm, maxMm),
      );

      const temp = document.createElement("div");
      temp.className = "day-row";
      temp.innerHTML = '<p class="day-label">Temperature, low to high</p>';
      temp.append(
        line("om", range(d.om_tmin_c, d.om_tmax_c)), rangeBar("om", d.om_tmin_c, d.om_tmax_c, min, max),
        line("met", range(d.met_tmin_c, d.met_tmax_c)), rangeBar("met", d.met_tmin_c, d.met_tmax_c, min, max),
      );

      card.append(head, rain, temp);
      grid.appendChild(card);
    });
  }

  async function init() {
    $("back-ic").innerHTML = RF.UI_ICONS.back;
    RF.mountSwitches($("switches"));

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
    RF.staleNotice(data);
    $("summary").textContent = loc.summary.text;
    $("ai-label").hidden = !loc.summary.ai_generated;  // only shown when an LLM wrote the text

    // Hero: the "now" values, a gradient for the weather level, and the animation on top of it.
    const now = loc.periods.now;
    const lvl = RF.rainLevel(now);
    $("hero").className = "hero rise lvl-" + lvl;
    $("hero-icon").innerHTML = RF.ICONS[lvl];
    $("hero-temp").textContent = now ? RF.fmtTemp(now.temp) : "–";
    $("hero-cond").textContent = RF.LEVEL_SHORT[lvl];
    $("hero-chips").innerHTML = now
      ? `<span class="chip">${RF.UI_ICONS.drop} Rain chance ${RF.fmtPct(now.prob)}</span><span class="chip">${RF.fmtTempC(now.temp)}</span>` +
        `<span class="chip">${loc.totals.hours} h forecast</span>`
      : '<span class="chip">No data right now</span>';
    FX.create($("hero-fx"), { contained: true, onColor: true }).setCondition(lvl);

    const select = $("location");
    for (const l of data.locations) select.add(new Option(l.name, l.slug));
    select.value = loc.slug;
    select.addEventListener("change", () => { window.location.href = "../" + select.value + "/"; });

    renderDaily(loc);
    const missing = Object.entries(loc.sources).filter(([, v]) => !v).map(([k]) => (k === "met-no" ? "MET Norway" : "Open-Meteo"));
    $("status").textContent = missing.length ? "Currently no recent data from: " + missing.join(", ") + "." : "";

    let chart = RF.drawChart($("chart"), loc);
    // Chart colours come from CSS, so redraw when the theme changes.
    document.addEventListener("rf:theme", () => {
      if (chart) chart.destroy();
      chart = RF.drawChart($("chart"), loc);
    });
  }

  init();
})();
