// Flow-path particle animation: ONE particle travels each displayed hyporheic flow path
// (comet streak by default, plain dot as the alternate style), and each particle's loop
// period is proportional to that path's residence time (feature properties.total_time_d,
// already shipped on every hz_paths_* feature) — so a 1-day path visibly cycles twice as
// fast as a 2-day path.
//
// Fully client-side: geometry and residence times are read straight off the live Leaflet
// layers, so the server's only involvement is the tiny "hype_fp_anim" settings message
// ({on, speed, color, style, mode, lmode, lw, lop}) sent from the Flow paths pane
// (_send_fp_anim). mode "total"/"elapsed" swaps the solid color for a turbo rainbow over
// residence time (with an on-canvas legend); "solid" is the classic swatch color. lmode
// is the LINE color mode: "total" recolors the Leaflet strokes themselves (server-baked
// per-feature colors; this canvas carries only the legend), while "elapsed" ALSO paints
// the true along-path gradient here — an offscreen line cache (buildLineCache) blitted
// over the baked total-color strokes, so zoom animations degrade to the total colors
// instead of blanking. lw/lop are the line stroke style the cache needs. The hz layers
// are parked/cloned/swept/healed constantly, so layer handles are NEVER cached across
// turns — any layeradd/layerremove triggers a debounced rescan instead.
(function () {
  "use strict";

  var S = {
    on: false, speed: 3, msPerDay: 0, color: "#ff2bd6", style: "comet",
    // mode: "solid" paints every particle S.color; "total" fixes each particle at its
    // path's total-residence-time rainbow color; "elapsed" shifts the color with time
    // in transit (so it lands on its total-time color exactly as it exits).
    mode: "solid",
    lmode: "class",                   // LINE color mode (legend shows for its rainbows too)
    lw: 1.5, lop: 0.9,                // line stroke style (the elapsed gradient overlay)
    tMin: 1, tMax: 1, t0: 0, t1: 0,   // displayed residence-time range (days + log10)
    map: null, canvas: null, ctx: null,
    lineCv: null, lineRef: null,      // offscreen elapsed-line cache + its anchor point
    lineDirty: true, moveRaf: 0,
    raf: 0, hidden: false, hiddenAt: 0, scanTimer: 0, waitTimer: 0, pulse: 0,
    lastTick: 0, paths: [], w: 0, h: 0, dpr: 1
  };

  // Margin around the viewport baked into the line cache, so ordinary pans blit
  // already-rendered pixels instead of showing bare edges until the settle rebuild.
  var LINE_PAD = 256;

  // ---- residence-time rainbow ------------------------------------------------------------
  // Turbo (Google's perceptually even rainbow), via its published polynomial fit: 256
  // precomputed rgb() strings so per-frame color lookups allocate nothing. Index 0 =
  // dark blue (quick), 255 = dark red (slow).
  var TURBO = (function () {
    function ch(v) { v = Math.round(v); return v < 0 ? 0 : v > 255 ? 255 : v; }
    var lut = [];
    for (var i = 0; i < 256; i++) {
      var t = i / 255;
      var r = ch(34.61 + t * (1172.33 + t * (-10793.56 + t * (33300.12
                + t * (-38394.49 + t * 14825.05)))));
      var g = ch(23.31 + t * (557.33 + t * (1225.33 + t * (-3574.96
                + t * (1073.77 + t * 707.56)))));
      var b = ch(27.2 + t * (3211.1 + t * (-15327.97 + t * (27814.0
                + t * (-22569.18 + t * 6838.66)))));
      lut.push("rgb(" + r + "," + g + "," + b + ")");
    }
    return lut;
  })();

  function tIdx(days) {
    // log10 mapping over the DISPLAYED range (residence times span decades, so a linear
    // axis would pile every path onto one end). Values at or below the range floor clamp
    // to the coolest color: a particle stays "young" until it outlives the quickest
    // displayed path, and an elapsed-mode particle converges on its total-time color.
    if (!(days > 0)) return 0;
    var f = (Math.log10(days) - S.t0) / ((S.t1 - S.t0) || 1);
    return f <= 0 ? 0 : f >= 1 ? 255 : Math.round(f * 255);
  }

  function fmtDays(v) {
    if (v >= 100) return String(Math.round(v));
    if (v >= 10) return String(Math.round(v * 10) / 10);
    return String(Math.round(v * 100) / 100);
  }

  // ---- path cache ------------------------------------------------------------------------
  function scan() {
    var m = S.map;
    if (!m || !window.L) return;
    var out = [];
    m.eachLayer(function (g) {
      // hz_paths_* layers are L.GeoJSON groups whose children all carry the same hz_lyr
      // tag (map_bounds.js tagOf doctrine); the selection highlight (hz_paths_sel) and
      // every non-path group bail on their first child.
      if (!(g instanceof window.L.GeoJSON) || !g.getLayers) return;
      var kids = g.getLayers();
      // Group eligibility comes from the first TAGGED kid anywhere (map_bounds.js
      // tagOf doctrine): one untagged clone mid-park must never discard the whole
      // class group, and non-path groups still bail cheaply.
      var gtag = null;
      for (var i0 = 0; i0 < kids.length; i0++) {
        var f0 = kids[i0] && kids[i0].feature;
        var t0 = f0 && f0.properties && f0.properties.hz_lyr;
        if (t0) { gtag = t0; break; }
      }
      if (!gtag || gtag.indexOf("hz_paths_") !== 0 || gtag === "hz_paths_sel") return;
      for (var i = 0; i < kids.length; i++) {
        var f = kids[i] && kids[i].feature;
        var pr = f && f.properties;
        var td = pr && +pr.total_time_d;
        if (!(td > 0) || !kids[i].getLatLngs) continue;
        var lls = kids[i].getLatLngs();
        if (lls && lls.length && Array.isArray(lls[0])) lls = lls[0];   // defensive
        if (!lls || lls.length < 2) continue;
        var cum = [0], tot = 0;
        for (var j = 1; j < lls.length; j++) {
          tot += lls[j - 1].distanceTo(lls[j]);
          cum.push(tot);
        }
        if (!(tot > 0)) continue;
        var pid = +(pr.particleid) || 0;
        out.push({
          lls: lls, cum: cum, total: tot, td: td, dur: 0,   // dur filled by retime()
          // Deterministic per-particle phase (golden-ratio hash of the stable particleid):
          // rescans and layer toggles never make a particle jump along its path.
          phase: (pid * 0.6180339887) % 1
        });
      }
    });
    S.paths = out;
    retime();
  }

  function retime() {
    // Residence times are log-distributed and span 0.1 to 1000+ days between sites, so a
    // fixed seconds-per-day is either a strobe or a glacier. Anchor the MEDIAN displayed
    // path to a 36/speed-second loop (12 s at the default speed 3) and scale every other
    // path linearly with its residence time — relative speeds stay exactly proportional
    // (a 1-day path cycles twice as fast as a 2-day path), while the absolute pace adapts
    // to whatever site is on screen. 800 ms floor: the quickest paths must travel, not blink.
    var tds = [];
    for (var i = 0; i < S.paths.length; i++) tds.push(S.paths[i].td);
    tds.sort(function (a, b) { return a - b; });
    var med = tds.length ? tds[(tds.length - 1) >> 1] : 1;
    S.msPerDay = (36000 / Math.max(S.speed, 0.1)) / (med || 1);
    // Rainbow scale range = the displayed population's min/max residence time (a
    // degenerate one-value range spreads half a decade each way so the legend still
    // reads). Per-path total-time colors are frozen here; elapsed colors interpolate
    // per frame against the same scale.
    var lo = tds.length ? tds[0] : 1;
    var hi = tds.length ? tds[tds.length - 1] : 1;
    if (!(lo > 0)) lo = 0.001;
    if (!(hi > lo)) { hi = lo * Math.sqrt(10); lo = lo / Math.sqrt(10); }
    S.tMin = lo; S.tMax = hi;
    S.t0 = Math.log10(lo); S.t1 = Math.log10(hi);
    for (var j = 0; j < S.paths.length; j++) {
      S.paths[j].dur = Math.max(S.paths[j].td * S.msPerDay, 800);
      S.paths[j].ci = tIdx(S.paths[j].td);
    }
    S.lineDirty = true;               // geometry, range, or style moved under the cache
    if (!S.on && lineRainbow()) staticDraw();   // fresh range -> fresh static frame
  }

  function lineRainbow() {
    return S.lmode === "total" || S.lmode === "elapsed";
  }

  function lineElapsed() {
    // Only elapsed needs canvas-drawn lines (total lives in the Leaflet strokes), and
    // a zeroed opacity means "Show path lines" is off — nothing to paint then.
    return S.lmode === "elapsed" && S.lop > 0;
  }

  function buildLineCache() {
    // Offscreen render of the elapsed-gradient lines in container coordinates (+ margin):
    // frames and pans then BLIT one bitmap instead of restroking thousands of segments.
    // Per-segment color matches the capture renderer (video._draw_elapsed_lines): elapsed
    // at the segment midpoint = td * midArc/total, on the shared log scale. Consecutive
    // same-color segments merge into one stroke to keep canvas state flips rare.
    S.lineCv = null;
    S.lineRef = null;
    if (!S.map || !lineElapsed() || !S.paths.length) return;
    var w = S.w + 2 * LINE_PAD, h = S.h + 2 * LINE_PAD;
    if (!(S.w > 0 && S.h > 0)) return;
    var center, ref;
    try {
      center = S.map.getCenter();
      ref = S.map.latLngToContainerPoint(center);
    } catch (e) { return; }
    var cv = document.createElement("canvas");
    cv.width = Math.round(w * S.dpr);
    cv.height = Math.round(h * S.dpr);
    var c = cv.getContext("2d");
    c.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
    c.lineCap = "round";
    c.lineJoin = "round";
    c.lineWidth = S.lw + 0.75;        // fully covers the total-color Leaflet stroke beneath
    c.globalAlpha = S.lop;
    for (var i = 0; i < S.paths.length; i++) {
      var p = S.paths[i];
      var pts = [], ok = true;
      var minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
      for (var j = 0; j < p.lls.length; j++) {
        var q;
        try { q = S.map.latLngToContainerPoint(p.lls[j]); } catch (e) { ok = false; break; }
        pts.push(q);
        if (q.x < minX) minX = q.x;
        if (q.y < minY) minY = q.y;
        if (q.x > maxX) maxX = q.x;
        if (q.y > maxY) maxY = q.y;
      }
      if (!ok || maxX < -LINE_PAD || maxY < -LINE_PAD
          || minX > S.w + LINE_PAD || minY > S.h + LINE_PAD) continue;
      var cur = null;
      for (var k = 0; k < pts.length - 1; k++) {
        var frac = (p.cum[k] + p.cum[k + 1]) / (2 * p.total);
        var col = TURBO[tIdx(p.td * frac)];
        if (col !== cur) {
          if (cur !== null) c.stroke();
          c.beginPath();
          c.moveTo(pts[k].x + LINE_PAD, pts[k].y + LINE_PAD);
          c.strokeStyle = col;
          cur = col;
        }
        c.lineTo(pts[k + 1].x + LINE_PAD, pts[k + 1].y + LINE_PAD);
      }
      if (cur !== null) c.stroke();
    }
    S.lineCv = cv;
    S.lineRef = { lat: center.lat, lng: center.lng, x: ref.x, y: ref.y,
                  zoom: S.map.getZoom() };
    S.lineDirty = false;
  }

  function blitLines(ctx) {
    // Blit the cached line layer at the current pan offset (exact for pans; a zoom, a
    // data change, or a style change rebuilds first).
    if (!lineElapsed()) return;
    var zoom;
    try { zoom = S.map.getZoom(); } catch (e) { return; }
    if (S.lineDirty || !S.lineCv || !S.lineRef || S.lineRef.zoom !== zoom) buildLineCache();
    if (!S.lineCv || !S.lineRef) return;
    var cur;
    try { cur = S.map.latLngToContainerPoint([S.lineRef.lat, S.lineRef.lng]); }
    catch (e) { return; }
    ctx.drawImage(S.lineCv, 0, 0, S.lineCv.width, S.lineCv.height,
                  cur.x - S.lineRef.x - LINE_PAD, cur.y - S.lineRef.y - LINE_PAD,
                  S.lineCv.width / S.dpr, S.lineCv.height / S.dpr);
  }

  function legendLabel() {
    // Shared title rule: one rainbow meaning active gets its specific title; lines and
    // particles active with DIFFERENT meanings share the scale, so the generic title.
    var kinds = {};
    if (S.on && (S.mode === "total" || S.mode === "elapsed")) kinds[S.mode] = 1;
    if (lineRainbow()) kinds[S.lmode] = 1;
    var ks = Object.keys(kinds);
    if (ks.length === 1) {
      return ks[0] === "total" ? "Total residence time (days)"
                               : "Elapsed residence time (days)";
    }
    return "Residence time (days)";
  }

  function staticDraw() {
    // Lines-rainbow with the animation off: elapsed blits the gradient line cache over
    // the baked total-color strokes; total leaves the recoloring to the Leaflet layers
    // themselves. Either way the canvas carries the legend.
    if (!S.map || !S.ctx) return;
    resize();
    S.ctx.clearRect(0, 0, S.w, S.h);
    if (S.hidden || !S.paths.length) return;
    if (lineElapsed()) blitLines(S.ctx);
    if (lineRainbow()) drawLegend(S.ctx);
  }

  function schedScan() {
    if (!S.on && !lineRainbow()) return;   // legend-only mode still tracks layer churn
    clearTimeout(S.scanTimer);
    S.scanTimer = setTimeout(scan, 250);
  }

  // ---- map + canvas binding --------------------------------------------------------------
  function onZoomStart() {
    // Container-point math is unreliable while a zoom is in flight (CSS-scaled panes,
    // flyTo re-projection), and guardVectors may be hiding the path lines anyway — hide
    // the particles for the duration and pop them back at settle. The timestamp lets
    // the watchdog un-latch an interrupted zoom that never fires a settle event.
    S.hidden = true;
    S.hiddenAt = performance.now();
    if (S.canvas) S.canvas.style.visibility = "hidden";
  }

  function onSettle() {
    S.hidden = false;
    if (S.canvas) S.canvas.style.visibility = "";
    if (!S.on) staticDraw();               // static line/legend frame after a view move
  }

  function onMove() {
    // Continuous pan tracking for the static elapsed lines (the anim loop covers it
    // while running): rAF-throttled blit-only redraws, so a drag never smears the
    // gradient against the moving basemap.
    if (S.on || !lineElapsed() || S.hidden || S.moveRaf) return;
    S.moveRaf = requestAnimationFrame(function () {
      S.moveRaf = 0;
      if (!S.on && lineElapsed() && !S.hidden) staticDraw();
    });
  }

  function bind(m) {
    if (S.map === m) return;
    // Resolve the container FIRST: if it throws (mid-teardown map), nothing has been
    // torn down yet, S.map still points at the old binding, and the next frame's
    // rebind check simply retries.
    var cont = m.getContainer();
    if (!cont) return;
    if (S.map) {
      try { S.map.off("layeradd layerremove", schedScan); } catch (e) { /* gone */ }
      try { S.map.off("zoomstart zoomanim", onZoomStart); } catch (e) { /* gone */ }
      try { S.map.off("zoomend moveend viewreset", onSettle); } catch (e) { /* gone */ }
      try { S.map.off("move", onMove); } catch (e) { /* gone */ }
    }
    if (S.canvas && S.canvas.parentNode) S.canvas.parentNode.removeChild(S.canvas);
    if (!S.canvas) {
      S.canvas = document.createElement("canvas");
      S.canvas.className = "hype-fp-anim";
      S.ctx = S.canvas.getContext("2d");
    }
    cont.appendChild(S.canvas);
    S.w = S.h = 0;                          // force a resize on the next frame
    m.on("layeradd layerremove", schedScan);
    m.on("zoomstart zoomanim", onZoomStart);
    m.on("zoomend moveend viewreset", onSettle);
    m.on("move", onMove);
    S.map = m;                              // LAST: only a fully wired map is current
    S.hidden = false;                       // a fresh map never inherits the zoom latch
    S.hiddenAt = 0;
    if (S.canvas) S.canvas.style.visibility = "";
    scan();
  }

  function resize() {
    // The container's clientWidth/Height are 0 in this widget layout (map_bounds.js:17-26),
    // so the canvas is sized from the rendered box, not from CSS inset.
    var r = S.map.getContainer().getBoundingClientRect();
    var w = r.width, h = r.height;
    if (!(w > 0 && h > 0)) {                // hidden/backgrounded pane
      w = window.innerWidth || 1000;
      h = window.innerHeight || 700;
    }
    var dpr = window.devicePixelRatio || 1;
    if (w === S.w && h === S.h && dpr === S.dpr) return;
    S.w = w; S.h = h; S.dpr = dpr;
    S.lineDirty = true;               // the cache is sized from these dims
    S.canvas.width = Math.round(w * dpr);
    S.canvas.height = Math.round(h * dpr);
    S.canvas.style.width = w + "px";
    S.canvas.style.height = h + "px";
    S.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // ---- draw loop -------------------------------------------------------------------------
  function frame(now) {
    S.raf = S.on ? requestAnimationFrame(frame) : 0;
    if (!S.on) return;
    S.lastTick = performance.now();   // heartbeat: proves the loop is actually running
    var live = window.__hypeMap;
    if (live && live !== S.map) bind(live);  // map widget was rebuilt under us
    if (!S.map || !S.ctx) return;
    resize();
    var ctx = S.ctx;
    ctx.clearRect(0, 0, S.w, S.h);
    if (S.hidden || !S.paths.length) return;
    if (lineElapsed()) blitLines(ctx);   // gradient lines under the particles, one blit
    var rainbow = S.mode === "total" || S.mode === "elapsed";
    var items = [];
    for (var i = 0; i < S.paths.length; i++) {
      var p = S.paths[i];
      var fr = ((now / p.dur) + p.phase) % 1;
      var s = fr * p.total;
      var lo = 0, hi = p.cum.length - 1;
      while (hi - lo > 1) {
        var mid = (lo + hi) >> 1;
        if (p.cum[mid] <= s) lo = mid; else hi = mid;
      }
      var t = (s - p.cum[lo]) / ((p.cum[lo + 1] - p.cum[lo]) || 1);
      var a = p.lls[lo], b = p.lls[lo + 1];
      var pt;
      try {
        pt = S.map.latLngToContainerPoint([a.lat + (b.lat - a.lat) * t,
                                           a.lng + (b.lng - a.lng) * t]);
      } catch (e) { continue; }
      if (pt.x < -60 || pt.y < -60 || pt.x > S.w + 60 || pt.y > S.h + 60) continue;
      // LUT strings are shared references, so identical colors compare with === and
      // the draw passes only touch canvas state when the color actually changes.
      var col = !rainbow ? S.color
        : (S.mode === "total" ? TURBO[p.ci] : TURBO[tIdx(fr * p.td)]);
      items.push({ p: p, s: s, lo: lo, x: pt.x, y: pt.y, col: col });
    }
    if (S.style === "dots") drawDots(ctx, items);
    else drawComets(ctx, items);
    if (rainbow || lineRainbow()) drawLegend(ctx);
  }

  function drawDots(ctx, items) {
    // Two passes (all glows, then all rings) keep canvas state flips off the hot path;
    // in the rainbow modes styles are reassigned only when the item color changes.
    ctx.shadowBlur = 4;
    var cur = null;
    for (var k = 0; k < items.length; k++) {
      if (items[k].col !== cur) {
        cur = items[k].col;
        ctx.shadowColor = cur;
        ctx.fillStyle = cur;
      }
      ctx.beginPath(); ctx.arc(items[k].x, items[k].y, 2, 0, 6.2832); ctx.fill();
    }
    ctx.shadowBlur = 0;
    // contrast ring on any basemap; rainbow colors are never white
    ctx.strokeStyle = (S.mode === "solid" && S.color.toLowerCase() === "#ffffff")
      ? "rgba(0,0,0,.55)" : "rgba(255,255,255,.85)";
    ctx.lineWidth = 0.75;
    for (var k2 = 0; k2 < items.length; k2++) {
      ctx.beginPath(); ctx.arc(items[k2].x, items[k2].y, 2, 0, 6.2832); ctx.stroke();
    }
  }

  function strokeFrom(ctx, pts, fromArc) {
    // Stroke the comet from the given arc position to the head. pts is tail→head with
    // per-point arc values; the start lands exactly at fromArc via interpolation, so the
    // stacked strokes taper smoothly even when a tail spans a single long segment.
    var i = 0;
    while (i < pts.length - 1 && pts[i + 1].a <= fromArc) i++;
    var A = pts[i], B = pts[i + 1] || A;
    var t = Math.min(Math.max((fromArc - A.a) / ((B.a - A.a) || 1), 0), 1);
    ctx.beginPath();
    ctx.moveTo(A.x + (B.x - A.x) * t, A.y + (B.y - A.y) * t);
    for (var j = i + 1; j < pts.length; j++) ctx.lineTo(pts[j].x, pts[j].y);
    ctx.stroke();
  }

  function drawComets(ctx, items) {
    // RAS2025-style streaks, one comet per path: thin stacked strokes fading toward the
    // tail (no shadowBlur — hundreds of blurred strokes would tank the frame rate), plus
    // a small bright head so a single parcel stays trackable.
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.shadowBlur = 0;
    // bright core so a single parcel stays trackable; rainbow colors are never white
    var core = (S.mode === "solid" && S.color.toLowerCase() === "#ffffff")
      ? "rgba(0,0,0,.5)" : "rgba(255,255,255,.9)";
    var cur = null;
    for (var i = 0; i < items.length; i++) {
      var it = items[i], p = it.p;
      if (it.col !== cur) { cur = it.col; ctx.strokeStyle = cur; }
      // Tail = the arc covered in the last second of wall clock, so tail length reads as
      // speed (fast shallow paths streak, slow deep ones crawl); capped at a quarter of
      // the path, and clamped at the start so each loop begins with a release pulse.
      var tailLen = Math.min(p.total / p.dur * 1000, p.total * 0.25);
      var ta = Math.max(it.s - tailLen, 0);
      if (it.s - ta > 1e-6) {
        var ti = it.lo;
        while (ti > 0 && p.cum[ti] > ta) ti--;
        var pts = null;
        try {
          var t0 = (ta - p.cum[ti]) / ((p.cum[ti + 1] - p.cum[ti]) || 1);
          var a0 = p.lls[ti], b0 = p.lls[ti + 1];
          var q = S.map.latLngToContainerPoint(
            [a0.lat + (b0.lat - a0.lat) * t0, a0.lng + (b0.lng - a0.lng) * t0]);
          pts = [{ x: q.x, y: q.y, a: ta }];
          for (var v = ti + 1; v <= it.lo; v++) {
            q = S.map.latLngToContainerPoint(p.lls[v]);
            pts.push({ x: q.x, y: q.y, a: p.cum[v] });
          }
          pts.push({ x: it.x, y: it.y, a: it.s });
        } catch (e) { pts = null; }
        if (pts) {
          var span = it.s - ta;
          ctx.globalAlpha = 0.12; ctx.lineWidth = 1.0; strokeFrom(ctx, pts, ta);
          ctx.globalAlpha = 0.2;  ctx.lineWidth = 1.4; strokeFrom(ctx, pts, ta + span / 3);
          ctx.globalAlpha = 0.3;  ctx.lineWidth = 1.9; strokeFrom(ctx, pts, ta + 2 * span / 3);
        }
      }
      ctx.globalAlpha = 1;
      ctx.fillStyle = it.col;
      ctx.beginPath(); ctx.arc(it.x, it.y, 2, 0, 6.2832); ctx.fill();
      ctx.fillStyle = core;
      ctx.beginPath(); ctx.arc(it.x, it.y, 0.8, 0, 6.2832); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  // ---- residence-time legend -------------------------------------------------------------
  function drawLegend(ctx) {
    // Bottom-left of the view, redrawn each frame on the same canvas (the frame clears
    // it anyway): a turbo bar over the displayed range with decade ticks, so the rainbow
    // is readable as actual days. Titles name the quantity, the bar names the scale.
    var barW = 190, barH = 10, pad = 10;
    var panelW = barW + 2 * pad;
    var panelH = 52;
    var px = 10, py = S.h - 10 - panelH;
    var bx = px + pad, by = py + 24;
    ctx.save();
    ctx.globalAlpha = 1;
    ctx.fillStyle = "rgba(255,255,255,.85)";
    if (ctx.roundRect) {
      ctx.beginPath(); ctx.roundRect(px, py, panelW, panelH, 6); ctx.fill();
    } else {
      ctx.fillRect(px, py, panelW, panelH);
    }
    ctx.fillStyle = "#1a2733";
    ctx.font = "600 11px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    ctx.fillText(legendLabel(), bx, py + 16);
    var grad = ctx.createLinearGradient(bx, 0, bx + barW, 0);
    for (var i = 0; i <= 12; i++) grad.addColorStop(i / 12, TURBO[Math.round(i / 12 * 255)]);
    ctx.fillStyle = grad;
    ctx.fillRect(bx, by, barW, barH);
    // decade tick marks strictly inside the range, labels skipped near the endpoints
    ctx.font = "10px system-ui, sans-serif";
    ctx.strokeStyle = "rgba(26,39,51,.7)";
    ctx.lineWidth = 1;
    for (var d = Math.ceil(S.t0 + 1e-9); d <= Math.floor(S.t1 - 1e-9); d++) {
      var fx = bx + (d - S.t0) / ((S.t1 - S.t0) || 1) * barW;
      ctx.beginPath(); ctx.moveTo(fx, by + barH); ctx.lineTo(fx, by + barH + 3); ctx.stroke();
      if (fx - bx > 24 && bx + barW - fx > 24) {
        ctx.textAlign = "center";
        ctx.fillStyle = "#1a2733";
        ctx.fillText(fmtDays(Math.pow(10, d)), fx, by + barH + 13);
      }
    }
    ctx.fillStyle = "#1a2733";
    ctx.textAlign = "left";
    ctx.fillText(fmtDays(S.tMin), bx, by + barH + 13);
    ctx.textAlign = "right";
    ctx.fillText(fmtDays(S.tMax), bx + barW, by + barH + 13);
    ctx.restore();
  }

  // ---- settings message ------------------------------------------------------------------
  function pulse() {
    // Self-healing watchdog while the animation is on: whatever latched or starved
    // the loop (an interrupted zoom with no settle event, an empty rescan that hit
    // mid-layer-swap, a broken rAF chain), it recovers within ~2 s instead of
    // showing static lines until the user fiddles. Self-clears when off (the
    // waitTimer idiom).
    if (S.pulse) return;
    S.pulse = setInterval(function () {
      if (!S.on) { clearInterval(S.pulse); S.pulse = 0; return; }
      if (S.hidden && performance.now() - S.hiddenAt > 2000) {
        var az = false;
        try { az = !!(S.map && S.map._animatingZoom); } catch (e) { /* fine */ }
        if (!az) onSettle();                 // zoom never settled: un-latch
      }
      if (!S.paths.length) scan();           // empty rescan mid-swap: retry
      // Liveness is TIME-based: a stranded rAF id looks armed forever, but "no
      // frame for 3 s" cannot lie. Clear any stale handle and arm fresh.
      var silent = S.lastTick > 0 && performance.now() - S.lastTick > 3000;
      if (!S.raf || silent) {
        if (S.raf) { try { cancelAnimationFrame(S.raf); } catch (e) { /**/ } }
        S.raf = requestAnimationFrame(frame);
        S.lastTick = performance.now();      // one forced restart per silent window
      }
    }, 2000);
  }

  function kick() {
    pulse();
    var m = window.__hypeMap;
    if (m) {
      if (m !== S.map) bind(m); else scan();
      if (!S.raf) {
        S.raf = requestAnimationFrame(frame);
        S.lastTick = performance.now();      // fresh arm gets a full grace window
      }
      return;
    }
    if (S.waitTimer) return;
    S.waitTimer = setInterval(function () {  // a restore-burst message can beat the map
      if (!S.on) { clearInterval(S.waitTimer); S.waitTimer = 0; return; }
      if (window.__hypeMap) { clearInterval(S.waitTimer); S.waitTimer = 0; kick(); }
    }, 200);
  }

  function apply(msg) {
    if (!msg) return;
    // Shiny keeps ONE handler per custom message type, so the 3-D animator in
    // mesh3d.js cannot register its own hype_fp_anim handler: forward each
    // message through its hook instead (absent until the 3-D viewer loads).
    if (window.__hypeFpAnim3dApply) {
      try { window.__hypeFpAnim3dApply(msg); } catch (e) { /* viewer not ready */ }
    }
    S.on = !!msg.on;
    if (typeof msg.speed === "number" && msg.speed > 0) S.speed = msg.speed;
    if (typeof msg.color === "string" && msg.color) S.color = msg.color;
    if (msg.style === "comet" || msg.style === "dots") S.style = msg.style;
    if (msg.mode === "solid" || msg.mode === "total" || msg.mode === "elapsed") {
      S.mode = msg.mode;
    }
    if (msg.lmode === "class" || msg.lmode === "total" || msg.lmode === "elapsed"
        || msg.lmode === "solid" || msg.lmode === "single") {   // retired names tolerated
      S.lmode = msg.lmode;
    }
    if (typeof msg.lw === "number" && msg.lw > 0) S.lw = msg.lw;
    if (typeof msg.lop === "number" && msg.lop >= 0) S.lop = msg.lop;
    S.lineDirty = true;          // any settings push may restyle the line cache
    if (!lineElapsed()) { S.lineCv = null; S.lineRef = null; }
    retime();                    // speed changes retime in place, no rescan; retime also
                                 // refreshes the rainbow scale + per-path colors
    if (!S.on) {
      if (S.raf) { cancelAnimationFrame(S.raf); S.raf = 0; }
      if (lineRainbow()) {
        // Static mode: bind if the map arrived after us; staticDraw clears and (with
        // paths + a rainbow line mode) draws the elapsed lines and/or the legend.
        var m = window.__hypeMap;
        if (m && m !== S.map) bind(m);     // bind -> scan -> retime -> staticDraw
        else staticDraw();
      } else if (S.ctx) {
        S.ctx.clearRect(0, 0, S.w, S.h);
      }
      return;
    }
    kick();
  }

  function register() {
    if (!(window.Shiny && window.Shiny.addCustomMessageHandler)) return false;
    try {                                    // reconnects re-run this; duplicates THROW
      window.Shiny.addCustomMessageHandler("hype_fp_anim", apply);
    } catch (e) { /* already registered */ }
    return true;
  }
  if (!register()) document.addEventListener("shiny:connected", register);

  window.__hypeFpAnim = S;                   // debug/E2E handle (the __hypeMesh3d convention)
})();
