// The numbers behind the map-anchored weather (see weather-gl.js):
//  - a smooth weather FIELD over Czechia, made from the Scene of each place (clouds, fog, rain, snow ...),
//  - a tileable noise texture for clouds,
//  - the position of the sun for a time and place (light direction, shadows, warm light at dusk).
// Positions are metres east / north of a fixed origin in the middle of the country ("ENU"), which is what
// the shaders work in.
const WeatherField = (function () {
  "use strict";

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const rad = Math.PI / 180;

  const ORIGIN = { lon: 15.4, lat: 49.8 };
  const M_LAT = 110574;
  const M_LON = 111320 * Math.cos(ORIGIN.lat * rad);
  const toENU = (lon, lat) => [(lon - ORIGIN.lon) * M_LON, (lat - ORIGIN.lat) * M_LAT];

  // grid over the country plus a wide margin (the edge values simply continue outside)
  const NX = 64, NY = 40, X0 = -520e3, Y0 = -380e3, SX = 1040e3, SY = 760e3;
  const SIGMA = 85e3;          // how far one place's weather reaches (metres)
  const CH = 8;                // low, mid, high cloud, fog | rain, snow, thunder, showery

  // ---------------------------------------------------------------- the field

  function createField() {
    const target = new Float32Array(NX * NY * CH);
    const shown = new Float32Array(NX * NY * CH);
    const a = new Uint8Array(NX * NY * 4), b = new Uint8Array(NX * NY * 4);
    let first = true;

    // stations: [{ lon, lat, v: [8 numbers 0..1] }]
    function set(stations) {
      const pts = stations.map((s) => toENU(s.lon, s.lat));
      for (let j = 0; j < NY; j++) {
        const y = Y0 + ((j + 0.5) / NY) * SY;
        for (let i = 0; i < NX; i++) {
          const x = X0 + ((i + 0.5) / NX) * SX;
          let sum = 0;
          const acc = new Array(CH).fill(0);
          for (let k = 0; k < stations.length; k++) {
            const dx = x - pts[k][0], dy = y - pts[k][1];
            const w = Math.exp(-(dx * dx + dy * dy) / (2 * SIGMA * SIGMA)) + 1e-9;
            sum += w;
            for (let c = 0; c < CH; c++) acc[c] += w * stations[k].v[c];
          }
          const o = (j * NX + i) * CH;
          for (let c = 0; c < CH; c++) target[o + c] = sum > 0 ? acc[c] / sum : 0;
        }
      }
      if (first) { shown.set(target); first = false; }
    }

    // Ease the shown field towards the target; returns true while it is still changing.
    function step(dt) {
      const k = 1 - Math.exp(-dt * 2.4);
      let moving = false;
      for (let i = 0; i < shown.length; i++) {
        const d = target[i] - shown[i];
        if (Math.abs(d) > 0.002) { shown[i] += d * k; moving = true; } else shown[i] = target[i];
      }
      return moving;
    }

    // The two RGBA8 textures the shader reads.
    function pack() {
      for (let n = 0; n < NX * NY; n++) {
        for (let c = 0; c < 4; c++) {
          a[n * 4 + c] = Math.round(clamp(shown[n * CH + c], 0, 1) * 255);
          b[n * 4 + c] = Math.round(clamp(shown[n * CH + 4 + c], 0, 1) * 255);
        }
      }
      return { a, b };
    }

    // bilinear value of one channel at an ENU position (used to place lightning in a stormy cell)
    function sample(x, y, c) {
      const u = clamp(((x - X0) / SX) * NX - 0.5, 0, NX - 1.001), v = clamp(((y - Y0) / SY) * NY - 0.5, 0, NY - 1.001);
      const i = Math.floor(u), j = Math.floor(v), fu = u - i, fv = v - j;
      const g = (ii, jj) => shown[(jj * NX + ii) * CH + c];
      return (g(i, j) * (1 - fu) + g(i + 1, j) * fu) * (1 - fv) + (g(i, j + 1) * (1 - fu) + g(i + 1, j + 1) * fu) * fv;
    }

    return { set, step, pack, sample, nx: NX, ny: NY, min: [X0, Y0], size: [SX, SY] };
  }

  // The 8 channels for one Scene (all 0..1).
  function channels(s) {
    return [s.low / 100, s.mid / 100, s.high / 100, clamp(s.fog + s.haze * 0.35, 0, 1), s.rain, s.snow, s.thunder ? 1 : 0, s.showery ? 1 : 0];
  }

  // ---------------------------------------------------------------- noise texture

  function mulberry32(seed) {
    let t = seed >>> 0;
    return () => { t += 0x6d2b79f5; let r = Math.imul(t ^ (t >>> 15), 1 | t); r ^= r + Math.imul(r ^ (r >>> 7), 61 | r); return ((r ^ (r >>> 14)) >>> 0) / 4294967296; };
  }

  // A square texture of tileable value noise (fractal sums), three independent channels.
  function makeNoise(size) {
    const out = new Uint8Array(size * size * 4);
    const specs = [{ seed: 11, oct: 5, base: 4 }, { seed: 23, oct: 4, base: 8 }, { seed: 37, oct: 4, base: 5 }];
    specs.forEach((sp, ch) => {
      const rnd = mulberry32(sp.seed);
      const lattices = [];
      for (let o = 0; o < sp.oct; o++) {
        const f = sp.base * 2 ** o, g = new Float32Array(f * f);
        for (let i = 0; i < g.length; i++) g[i] = rnd();
        lattices.push({ f, g });
      }
      let norm = 0;
      for (let o = 0; o < sp.oct; o++) norm += 0.5 ** o;
      for (let y = 0; y < size; y++) {
        for (let x = 0; x < size; x++) {
          let v = 0;
          for (let o = 0; o < sp.oct; o++) {
            const { f, g } = lattices[o];
            const fx = (x / size) * f, fy = (y / size) * f;
            const ix = Math.floor(fx), iy = Math.floor(fy);
            let tx = fx - ix, ty = fy - iy;
            tx = tx * tx * (3 - 2 * tx); ty = ty * ty * (3 - 2 * ty);
            const x1 = (ix + 1) % f, y1 = (iy + 1) % f, x0 = ix % f, y0 = iy % f;
            const top = g[y0 * f + x0] * (1 - tx) + g[y0 * f + x1] * tx;
            const bot = g[y1 * f + x0] * (1 - tx) + g[y1 * f + x1] * tx;
            v += (top * (1 - ty) + bot * ty) * 0.5 ** o;
          }
          out[(y * size + x) * 4 + ch] = Math.round(clamp(v / norm, 0, 1) * 255);
        }
      }
    });
    for (let i = 3; i < out.length; i += 4) out[i] = 255;
    return out;
  }

  // ---------------------------------------------------------------- the sun

  // Direction of the sun for a moment (ms) and place: azimuth in radians clockwise from north, elevation in radians.
  // (The usual astronomical approximation, good to a fraction of a degree.)
  function sunPosition(ms, lat, lon) {
    const d = ms / 86400000 - 0.5 + 2440588 - 2451545;
    const M = rad * (357.5291 + 0.98560028 * d);
    const C = rad * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
    const L = M + C + rad * 102.9372 + Math.PI;
    const e = rad * 23.4397;
    const dec = Math.asin(Math.sin(e) * Math.sin(L));
    const ra = Math.atan2(Math.sin(L) * Math.cos(e), Math.cos(L));
    const H = rad * (280.16 + 360.9856235 * d) - rad * -lon - ra;
    const phi = rad * lat;
    const elevation = Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H));
    const az = Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi)) + Math.PI;
    return { azimuth: az, elevation };
  }

  return { ORIGIN, toENU, createField, channels, makeNoise, sunPosition };
})();
