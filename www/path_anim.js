// Flow-path particle animation: ONE particle travels each displayed hyporheic flow path
// (comet streak by default, plain dot as the alternate style), and each particle's loop
// period is proportional to that path's residence time (feature properties.total_time_d,
// already shipped on every hz_paths_* feature) — so a 1-day path visibly cycles twice as
// fast as a 2-day path.
//
// Fully client-side: geometry and residence times are read straight off the live Leaflet
// layers, so the server's only involvement is the tiny "hype_fp_anim" settings message
// ({on, speed, color}) sent from the Flow paths pane (app.py _send_fp_anim). The
// hz layers are parked/cloned/swept/healed constantly, so layer handles are NEVER cached
// across turns — any layeradd/layerremove triggers a debounced rescan instead.
(function () {
  "use strict";

  var S = {
    on: false, speed: 3, msPerDay: 0, color: "#ff2bd6", style: "comet",
    map: null, canvas: null, ctx: null,
    raf: 0, hidden: false, scanTimer: 0, waitTimer: 0,
    paths: [], w: 0, h: 0, dpr: 1
  };

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
      for (var i = 0; i < kids.length; i++) {
        var f = kids[i] && kids[i].feature;
        var pr = f && f.properties;
        var tag = pr && pr.hz_lyr;
        if (!tag || tag.indexOf("hz_paths_") !== 0 || tag === "hz_paths_sel") break;
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
    for (var j = 0; j < S.paths.length; j++) {
      S.paths[j].dur = Math.max(S.paths[j].td * S.msPerDay, 800);
    }
  }

  function schedScan() {
    if (!S.on) return;
    clearTimeout(S.scanTimer);
    S.scanTimer = setTimeout(scan, 250);
  }

  // ---- map + canvas binding --------------------------------------------------------------
  function onZoomStart() {
    // Container-point math is unreliable while a zoom is in flight (CSS-scaled panes,
    // flyTo re-projection), and guardVectors may be hiding the path lines anyway — hide
    // the particles for the duration and pop them back at settle.
    S.hidden = true;
    if (S.canvas) S.canvas.style.visibility = "hidden";
  }

  function onSettle() {
    S.hidden = false;
    if (S.canvas) S.canvas.style.visibility = "";
  }

  function bind(m) {
    if (S.map === m) return;
    if (S.map) {
      try { S.map.off("layeradd layerremove", schedScan); } catch (e) { /* gone */ }
      try { S.map.off("zoomstart zoomanim", onZoomStart); } catch (e) { /* gone */ }
      try { S.map.off("zoomend moveend viewreset", onSettle); } catch (e) { /* gone */ }
    }
    if (S.canvas && S.canvas.parentNode) S.canvas.parentNode.removeChild(S.canvas);
    S.map = m;
    if (!S.canvas) {
      S.canvas = document.createElement("canvas");
      S.canvas.className = "hype-fp-anim";
      S.ctx = S.canvas.getContext("2d");
    }
    m.getContainer().appendChild(S.canvas);
    S.w = S.h = 0;                          // force a resize on the next frame
    m.on("layeradd layerremove", schedScan);
    m.on("zoomstart zoomanim", onZoomStart);
    m.on("zoomend moveend viewreset", onSettle);
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
    var live = window.__hypeMap;
    if (live && live !== S.map) bind(live);  // map widget was rebuilt under us
    if (!S.map || !S.ctx) return;
    resize();
    var ctx = S.ctx;
    ctx.clearRect(0, 0, S.w, S.h);
    if (S.hidden || !S.paths.length) return;
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
      items.push({ p: p, s: s, lo: lo, x: pt.x, y: pt.y });
    }
    if (S.style === "dots") drawDots(ctx, items);
    else drawComets(ctx, items);
  }

  function drawDots(ctx, items) {
    // Two passes (all glows, then all rings) keep canvas state flips off the hot path.
    ctx.shadowColor = S.color;
    ctx.shadowBlur = 4;
    ctx.fillStyle = S.color;
    for (var k = 0; k < items.length; k++) {
      ctx.beginPath(); ctx.arc(items[k].x, items[k].y, 2, 0, 6.2832); ctx.fill();
    }
    ctx.shadowBlur = 0;
    ctx.strokeStyle = (S.color.toLowerCase() === "#ffffff")
      ? "rgba(0,0,0,.55)" : "rgba(255,255,255,.85)";   // contrast ring on any basemap
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
    ctx.strokeStyle = S.color;
    ctx.shadowBlur = 0;
    var core = (S.color.toLowerCase() === "#ffffff")
      ? "rgba(0,0,0,.5)" : "rgba(255,255,255,.9)";
    for (var i = 0; i < items.length; i++) {
      var it = items[i], p = it.p;
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
      ctx.fillStyle = S.color;
      ctx.beginPath(); ctx.arc(it.x, it.y, 2, 0, 6.2832); ctx.fill();
      ctx.fillStyle = core;
      ctx.beginPath(); ctx.arc(it.x, it.y, 0.8, 0, 6.2832); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  // ---- settings message ------------------------------------------------------------------
  function kick() {
    var m = window.__hypeMap;
    if (m) {
      if (m !== S.map) bind(m); else scan();
      if (!S.raf) S.raf = requestAnimationFrame(frame);
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
    S.on = !!msg.on;
    if (typeof msg.speed === "number" && msg.speed > 0) S.speed = msg.speed;
    if (typeof msg.color === "string" && msg.color) S.color = msg.color;
    if (msg.style === "comet" || msg.style === "dots") S.style = msg.style;
    retime();                                    // speed changes retime in place, no rescan
    if (!S.on) {
      if (S.raf) { cancelAnimationFrame(S.raf); S.raf = 0; }
      if (S.ctx) S.ctx.clearRect(0, 0, S.w, S.h);
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
