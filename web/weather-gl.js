// Map-anchored weather: two MapLibre "custom layers" that draw clouds, cloud shadows, fog, rain, snow and
// lightning in the same WebGL context as the map. Everything is placed in map coordinates, so it moves with
// the map (pan, zoom, rotate, tilt), differs from region to region, and the clouds hover above the ground, so
// they parallax when the map is tilted.
//
//   const weather = WeatherGL.attach(map, { before: firstLabelLayerId, onFail: () => ... });
//   weather.setScene(stations, globals);   // stations: [{ lon, lat, scene }], globals: the shown Scene
//   weather.setEnabled(false);             // the animation switch
//
// How it works, in short:
//   1. weather-field.js turns the scenes of the places into a smooth grid (two small textures).
//   2. Every frame the camera rays are worked out on the CPU (in doubles) and handed to the shader, which
//      intersects each pixel's ray with the ground and with three cloud planes.
//   3. Pass 1 renders clouds + shadows + fog at reduced resolution into a framebuffer (the quality adapts to
//      the frame rate). The ground layer (under the labels) paints the shadows and fog; the sky layer (on top)
//      paints the clouds and then rain, snow and lightning at full resolution.
const WeatherGL = (function () {
  "use strict";

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
  const lerp = (a, b, k) => a + (b - a) * k;
  const rad = Math.PI / 180;
  const TIME_WARP = 650;                          // wind speed is shown this many times faster than it blows
  const SCALES = [0.34, 0.42, 0.5, 0.6, 0.7];     // the quality steps (share of the screen size for pass 1)

  // ---------------------------------------------------------------- shaders

  const VERT = `#version 300 es
layout(location = 0) in vec2 aPos;
out vec2 vUv;
void main() { vUv = aPos * 0.5 + 0.5; gl_Position = vec4(aPos, 0.0, 1.0); }`;

  // Pass 1: for every pixel of the reduced-resolution target, find where its ray meets the ground and the three
  // cloud planes, and compute the cloud colour/opacity there, the cloud shadow on the ground, and fog.
  const PASS1 = `#version 300 es
precision highp float;
in vec2 vUv;
layout(location = 0) out vec4 oGround;
layout(location = 1) out vec4 oSky;

uniform vec3 uCam, uR00, uR10, uR01, uR11;
uniform sampler2D uF0, uF1, uNoise;
uniform vec2 uFieldMin, uFieldInv, uDrift, uWind, uFlashPos;
uniform vec3 uSunDir, uLit, uShade, uFogCol, uAlt;
uniform float uDay, uMpp, uPitch, uLite, uFade, uFlashAmt, uTanE;

// how big the clouds are: the length (metres) after which each layer's noise pattern repeats
const float PL = 170e3, PM = 300e3, PH = 480e3;

vec3 gD;
float gN;     // the raw noise value of the last dens() call (for variation inside thick cloud)

vec4 F0(vec2 p) { return textureLod(uF0, (p - uFieldMin) * uFieldInv, 0.0); }
vec4 F1(vec2 p) { return textureLod(uF1, (p - uFieldMin) * uFieldInv, 0.0); }

float cloudN(vec2 q) {
  vec2 a = textureLod(uNoise, q, 0.0).rg;
  vec2 b = textureLod(uNoise, q * 2.37 + vec2(0.31, 0.77), 0.0).rg;
  return a.r * 0.58 + a.g * 0.24 + b.r * 0.18;
}

// density of one cloud layer at a position on its plane (0..1). cover = how much of the sky it should fill.
float dens(vec2 pos, float cover, float period, float drift, float aniso) {
  vec2 p = pos - uDrift * drift;
  vec2 c = vec2(-uWind.y, uWind.x);
  vec2 q = vec2(dot(p, uWind) / (period * aniso), dot(p, c) / period);
  gN = cloudN(q);
  float nn = (gN - 0.5) * 2.4 + 0.5;
  float thr = 1.12 - cover * 1.2;
  return smoothstep(thr - 0.22, thr + 0.22, nn);
}

vec4 cloudLayer(float alt, int ch, float period, float speed, float aniso, float maxA) {
  float t = (alt - uCam.z) / gD.z;
  vec2 C = uCam.xy + gD.xy * t;
  vec4 f0 = F0(C);
  float cover = ch == 0 ? f0.r : (ch == 1 ? f0.g : f0.b);
  float dn = dens(C, cover, period, speed, aniso);
  float body = gN;
  vec2 sh = normalize(uSunDir.xy + vec2(1e-4));
  float ds = dens(C + sh * period * 0.06, cover, period, speed, aniso);   // is there more cloud towards the sun?
  float lit = clamp(0.58 + (dn - ds) * 2.4 + (body - 0.5) * 1.0, 0.0, 1.0);
  vec4 f1 = F1(C);
  float storm = clamp(f1.r * 1.1 + f1.b * 0.6 + f1.g * 0.35, 0.0, 1.0);
  vec3 col = mix(uShade, uLit, lit);
  vec3 slate = mix(vec3(0.08, 0.1, 0.18), vec3(0.3, 0.33, 0.4), uDay);   // storm clouds turn slate grey
  col = mix(col, slate * (0.55 + 0.45 * lit), storm * 0.6);
  vec2 dd = C - uFlashPos;
  col += uFlashAmt * exp(-dot(dd, dd) / 3.6e9) * vec3(0.55, 0.65, 1.0);   // lightning lights nearby cloud
  float a = dn * maxA * (0.72 + 0.5 * smoothstep(0.25, 0.8, body));
  a = min(a, maxA);
  return vec4(col * a, a);
}

void main() {
  gD = mix(mix(uR00, uR10, vUv.x), mix(uR01, uR11, vUv.x), vUv.y);
  float tg = -uCam.z / gD.z;
  vec2 G = uCam.xy + gD.xy * tg;

  // --- ground: cloud shadows, wet ground, fog
  float shadow = 0.0;
  if (uDay > 0.03) {
    vec2 sh = normalize(uSunDir.xy + vec2(1e-4));
    float maxOff = 70.0 * uMpp;
    vec2 pL = G + sh * min(uAlt.x / uTanE, maxOff);
    float low = dens(pL, F0(pL).r, PL, 1.0, 1.0);
    vec2 pM = G + sh * min(uAlt.y / uTanE, maxOff);
    float mid = dens(pM, F0(pM).g, PM, 1.35, 1.0);
    shadow = 1.0 - (1.0 - low * 0.9) * (1.0 - mid * 0.6);
  }
  vec4 f1 = F1(G);
  float wet = clamp(f1.r + f1.g * 0.3, 0.0, 1.0) * 0.10;
  float fogN = textureLod(uNoise, (G - uDrift * 0.5) / 45e3, 0.0).b;
  float fogD = clamp(F0(G).a * (0.45 + 0.9 * fogN), 0.0, 1.0);
  float haze = uPitch * 0.3 * smoothstep(0.4, 1.0, vUv.y);     // atmospheric haze towards the far edge when tilted
  float sA = clamp(shadow * 0.34 * uDay + wet, 0.0, 0.6);
  float fA = clamp(fogD * 0.85 + haze, 0.0, 0.95);
  vec3 prem = uFogCol * fA + (1.0 - fA) * vec3(0.02, 0.04, 0.12) * sA;
  oGround = vec4(prem, fA + (1.0 - fA) * sA) * uFade;

  // --- sky: three layers of cloud, high ones in front
  vec4 lo = cloudLayer(uAlt.x, 0, PL, 1.0, 1.0, 0.66);
  vec4 md = cloudLayer(uAlt.y, 1, PM, 1.35, 1.0, 0.52);
  vec4 hi = uLite < 0.5 ? cloudLayer(uAlt.z, 2, PH, 1.8, 2.4, 0.38) : vec4(0.0);
  vec4 s = hi + (1.0 - hi.a) * (md + (1.0 - md.a) * lo);
  oSky = s * uFade;
}`;

  // Pass 2 (full resolution): the clouds from pass 1, then rain, snow, lightning on top. Rain and snow are drawn as
  // screen-space streaks / flakes (they fall past the camera), but where they appear is decided by the weather field
  // at the point of the ground under each pixel, so rain areas have real edges and slide with the map.
  const PASS2 = `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 oColor;

uniform sampler2D uSky, uF1, uNoise;
uniform vec3 uCam, uR00, uR10, uR01, uR11, uRainCol;
uniform vec2 uFieldMin, uFieldInv, uDrift;
uniform vec4 uBolt[16];
uniform int uBoltN;
uniform float uTime, uFade, uSkyOp, uDrizzle, uPx, uLite, uPitch, uSlant, uFlashAmt, uBoltAmt, uShowery;

float hash11(float p) { p = fract(p * 0.1031); p *= p + 33.33; p *= p + p; return fract(p); }
float hash21(vec2 p) { vec3 p3 = fract(vec3(p.xyx) * 0.1031); p3 += dot(p3, p3.yzx + 33.33); return fract((p3.x + p3.y) * p3.z); }

// one depth layer of falling streaks: columns of drops, each column with its own speed, spacing and length
float rainLayer(vec2 frag, float cell, float speed, float len, float dens, float seed) {
  vec2 p = vec2(frag.x + frag.y * uSlant, frag.y);
  float cx = floor(p.x / cell);
  float fx = fract(p.x / cell);
  float h = hash11(cx * 1.37 + seed);
  float per = 170.0 * uPx + h * 260.0 * uPx;
  float y = p.y + uTime * speed * (0.75 + 0.5 * h) + h * per * 7.0;
  float row = floor(y / per);
  float cy = (y - row * per);
  float on = step(1.0 - dens, hash21(vec2(cx, row) + seed));
  float w = 1.8 * uPx / cell;
  float line = 1.0 - smoothstep(0.0, w, abs(fx - 0.5));
  float streak = cy < len ? pow(1.0 - cy / len, 1.4) : 0.0;
  return on * line * streak;
}

float snowLayer(vec2 frag, float cell, float speed, float seed, float dens, float sway) {
  vec2 p = frag / cell;
  p.y += uTime * speed / cell;
  p.x += sin(uTime * 0.6 + p.y * 1.3 + seed) * sway;
  vec2 id = floor(p), f = fract(p);
  float h = hash21(id + seed);
  if (h > dens) return 0.0;
  vec2 o = vec2(0.2 + 0.6 * hash21(id + 7.7), 0.2 + 0.6 * hash21(id + 3.3));
  float r = 0.08 + 0.12 * hash21(id + 9.1);
  return smoothstep(r, r * 0.35, length(f - o));
}

float segDist(vec2 p, vec2 a, vec2 b) {
  vec2 pa = p - a, ba = b - a;
  float h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-4), 0.0, 1.0);
  return length(pa - ba * h);
}

void main() {
  vec3 gD = mix(mix(uR00, uR10, vUv.x), mix(uR01, uR11, vUv.x), vUv.y);
  vec2 G = uCam.xy + gD.xy * (-uCam.z / gD.z);
  vec2 frag = gl_FragCoord.xy;

  vec4 sky = texture(uSky, vUv) * uSkyOp;
  vec4 f1 = textureLod(uF1, (G - uFieldMin) * uFieldInv, 0.0);
  float cloudF = 0.3 + 0.7 * clamp(sky.a * 1.5, 0.0, 1.0);          // precipitation falls from the clouds
  float cellN = textureLod(uNoise, (G - uDrift * 0.8) / 32e3, 0.0).b;
  float pulse = mix(1.0, 0.45 + 0.55 * abs(sin(uTime * 0.35 + cellN * 6.0)), f1.a);   // showers come in waves
  float rainL = clamp(f1.r * 1.25, 0.0, 1.0) * smoothstep(0.22, 0.62, cellN + f1.r * 0.55) * cloudF * pulse;
  float snowL = clamp(f1.g * 1.25, 0.0, 1.0) * smoothstep(0.2, 0.55, cellN + f1.g * 0.5) * cloudF * pulse;

  float tilt = 1.0 + uPitch * 0.7 * (0.5 - vUv.y);                     // finer streaks far away, bigger near the bottom
  float sc = uPx * tilt;
  float dz = uDrizzle;
  float r = 0.0;
  if (rainL > 0.01) {
    float d = rainL * (1.0 - 0.35 * dz);
    float ln = 1.0 - 0.5 * dz, sp = 1.0 - 0.4 * dz;
    r += rainLayer(frag, 20.0 * sc, 1500.0 * sc * sp, 95.0 * sc * ln, d * 0.55, 1.0);
    r += rainLayer(frag, 32.0 * sc, 2100.0 * sc * sp, 140.0 * sc * ln, d * 0.42, 5.0) * 1.15;
    if (uLite < 0.5) r += rainLayer(frag, 52.0 * sc, 2900.0 * sc * sp, 170.0 * sc * ln, d * 0.3, 9.0) * 1.4;
  }
  float sn = 0.0;
  if (snowL > 0.01) {
    sn += snowLayer(frag, 15.0 * sc, 70.0 * sc, 1.0, snowL * 0.5, 0.2);
    sn += snowLayer(frag, 26.0 * sc, 95.0 * sc, 4.0, snowL * 0.38, 0.28);
    if (uLite < 0.5) sn += snowLayer(frag, 44.0 * sc, 130.0 * sc, 8.0, snowL * 0.28, 0.35);
  }

  float bolt = 0.0;
  for (int i = 0; i < 16; i++) {
    if (i >= uBoltN) break;
    vec4 sg = uBolt[i];
    float d = segDist(frag, sg.xy, sg.zw);
    bolt += exp(-d * d / (2.0 * uPx * uPx * 1.4)) + 0.4 * exp(-d * d / (2.0 * uPx * uPx * 70.0));
  }

  float ra = clamp(r, 0.0, 1.0) * 0.8;
  float sa = clamp(sn, 0.0, 1.0) * 0.95;
  vec4 col = sky;
  col = vec4(uRainCol * ra, ra) + (1.0 - ra) * col;
  col = vec4(vec3(0.97, 0.99, 1.0) * sa, sa) + (1.0 - sa) * col;
  float glow = clamp(bolt * uBoltAmt, 0.0, 1.0) + uFlashAmt * 0.07 * clamp(f1.b * 2.0, 0.0, 1.0);
  col.rgb += vec3(0.72, 0.82, 1.0) * glow;
  col.a = max(col.a, glow * 0.8);
  col *= uFade;
  col.rgb += (hash21(frag) - 0.5) / 255.0 * col.a;
  oColor = col;
}`;

  // Draws the "ground" texture (shadows and fog) under the labels.
  const COMPOSITE = `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 oColor;
uniform sampler2D uTex;
void main() { oColor = texture(uTex, vUv); }`;

  // ---------------------------------------------------------------- small maths

  function invert4(a) {
    const o = new Float64Array(16);
    const a00 = a[0], a01 = a[1], a02 = a[2], a03 = a[3], a10 = a[4], a11 = a[5], a12 = a[6], a13 = a[7];
    const a20 = a[8], a21 = a[9], a22 = a[10], a23 = a[11], a30 = a[12], a31 = a[13], a32 = a[14], a33 = a[15];
    const b00 = a00 * a11 - a01 * a10, b01 = a00 * a12 - a02 * a10, b02 = a00 * a13 - a03 * a10, b03 = a01 * a12 - a02 * a11;
    const b04 = a01 * a13 - a03 * a11, b05 = a02 * a13 - a03 * a12, b06 = a20 * a31 - a21 * a30, b07 = a20 * a32 - a22 * a30;
    const b08 = a20 * a33 - a23 * a30, b09 = a21 * a32 - a22 * a31, b10 = a21 * a33 - a23 * a31, b11 = a22 * a33 - a23 * a32;
    let det = b00 * b11 - b01 * b10 + b02 * b09 + b03 * b08 - b04 * b07 + b05 * b06;
    if (!det) return null;
    det = 1 / det;
    o[0] = (a11 * b11 - a12 * b10 + a13 * b09) * det; o[1] = (a02 * b10 - a01 * b11 - a03 * b09) * det;
    o[2] = (a31 * b05 - a32 * b04 + a33 * b03) * det; o[3] = (a22 * b04 - a21 * b05 - a23 * b03) * det;
    o[4] = (a12 * b08 - a10 * b11 - a13 * b07) * det; o[5] = (a00 * b11 - a02 * b08 + a03 * b07) * det;
    o[6] = (a32 * b02 - a30 * b05 - a33 * b01) * det; o[7] = (a20 * b05 - a22 * b02 + a23 * b01) * det;
    o[8] = (a10 * b10 - a11 * b08 + a13 * b06) * det; o[9] = (a01 * b08 - a00 * b10 - a03 * b06) * det;
    o[10] = (a30 * b04 - a31 * b02 + a33 * b00) * det; o[11] = (a21 * b02 - a20 * b04 - a23 * b00) * det;
    o[12] = (a11 * b07 - a10 * b09 - a12 * b06) * det; o[13] = (a00 * b09 - a01 * b07 + a02 * b06) * det;
    o[14] = (a31 * b01 - a30 * b03 - a32 * b00) * det; o[15] = (a20 * b03 - a21 * b01 + a22 * b00) * det;
    return o;
  }
  const mulPoint = (m, x, y, z, w) => [0, 1, 2, 3].map((i) => m[i] * x + m[4 + i] * y + m[8 + i] * z + m[12 + i] * w);
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

  // The point closest to all the lines (origin o, direction d): where the camera is.
  function nearestPoint(lines) {
    const A = [0, 0, 0, 0, 0, 0, 0, 0, 0], B = [0, 0, 0];
    for (const { o, d } of lines) {
      const len = Math.hypot(d[0], d[1], d[2]);
      const u = [d[0] / len, d[1] / len, d[2] / len];
      for (let r = 0; r < 3; r++) {
        for (let c = 0; c < 3; c++) {
          const m = (r === c ? 1 : 0) - u[r] * u[c];
          A[r * 3 + c] += m;
          B[r] += m * o[c];
        }
      }
    }
    const det = A[0] * (A[4] * A[8] - A[5] * A[7]) - A[1] * (A[3] * A[8] - A[5] * A[6]) + A[2] * (A[3] * A[7] - A[4] * A[6]);
    const solve = (col) => {
      const M = A.slice();
      for (let r = 0; r < 3; r++) M[r * 3 + col] = B[r];
      return (M[0] * (M[4] * M[8] - M[5] * M[7]) - M[1] * (M[3] * M[8] - M[5] * M[6]) + M[2] * (M[3] * M[7] - M[4] * M[6])) / det;
    };
    return [solve(0), solve(1), solve(2)];
  }

  // ---------------------------------------------------------------- attach to a map

  function attach(map, options) {
    options = options || {};
    const GROUND = "rf-weather-ground", SKY = "rf-weather-sky";
    const field = WeatherField.createField();
    const noise = WeatherField.makeNoise(256);
    const mercOrigin = maplibregl.MercatorCoordinate.fromLngLat({ lng: WeatherField.ORIGIN.lon, lat: WeatherField.ORIGIN.lat }, 0);
    const mpu = mercOrigin.meterInMercatorCoordinateUnits();      // one metre in mercator units

    const E = {
      gl: null, ready: false, failed: false, enabled: true, fade: 0, fadeTarget: 1,
      progs: {}, vao: null, tex: {}, fbo: null, fboSize: [0, 0],
      scaleIdx: 2, lite: false, ema: 16, lastT: 0, lastAdapt: 0, slowCount: 0, cooldown: 0,
      time: 0, drift: [0, 0], flow: [1, 0], wind: [1, 0], flowTarget: [1, 0], windSpeed: 5,
      globals: null, drizzle: 0, drizzleT: 0, showery: 0, pixelRatio: 1,
      light: { lit: [1, 1, 1], shade: [0.55, 0.62, 0.74], fog: [0.86, 0.89, 0.94], rain: [0.16, 0.34, 0.78], day: 1, sun: [0, 0.5, 0.8], tanE: 0.8 },
      lightT: null, thunder: 0, nextBolt: 5, bolt: null, flash: { pos: [0, 0], amt: 0 },
      fieldMoving: false, fieldDirty: false,
    };

    // ---------------------------------------------------------- GL setup

    function compile(gl, type, src) {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    }
    function program(gl, frag) {
      const p = gl.createProgram();
      gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, VERT));
      gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, frag));
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
      const locs = {};
      return { p, u: (name) => (name in locs ? locs[name] : (locs[name] = gl.getUniformLocation(p, name))) };
    }
    function texture(gl, w, h, data, opts) {
      const t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, data || null);
      const filter = opts.nearest ? gl.NEAREST : gl.LINEAR;
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
      const wrap = opts.repeat ? gl.REPEAT : gl.CLAMP_TO_EDGE;
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, wrap);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, wrap);
      return t;
    }

    function init(gl) {
      if (E.ready || E.failed) return;
      try {
        if (typeof WebGL2RenderingContext === "undefined" || !(gl instanceof WebGL2RenderingContext)) throw new Error("WebGL2 is not available");
        E.gl = gl;
        E.progs.p1 = program(gl, PASS1);
        E.progs.p2 = program(gl, PASS2);
        E.progs.comp = program(gl, COMPOSITE);
        E.vao = gl.createVertexArray();
        gl.bindVertexArray(E.vao);
        const buf = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buf);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
        gl.enableVertexAttribArray(0);
        gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
        gl.bindVertexArray(null);
        E.buf = buf;
        E.tex.f0 = texture(gl, field.nx, field.ny, null, {});
        E.tex.f1 = texture(gl, field.nx, field.ny, null, {});
        E.tex.noise = texture(gl, 256, 256, noise, { repeat: true });
        E.ready = true;
        E.fieldDirty = true;
      } catch (err) {
        fail(err);
      }
    }

    function fail(err) {
      E.failed = true;
      console.warn("Weather layer unavailable:", err && err.message ? err.message : err);
      if (options.onFail) setTimeout(() => options.onFail(err), 0);
    }

    function ensureFbo(gl, w, h, prevFb) {
      if (E.fbo && E.fboSize[0] === w && E.fboSize[1] === h) return true;
      if (E.fbo) { gl.deleteFramebuffer(E.fbo); gl.deleteTexture(E.tex.ground); gl.deleteTexture(E.tex.sky); }
      E.tex.ground = texture(gl, w, h, null, {});
      E.tex.sky = texture(gl, w, h, null, {});
      E.fbo = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, E.fbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, E.tex.ground, 0);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT1, gl.TEXTURE_2D, E.tex.sky, 0);
      gl.drawBuffers([gl.COLOR_ATTACHMENT0, gl.COLOR_ATTACHMENT1]);
      const ok = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
      gl.bindFramebuffer(gl.FRAMEBUFFER, prevFb);   // leave the map's own framebuffer bound
      E.fboSize = [w, h];
      return ok;
    }

    // ---------------------------------------------------------- per frame: camera, light, lightning

    // Camera rays in ENU metres (east, north, up) from the map's matrix, worked out in doubles.
    function cameraRays(matrix) {
      const inv = invert4(matrix);
      if (!inv) return null;
      const toENU = (p) => [(p[0] - mercOrigin.x) / mpu, -(p[1] - mercOrigin.y) / mpu, p[2] / mpu];
      const unproj = (x, y, z) => { const p = mulPoint(inv, x, y, z, 1); return [p[0] / p[3], p[1] / p[3], p[2] / p[3]]; };
      const corners = [[-1, -1], [1, -1], [-1, 1], [1, 1]];
      const lines = corners.map(([x, y]) => {
        const n = toENU(unproj(x, y, -1)), f = toENU(unproj(x, y, 1));
        return { o: n, d: sub(f, n), far: f };
      });
      const cam = nearestPoint(lines);
      // depth normal: w of the clip coordinates grows along the viewing direction (expressed in ENU metres)
      const nrm = [matrix[3] * mpu, -matrix[7] * mpu, matrix[11] * mpu];
      const rays = lines.map((l) => { const d = sub(l.far, cam); const k = dot(nrm, d); return [d[0] / k, d[1] / k, d[2] / k]; });
      return { cam, rays };
    }

    function project(matrix, e, n, z, w, h) {
      const p = mulPoint(matrix, mercOrigin.x + e * mpu, mercOrigin.y - n * mpu, z * mpu, 1);
      if (p[3] <= 0) return null;
      return [(p[0] / p[3] * 0.5 + 0.5) * w, (p[1] / p[3] * 0.5 + 0.5) * h];
    }

    function updateLighting(dt) {
      const g = E.globals;
      if (!g) return;
      const pos = WeatherField.sunPosition(g.t || Date.now(), WeatherField.ORIGIN.lat, WeatherField.ORIGIN.lon);
      const day = clamp(g.daylight, 0, 1);
      const elev = pos.elevation;
      const warm = (1 - smooth(0.05, 0.5, elev)) * day * 0.6;
      const mixc = (a, b, k) => a.map((v, i) => lerp(v, b[i], k));
      const dayLit = mixc([0.98, 0.98, 1.0], [1.0, 0.8, 0.66], warm);
      const dayShade = mixc([0.56, 0.62, 0.75], [0.66, 0.54, 0.6], warm);
      const T = {
        lit: mixc([0.36, 0.42, 0.66], dayLit, day),
        shade: mixc([0.09, 0.11, 0.2], dayShade, day),
        fog: mixc([0.12, 0.16, 0.28], [0.86, 0.89, 0.94], day),
        rain: mixc([0.7, 0.82, 1.0], [0.16, 0.34, 0.78], day),
        day,
      };
      // the direction to the sun (at night a fixed, high direction: it only orients the cloud lighting)
      const el = elev > 0.05 ? elev : 0.6, az = elev > 0.05 ? pos.azimuth : 4.0;
      T.sun = [Math.sin(az) * Math.cos(el), Math.cos(az) * Math.cos(el), Math.sin(el)];
      T.tanE = Math.max(Math.tan(el), 0.18);
      const k = E.lightT ? 1 - Math.exp(-dt * 2.5) : 1;
      const L = E.light;
      for (const key of ["lit", "shade", "fog", "rain", "sun"]) L[key] = L[key].map((v, i) => lerp(v, T[key][i], k));
      L.day = lerp(L.day, T.day, k);
      L.tanE = lerp(L.tanE, T.tanE, k);
      E.lightT = T;

      // wind: the clouds drift towards where the wind blows (the time warp makes that visible)
      const dirRad = g.dir * rad;
      const spd = clamp(g.wind, 1.5, 18);
      E.flowTarget = [-Math.sin(dirRad), -Math.cos(dirRad)];
      E.windSpeed = lerp(E.windSpeed, spd, k);
      E.flow = [lerp(E.flow[0], E.flowTarget[0], k), lerp(E.flow[1], E.flowTarget[1], k)];
      const fl = Math.hypot(E.flow[0], E.flow[1]) || 1;
      E.wind = [E.flow[0] / fl, E.flow[1] / fl];
      E.drizzle = lerp(E.drizzle, g.drizzle ? 1 : 0, k);
      E.showery = lerp(E.showery, g.showery ? 1 : 0, k);
    }

    // Lightning inside a thundery area that is on screen.
    function updateLightning(dt, rays, matrix, w, h) {
      const f = E.flash;
      if (E.bolt) {
        E.bolt.t += dt;
        const t = E.bolt.t;
        f.amt = t < 0.08 ? 1 : t < 0.14 ? 0.2 : t < 0.22 ? 0.9 : Math.max(0, 1 - (t - 0.22) * 3.2);
        if (t > 0.6) { E.bolt = null; f.amt = 0; }
      }
      if (!E.thunder || E.bolt || !rays) return;
      E.nextBolt -= dt;
      if (E.nextBolt > 0) return;
      E.nextBolt = 3 + Math.random() * 6;
      // pick a spot on screen whose field says thunder
      for (let tries = 0; tries < 24; tries++) {
        const u = 0.1 + Math.random() * 0.8, v = 0.15 + Math.random() * 0.8;
        const d = [0, 1, 2].map((i) => lerp(lerp(rays.rays[0][i], rays.rays[1][i], u), lerp(rays.rays[2][i], rays.rays[3][i], u), v));
        const t = -rays.cam[2] / d[2];
        const x = rays.cam[0] + d[0] * t, y = rays.cam[1] + d[1] * t;
        if (field.sample(x, y, 6) < 0.3) continue;
        const top = project(matrix, x, y, E.alt[0] * 1.2, w, h), bottom = project(matrix, x, y, 0, w, h);
        if (!top || !bottom) continue;
        const segs = [];
        let prev = top;
        const n = 9, len = Math.hypot(bottom[0] - top[0], bottom[1] - top[1]);
        const nx = -(bottom[1] - top[1]) / (len || 1), ny = (bottom[0] - top[0]) / (len || 1);
        for (let i = 1; i <= n; i++) {
          const k = i / n, off = i === n ? 0 : (Math.random() - 0.5) * len * 0.16;
          const p = [lerp(top[0], bottom[0], k) + nx * off, lerp(top[1], bottom[1], k) + ny * off];
          segs.push([prev[0], prev[1], p[0], p[1]]);
          if (i === 4) {   // a short fork
            const fk = [p[0] + nx * len * 0.12 * (Math.random() < 0.5 ? -1 : 1), p[1] - len * 0.07];
            segs.push([p[0], p[1], fk[0], fk[1]]);
          }
          prev = p;
        }
        E.bolt = { t: 0, segs: segs.slice(0, 16) };
        f.pos = [x, y];
        f.amt = 1;
        return;
      }
    }

    // Choose the quality from the frame time: step the pass-1 resolution down when slow, up (carefully) when fast.
    function adapt(now) {
      if (E.lastT) {
        const dt = now - E.lastT;
        if (dt < 250) E.ema = lerp(E.ema, dt, 0.08);
      }
      E.lastT = now;
      if (now - E.lastAdapt < 600) return;
      E.lastAdapt = now;
      if (E.ema > 26 && E.scaleIdx > 0) { E.scaleIdx--; E.cooldown = now + 4000; E.slowCount = 0; }
      else if (E.ema > 38 && E.scaleIdx === 0) { if (++E.slowCount >= 3) E.lite = true; }
      else if (E.ema < 18.5 && E.scaleIdx < SCALES.length - 1 && now > E.cooldown) { E.scaleIdx++; E.cooldown = now + 6000; }
    }

    // ---------------------------------------------------------- drawing

    function bindTex(gl, unit, t) { gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, t); }

    function uploadField(gl) {
      const { a, b } = field.pack();
      for (const [t, data] of [[E.tex.f0, a], [E.tex.f1, b]]) {
        gl.bindTexture(gl.TEXTURE_2D, t);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
        gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
        gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, field.nx, field.ny, gl.RGBA, gl.UNSIGNED_BYTE, data);
      }
    }

    let frameData = null;     // what pass 1 worked out this frame, for the sky layer

    function renderGround(gl, matrix) {
      init(gl);
      frameData = null;
      if (!E.ready || E.failed) return;
      const now = performance.now();
      const dt = E.time ? Math.min(0.05, (now - E.prevNow) / 1000) : 0.016;
      E.prevNow = now;
      E.time += dt;
      E.fade = lerp(E.fade, E.fadeTarget, 1 - Math.exp(-dt * 4));
      if (E.fadeTarget === 0 && E.fade < 0.01) { E.fade = 0; setTimeout(hideLayers, 0); return; }
      if (!E.globals) { map.triggerRepaint(); return; }

      adapt(now);
      updateLighting(dt);
      E.drift[0] += E.flow[0] * E.windSpeed * TIME_WARP * dt;
      E.drift[1] += E.flow[1] * E.windSpeed * TIME_WARP * dt;
      if (field.step(dt) || E.fieldDirty) { uploadField(gl); E.fieldDirty = false; }

      const canvas = gl.canvas;
      const W = gl.drawingBufferWidth, H = gl.drawingBufferHeight;
      const scale = E.lite ? SCALES[0] : SCALES[E.scaleIdx];
      const fw = Math.max(2, Math.round(W * scale)), fh = Math.max(2, Math.round(H * scale));
      const rays = cameraRays(matrix);
      const prevFb = gl.getParameter(gl.FRAMEBUFFER_BINDING);
      if (!rays || !ensureFbo(gl, fw, fh, prevFb)) { map.triggerRepaint(); return; }

      // numbers the shaders need
      const centre = [0, 1, 2].map((i) => (rays.rays[0][i] + rays.rays[1][i] + rays.rays[2][i] + rays.rays[3][i]) / 4);
      const tc = -rays.cam[2] / centre[2];
      const dist = Math.hypot(centre[0] * tc, centre[1] * tc, rays.cam[2]);
      const mpp = dist * 2 * Math.tan(0.3217) / H;                      // metres per device pixel at the screen centre
      const camH = Math.max(rays.cam[2], 1000);
      E.alt = [camH * 0.015, camH * 0.035, camH * 0.07];
      E.mpp = mpp;
      E.rays = rays;
      E.pitch = clamp(map.getPitch() / 60, 0, 1);
      updateLightning(dt, rays, matrix, W, H);

      // ---- pass 1 into the framebuffer
      const vp = gl.getParameter(gl.VIEWPORT);
      gl.bindFramebuffer(gl.FRAMEBUFFER, E.fbo);
      gl.viewport(0, 0, fw, fh);
      gl.disable(gl.BLEND);
      gl.disable(gl.DEPTH_TEST);
      gl.disable(gl.STENCIL_TEST);
      gl.disable(gl.CULL_FACE);
      gl.colorMask(true, true, true, true);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      const P = E.progs.p1;
      gl.useProgram(P.p);
      bindTex(gl, 0, E.tex.f0); bindTex(gl, 1, E.tex.f1); bindTex(gl, 2, E.tex.noise);
      gl.uniform1i(P.u("uF0"), 0); gl.uniform1i(P.u("uF1"), 1); gl.uniform1i(P.u("uNoise"), 2);
      setRayUniforms(gl, P, rays);
      const L = E.light;
      gl.uniform2f(P.u("uFieldMin"), field.min[0], field.min[1]);
      gl.uniform2f(P.u("uFieldInv"), 1 / field.size[0], 1 / field.size[1]);
      gl.uniform2f(P.u("uDrift"), E.drift[0], E.drift[1]);
      gl.uniform2f(P.u("uWind"), E.wind[0], E.wind[1]);
      gl.uniform2f(P.u("uFlashPos"), E.flash.pos[0], E.flash.pos[1]);
      gl.uniform3f(P.u("uSunDir"), L.sun[0], L.sun[1], L.sun[2]);
      gl.uniform3f(P.u("uLit"), L.lit[0], L.lit[1], L.lit[2]);
      gl.uniform3f(P.u("uShade"), L.shade[0], L.shade[1], L.shade[2]);
      gl.uniform3f(P.u("uFogCol"), L.fog[0], L.fog[1], L.fog[2]);
      gl.uniform3f(P.u("uAlt"), E.alt[0], E.alt[1], E.alt[2]);
      gl.uniform1f(P.u("uDay"), L.day);
      gl.uniform1f(P.u("uMpp"), mpp);
      gl.uniform1f(P.u("uPitch"), E.pitch);
      gl.uniform1f(P.u("uLite"), E.lite ? 1 : 0);
      gl.uniform1f(P.u("uFade"), E.fade);
      gl.uniform1f(P.u("uFlashAmt"), E.flash.amt);
      gl.uniform1f(P.u("uTanE"), L.tanE);
      gl.bindVertexArray(E.vao);
      gl.drawBuffers([gl.COLOR_ATTACHMENT0, gl.COLOR_ATTACHMENT1]);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

      // ---- back to the map, and paint the ground layer (shadows + fog)
      gl.bindFramebuffer(gl.FRAMEBUFFER, prevFb);
      gl.viewport(vp[0], vp[1], vp[2], vp[3]);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      const C = E.progs.comp;
      gl.useProgram(C.p);
      bindTex(gl, 4, E.tex.ground);
      gl.uniform1i(C.u("uTex"), 4);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.bindVertexArray(null);
      gl.activeTexture(gl.TEXTURE0);
      frameData = { matrix, W, H };
    }

    function setRayUniforms(gl, P, rays) {
      gl.uniform3f(P.u("uCam"), rays.cam[0], rays.cam[1], rays.cam[2]);
      ["uR00", "uR10", "uR01", "uR11"].forEach((name, i) => gl.uniform3f(P.u(name), rays.rays[i][0], rays.rays[i][1], rays.rays[i][2]));
    }

    function renderSky(gl) {
      if (!E.ready || E.failed || !frameData) return;
      const rays = E.rays;
      const P = E.progs.p2;
      gl.useProgram(P.p);
      bindTex(gl, 3, E.tex.sky); bindTex(gl, 1, E.tex.f1); bindTex(gl, 2, E.tex.noise);
      gl.uniform1i(P.u("uSky"), 3); gl.uniform1i(P.u("uF1"), 1); gl.uniform1i(P.u("uNoise"), 2);
      setRayUniforms(gl, P, rays);
      gl.uniform2f(P.u("uFieldMin"), field.min[0], field.min[1]);
      gl.uniform2f(P.u("uFieldInv"), 1 / field.size[0], 1 / field.size[1]);
      gl.uniform2f(P.u("uDrift"), E.drift[0], E.drift[1]);
      gl.uniform3f(P.u("uRainCol"), E.light.rain[0], E.light.rain[1], E.light.rain[2]);
      const px = Math.max(1, gl.drawingBufferWidth / (gl.canvas.clientWidth || gl.drawingBufferWidth));
      gl.uniform1f(P.u("uTime"), E.time);
      gl.uniform1f(P.u("uFade"), E.fade);
      gl.uniform1f(P.u("uSkyOp"), 0.88);
      gl.uniform1f(P.u("uDrizzle"), E.drizzle);
      gl.uniform1f(P.u("uPx"), px);
      gl.uniform1f(P.u("uLite"), E.lite ? 1 : 0);
      gl.uniform1f(P.u("uPitch"), E.pitch);
      // slant of the rain on the screen: the wind's sideways part, turned by the map's bearing
      const b = map.getBearing() * rad;
      const sideways = E.flow[0] * Math.cos(b) - E.flow[1] * Math.sin(b);
      gl.uniform1f(P.u("uSlant"), 0.07 + sideways * clamp(E.windSpeed / 14, 0, 1) * 0.6);
      gl.uniform1f(P.u("uFlashAmt"), E.flash.amt);
      gl.uniform1f(P.u("uShowery"), E.showery);
      const segs = E.bolt ? E.bolt.segs : [];
      const arr = new Float32Array(64);
      segs.forEach((s, i) => arr.set(s, i * 4));
      gl.uniform4fv(P.u("uBolt"), arr);
      gl.uniform1i(P.u("uBoltN"), segs.length);
      gl.uniform1f(P.u("uBoltAmt"), E.bolt ? E.flash.amt : 0);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.bindVertexArray(E.vao);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.bindVertexArray(null);
      gl.activeTexture(gl.TEXTURE0);
      if (E.fadeTarget > 0 || E.fade > 0.01) map.triggerRepaint();   // the animation never rests while it is on
    }

    function hideLayers() {
      for (const id of [GROUND, SKY]) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", "none");
    }
    function showLayers() {
      for (const id of [GROUND, SKY]) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", "visible");
    }

    const layerBase = { type: "custom", renderingMode: "2d" };
    map.addLayer(Object.assign({}, layerBase, {
      id: GROUND,
      onAdd(m, gl) { init(gl); },
      render(gl, matrix) { renderGround(gl, matrix); },
      onRemove(m, gl) { dispose(gl); },
    }), options.before);
    map.addLayer(Object.assign({}, layerBase, {
      id: SKY,
      onAdd(m, gl) { init(gl); },
      render(gl) { renderSky(gl); },
    }));

    function dispose(gl) {
      if (!E.ready) return;
      for (const t of Object.values(E.tex)) gl.deleteTexture(t);
      if (E.fbo) gl.deleteFramebuffer(E.fbo);
      if (E.vao) gl.deleteVertexArray(E.vao);
      if (E.buf) gl.deleteBuffer(E.buf);
      for (const p of Object.values(E.progs)) gl.deleteProgram(p.p);
      E.ready = false;
    }

    if (E.failed) {
      for (const id of [GROUND, SKY]) if (map.getLayer(id)) map.removeLayer(id);
      return null;
    }

    return {
      // stations: [{ lon, lat, scene }], shown: the Scene that sets the light, wind and time
      setScene(stations, shown) {
        field.set(stations.map((s) => ({ lon: s.lon, lat: s.lat, v: WeatherField.channels(s.scene) })));
        E.globals = shown;
        E.thunder = stations.some((s) => s.scene.thunder) ? 1 : 0;
        E.fieldDirty = true;
        map.triggerRepaint();
      },
      setEnabled(on) {
        E.enabled = on;
        E.fadeTarget = on ? 1 : 0;
        if (on) { showLayers(); map.triggerRepaint(); }
      },
      info: () => ({ scale: E.lite ? SCALES[0] : SCALES[E.scaleIdx], lite: E.lite, ms: Math.round(E.ema * 10) / 10, failed: E.failed }),
      destroy() { for (const id of [GROUND, SKY]) if (map.getLayer(id)) map.removeLayer(id); },
    };
  }

  return { attach };
})();
