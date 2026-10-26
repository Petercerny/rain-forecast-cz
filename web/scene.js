// The "scene": one plain object that describes the sky above a place at one moment (kind of weather,
// cloud layers, rain / snow / fog amounts, wind, daylight). The markers, the detail panel, the map colours
// and the animation layer all read it, so they always agree. Built from the Open-Meteo hourly data;
// an older data.json without the sky fields still works (the scene is then guessed from the rain chance).
const Scene = (function () {
  "use strict";

  const HOUR = 3600000;
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
  const mean = (list) => (list.length ? list.reduce((a, b) => a + b, 0) / list.length : 0);

  // ---------------------------------------------------------------- weather codes

  // WMO code -> what falls from the sky and how hard (0..1). null = nothing special.
  function fromCode(code) {
    if (!isNum(code)) return null;
    if (code === 45 || code === 48) return { type: "fog" };
    if (code >= 51 && code <= 55) return { type: "drizzle", amt: [0.25, 0.4, 0.6][code - 51] };
    if (code === 56 || code === 57) return { type: "drizzle", amt: code === 56 ? 0.3 : 0.5 };
    if (code === 61 || code === 63 || code === 65) return { type: "rain", amt: [0.45, 0.7, 1][(code - 61) / 2] };
    if (code === 66 || code === 67) return { type: "rain", amt: code === 66 ? 0.5 : 0.8 };
    if (code === 71 || code === 73 || code === 75) return { type: "snow", amt: [0.35, 0.65, 1][(code - 71) / 2] };
    if (code === 77) return { type: "snow", amt: 0.3 };
    if (code >= 80 && code <= 82) return { type: "showers", amt: [0.5, 0.75, 1][code - 80] };
    if (code === 85 || code === 86) return { type: "snow", amt: code === 85 ? 0.5 : 0.9, showery: true };
    if (code === 95) return { type: "thunder", amt: 0.8 };
    if (code === 96 || code === 99) return { type: "thunder", amt: code === 96 ? 0.9 : 1 };
    return null;
  }

  // ---------------------------------------------------------------- scene from fields

  // Turn a bag of numbers into the final scene (kind, label, css class, icon). Used for one hour,
  // one day and for the average over all places, so "kind" always follows from the numbers.
  function finish(s) {
    const precip = Math.max(s.rain, s.snow);
    if (s.thunder > 0.3 && s.rain > 0.2) s.kind = "thunder";
    else if (s.snow > 0.12 && s.snow >= s.rain) s.kind = "snow";
    else if (s.rain > 0.1 && s.showery) s.kind = "showers";
    else if (s.rain > 0.1 && s.drizzle && s.rain < 0.65) s.kind = "drizzle";
    else if (s.rain > 0.1) s.kind = "rain";
    else if (s.fog > 0.35) s.kind = "fog";
    else if (s.cloud >= 85) s.kind = "overcast";
    else if (s.cloud >= 55) s.kind = "cloudy";
    else if (s.cloud >= 25) s.kind = "partly";
    else s.kind = "clear";
    s.precip = precip;
    s.night = s.daylight < 0.5;
    s.label = label(s);
    s.cls = cssClass(s);
    s.icon = iconKey(s);
    return s;
  }

  function label(s) {
    switch (s.kind) {
      case "clear": return s.night ? "Clear night" : s.cloud < 12 ? "Sunny" : "Mostly sunny";
      case "partly": return "Partly cloudy";
      case "cloudy": return "Cloudy";
      case "overcast": return "Overcast";
      case "fog": return "Fog";
      case "drizzle": return "Drizzle";
      case "showers": return s.rain > 0.7 ? "Heavy showers" : "Showers";
      case "rain": return s.rain > 0.75 ? "Heavy rain" : s.rain < 0.4 ? "Light rain" : "Rain";
      case "snow": return s.snow > 0.75 ? "Heavy snow" : s.snow < 0.4 ? "Light snow" : "Snow";
      case "thunder": return "Thunderstorm";
      default: return "No data";
    }
  }

  function cssClass(s) {
    switch (s.kind) {
      case "clear": return s.night ? "wx-night" : "wx-clear";
      case "partly": return s.night ? "wx-nightcloud" : "wx-partly";
      case "cloudy": case "overcast": return "wx-cloudy";
      case "fog": return "wx-fog";
      case "drizzle": return "wx-rain";
      case "showers": case "rain": return s.rain > 0.75 ? "wx-heavy" : "wx-rain";
      case "snow": return "wx-snow";
      case "thunder": return "wx-thunder";
      default: return "wx-none";
    }
  }

  function iconKey(s) {
    switch (s.kind) {
      case "clear": return s.night ? "moon" : "sun";
      case "partly": return s.night ? "partly-night" : "partly-day";
      case "cloudy": case "overcast": return "cloud";
      case "fog": return "fog";
      case "drizzle": case "rain": case "showers": return s.rain > 0.75 ? "heavy" : "rain";
      case "snow": return "snow";
      case "thunder": return "thunder";
      default: return "none";
    }
  }

  const blank = () => ({
    kind: "none", rain: 0, snow: 0, thunder: 0, showery: false, drizzle: false, heavyCode: false, fog: 0, haze: 0,
    cloud: 0, low: 0, mid: 0, high: 0, wind: 0, gust: 0, dir: 270, sunshine: 0.5, temp: null, prob: null, mm: 0,
    vis: null, hum: null, daylight: 1, sunPos: 0.5, moonPos: 0.5, t: 0,
  });

  // Fill a scene from the weather fields of one hour (h) or one day (d). `d` carries the same ideas
  // under other names, so both callers go through the same rules.
  function fill(s, f) {
    const code = fromCode(f.code);
    const mm = isNum(f.mm) ? f.mm : 0;
    s.mm = mm;
    s.temp = f.temp;
    s.prob = f.prob;
    s.vis = f.vis;
    s.hum = f.hum;
    s.wind = isNum(f.wind) ? f.wind : 0;
    s.gust = isNum(f.gust) ? f.gust : s.wind;
    s.dir = isNum(f.dir) ? f.dir : 270;
    s.haze = isNum(f.hum) ? smooth(82, 100, f.hum) * 0.5 : 0;

    // cloud layers: use what we have, derive the rest from the total
    const total = isNum(f.cloud) ? f.cloud : null;
    s.cloud = total != null ? total : isNum(f.low) ? Math.max(f.low, f.mid || 0, f.high || 0) : 0;
    s.low = isNum(f.low) ? f.low : s.cloud * 0.6;
    s.mid = isNum(f.mid) ? f.mid : s.cloud * 0.6;
    s.high = isNum(f.high) ? f.high : s.cloud * 0.6;
    s.sunshine = isNum(f.sunshine) ? clamp(f.sunshine, 0, 1) : clamp(1 - s.cloud / 100, 0, 1);

    // precipitation: the WMO code says what it is, the millimetres say how hard
    const byMm = mm >= 0.1 ? 0.15 + 0.85 * clamp(mm / 4, 0, 1) : 0;
    if (code && code.type !== "fog") {
      s.heavyCode = code.amt >= 0.7;
      const amt = Math.max(code.amt, byMm * 0.6);
      if (code.type === "snow") { s.snow = amt; s.showery = !!code.showery; }
      else {
        s.rain = amt;
        s.showery = code.type === "showers";
        s.drizzle = code.type === "drizzle";
        if (code.type === "thunder") s.thunder = 1;
      }
    } else if (byMm > 0) {
      s.rain = byMm;  // the numbers disagree with the code (or there is none): trust the millimetres
      s.drizzle = mm < 0.4;
    }
    // fog: from the code and from how far you can see
    const visFog = isNum(f.vis) ? 1 - smooth(500, 6000, f.vis) : 0;
    s.fog = clamp(Math.max(code && code.type === "fog" ? 0.85 : 0, visFog * 0.9), 0, 1);
    return s;
  }

  // ---------------------------------------------------------------- time helpers

  const TZ = "Europe/Prague";
  const dateFmt = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
  const hourFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", hour12: false });
  const pragueDate = (ms) => dateFmt.format(new Date(ms));            // "2026-10-26"
  const pragueHour = (ms) => Number(hourFmt.format(new Date(ms))) % 24;
  const addDays = (iso, n) => { const d = new Date(iso + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };

  // Daylight 0..1 with a soft 40-minute dusk and dawn, from the sunrise / sunset list of one place.
  // `fallback` is the hourly is_day flag, used when there is no sun data for that moment.
  function daylight(sun, ms, fallback) {
    if (!sun || !sun.length) return fallback == null ? 1 : fallback;
    const edge = 40 * 60000;
    let best = 0, covered = false;
    for (const e of sun) {
      const rise = Date.parse(e.sunrise), set = Date.parse(e.sunset);
      if (!isNum(rise) || !isNum(set)) continue;
      if (ms > rise - 12 * HOUR && ms < set + 12 * HOUR) covered = true;
      best = Math.max(best, smooth(rise - edge, rise + edge, ms) * (1 - smooth(set - edge, set + edge, ms)));
    }
    return covered ? best : (fallback == null ? 1 : fallback);
  }

  // Where the sun (day) or the moon (night) is on its way across the sky: 0 = rising side, 1 = setting side.
  function skyPos(sun, ms) {
    const marks = [];
    for (const e of sun || []) { marks.push({ t: Date.parse(e.sunrise), up: true }, { t: Date.parse(e.sunset), up: false }); }
    marks.sort((a, b) => a.t - b.t);
    let prev = null, next = null;
    for (const m of marks) { if (m.t <= ms) prev = m; else { next = m; break; } }
    let sunPos = null, moonPos = null;
    if (prev && next) {
      const p = (ms - prev.t) / (next.t - prev.t);
      if (prev.up) sunPos = p; else moonPos = p;
    }
    if (sunPos == null && moonPos == null) {  // outside the data: guess from the clock
      const h = pragueHour(ms);
      if (h >= 7 && h < 17) sunPos = (h - 7) / 10; else moonPos = ((h + 6) % 24) / 14;
    }
    return { sunPos: sunPos == null ? 0.5 : clamp(sunPos, 0, 1), moonPos: moonPos == null ? 0.5 : clamp(moonPos, 0, 1) };
  }

  function timing(s, loc, ms, dayFlag) {
    s.t = ms;
    // no sun times and no is_day flag (an old data.json): guess from the clock
    if (dayFlag == null) { const h = pragueHour(ms); dayFlag = h >= 7 && h < 18 ? 1 : 0; }
    s.daylight = daylight(loc.sun, ms, dayFlag);
    Object.assign(s, skyPos(loc.sun, ms));
    return s;
  }

  // ---------------------------------------------------------------- public: one place

  const hasSky = (h) => h && (h.code != null || h.cloud != null);

  // Index of the hour that contains `ms` (clamped to the forecast).
  function hourIndex(loc, ms) {
    const hs = loc.hourly;
    if (!hs.length) return -1;
    let lo = 0;
    for (let i = 0; i < hs.length; i++) { if (Date.parse(hs[i].t) <= ms) lo = i; else break; }
    return lo;
  }

  // What the legacy (rain chance only) data says, so a data.json from before the sky fields still draws something.
  function legacy(prob, mm) {
    const level = RF.rainLevel({ prob, rain_mm: mm });
    return { code: level === 3 ? 63 : level === 2 ? 61 : null, cloud: level >= 1 ? (level === 1 ? 60 : 90) : 10, mm: level >= 2 ? mm : 0 };
  }

  function mmOf(h) {
    const v = [h.om, h.met].filter(isNum);
    return v.length ? mean(v) : 0;
  }

  // The scene at hourly[i] of one place.
  function atIndex(loc, i) {
    const h = loc.hourly[i];
    const s = blank();
    if (!h) return finish(timing(s, loc, Date.now(), 1));
    const ms = Date.parse(h.t) + HOUR / 2;  // the middle of the hour
    let f;
    if (hasSky(h)) {
      f = { code: h.code, cloud: h.cloud, low: h.cloud_l, mid: h.cloud_m, high: h.cloud_h, mm: mmOf(h), temp: h.temp, prob: h.prob,
            vis: h.vis, hum: h.hum, wind: h.wind, gust: h.gust, dir: h.dir, sunshine: isNum(h.sun_s) ? h.sun_s / 3600 : null };
    } else {
      f = Object.assign(legacy(h.prob, mmOf(h)), { temp: h.temp, prob: h.prob });
    }
    fill(s, f);
    timing(s, loc, ms, h.day);
    return finish(s);
  }

  // Index of the first hourly entry on local calendar day number k (0 = the day of the first hour), or -1.
  function dayStart(loc, k) {
    if (!loc.hourly.length) return -1;
    const date = addDays(pragueDate(Date.parse(loc.hourly[0].t)), k);
    return loc.hourly.findIndex((h) => pragueDate(Date.parse(h.t)) === date);
  }

  // The hour that stands for day k: the one nearest 13:00, or "now" for today when it is already later.
  function middayIndex(loc, k) {
    const first = dayStart(loc, k);
    if (first < 0) return Math.max(0, loc.hourly.length - 1);
    if (k === 0 && pragueHour(Date.parse(loc.hourly[0].t)) >= 13) return 0;
    let best = first, bestGap = 99;
    for (let i = first; i < loc.hourly.length && pragueDate(Date.parse(loc.hourly[i].t)) === pragueDate(Date.parse(loc.hourly[first].t)); i++) {
      const gap = Math.abs(pragueHour(Date.parse(loc.hourly[i].t)) - 13);
      if (gap < bestGap) { best = i; bestGap = gap; }
    }
    return best;
  }

  // The scene that stands for a whole day k: its worst weather and average cloud, in daylight.
  function fromDay(loc, k) {
    const first = dayStart(loc, k);
    const date = first < 0 ? null : pragueDate(Date.parse(loc.hourly[first].t));
    const d = date && loc.daily.find((x) => x.date === date);
    const mid = middayIndex(loc, k);
    if (!d || d.worst_weather_code == null) return atIndex(loc, mid);  // no daily sky data: fall back to the midday hour
    const s = blank();
    const hrs = loc.hourly.filter((h) => pragueDate(Date.parse(h.t)) === date);
    const mm = Math.max(isNum(d.open_meteo_mm) ? d.open_meteo_mm : 0, isNum(d.met_no_mm) ? d.met_no_mm : 0);
    fill(s, {
      code: d.worst_weather_code, cloud: d.cloud_avg_pct, low: mean(hrs.map((h) => h.cloud_l).filter(isNum)), mid: mean(hrs.map((h) => h.cloud_m).filter(isNum)),
      high: mean(hrs.map((h) => h.cloud_h).filter(isNum)), mm: mm / Math.max(1, d.hours), temp: d.avg_tmax_c, prob: d.max_prob_pct,
      vis: d.visibility_min_m, hum: d.humidity_avg_pct, wind: d.wind_max_ms, gust: d.gust_max_ms, dir: hrs.length ? hrs[Math.floor(hrs.length / 2)].dir : 270,
      sunshine: isNum(d.sunshine_h) ? d.sunshine_h / 9 : null,
    });
    // a whole day is shown as a daytime scene, with the sun at its midday position
    timing(s, loc, Date.parse(loc.hourly[mid].t) + HOUR / 2, 1);
    s.daylight = 1;
    return finish(s);
  }

  // ---------------------------------------------------------------- public: the whole country

  // The average sky over all places (or over the places in `list`): amounts are averaged, so a country
  // that is half rainy shows light rain, and the kind / label follow from the averaged numbers.
  function region(scenes) {
    const s = blank();
    if (!scenes.length) return finish(s);
    const avg = (key) => mean(scenes.map((x) => x[key]));
    for (const k of ["rain", "snow", "thunder", "fog", "haze", "cloud", "low", "mid", "high", "wind", "gust", "sunshine", "mm", "daylight", "sunPos", "moonPos", "t"]) s[k] = avg(k);
    const temps = scenes.map((x) => x.temp).filter(isNum);
    s.temp = temps.length ? mean(temps) : null;
    // wind direction: the average of the vectors, not of the angles
    const vx = mean(scenes.map((x) => Math.sin(x.dir * Math.PI / 180))), vy = mean(scenes.map((x) => Math.cos(x.dir * Math.PI / 180)));
    s.dir = (Math.atan2(vx, vy) * 180 / Math.PI + 360) % 360;
    s.showery = scenes.filter((x) => x.showery).length > scenes.length / 2;
    s.drizzle = scenes.filter((x) => x.drizzle).length > scenes.length / 2;
    s.heavyCode = false;
    // one storm anywhere is enough for some distant lightning
    s.thunder = scenes.some((x) => x.thunder > 0) && s.rain > 0.2 ? 1 : 0;
    return finish(s);
  }

  // One call for the common case: the scenes of all places at hour i.
  const regionAt = (locations, i) => region(locations.map((l) => atIndex(l, Math.min(i, l.hourly.length - 1))));
  const regionDay = (locations, k) => region(locations.map((l) => fromDay(l, k)));

  return { atIndex, hourIndex, fromDay, middayIndex, dayStart, region, regionAt, regionDay, daylight, pragueDate, pragueHour, finish, blank };
})();
