// Raster hover probe: while a raster-valued tree node (Terrain, Water surface raster,
// Depth raster, Hydraulic head, Channel cut) is selected AND visible, a small dark chip
// follows the cursor showing the raster value under it. The server ships the SAME
// EPSG:4326 float32 grid the overlay PNG was colored from — over HTTP via dynamic_route,
// never the websocket (multi-MB frames have killed sessions) — so sampling is fully
// client-side: zero hover latency, and values always agree with the displayed colors.
// The "hype_probe" message carries only settings + the grid URL (app.py _send_probe).
(function () {
  "use strict";

  var S = {
    on: false, url: null, key: null, label: "", units: "m", decimals: 2,
    grid: null, w: 0, h: 0, b: null,        // Float32Array + dims + {s,w,n,e} center extents
    map: null, tip: null, val: null, lyr: null,
    hidden: false,                          // zoom in flight: container math unreliable
    lastCP: null, lastClient: null,         // last containerPoint + [clientX, clientY]
    seq: 0, waitTimer: 0, watchTimer: 0
  };

  // ---- grid fetch ------------------------------------------------------------------------
  function fetchGrid(msg) {
    var mySeq = ++S.seq;                    // stale responses (fast head-slider drags) lose
    S.grid = null;                          // never index an old grid with new dims/bounds
    fetch(msg.url).then(function (r) { return r.arrayBuffer(); }).then(function (buf) {
      if (mySeq !== S.seq || !S.on) return;
      if (buf.byteLength !== msg.w * msg.h * 4) return;   // truncated/foreign response
      // Bytes are numpy "<f4" (little-endian); Float32Array reads platform-endian, and
      // every target browser is little-endian.
      S.grid = new Float32Array(buf);
    }).catch(function () { /* no grid, no chip */ });
  }

  // ---- value lookup ----------------------------------------------------------------------
  function valueAt(lat, lng) {
    var b = S.b;
    if (!S.grid || !b) return null;
    var fx = (lng - b.w) / ((b.e - b.w) || 1);
    var fy = (b.n - lat) / ((b.n - b.s) || 1);            // row 0 = north
    if (!(fx >= 0 && fx <= 1 && fy >= 0 && fy <= 1)) return null;
    var col = Math.round(fx * (S.w - 1));                 // nearest cell CENTER: the bounds
    var row = Math.round(fy * (S.h - 1));                 // are center extents (dem.py)
    var v = S.grid[row * S.w + col];
    return (v === v) ? v : null;                          // NaN self-compares false: no data
  }

  function toolBusy() {
    // The crosshair tools own the cursor; a value chip riding along would fight them.
    if (window.__hypeMeasure2D && window.__hypeMeasure2D.active) return true;
    if (window.__hypeXSect && window.__hypeXSect.active) return true;
    var rs = window.__hypeReachState;
    if (rs && (rs.picking || rs.arm)) return true;
    var c = S.map && S.map.getContainer();
    return !!(c && c.classList.contains("hype-fpsel-arming"));
  }

  // ---- chip ------------------------------------------------------------------------------
  function ensureTip() {
    if (S.tip) return S.tip;
    S.tip = document.createElement("div");
    S.tip.className = "hype-probe-tip";
    S.val = document.createElement("span");
    S.lyr = document.createElement("span");
    S.lyr.className = "hype-probe-lyr";
    S.tip.appendChild(S.val);
    S.tip.appendChild(S.lyr);
    document.body.appendChild(S.tip);       // fixed-position, body-level: never clipped by
    return S.tip;                           // the map wrap (the pick-tooltip pattern)
  }

  function show(v) {
    if (v === null || !S.lastClient) { hideEl(); return; }
    var t = ensureTip();
    S.val.textContent = v.toFixed(S.decimals) + " " + S.units;
    S.lyr.textContent = S.label;
    t.style.display = "block";
    var x = S.lastClient[0] + 10, y = S.lastClient[1] + 13;
    var r = t.getBoundingClientRect();      // measured after display:block so it has a size
    if (x + r.width + 8 > window.innerWidth) x = S.lastClient[0] - r.width - 12;
    if (y + r.height + 8 > window.innerHeight) y = S.lastClient[1] - r.height - 12;
    t.style.left = x + "px";
    t.style.top = y + "px";
  }

  function hideEl() { if (S.tip) S.tip.style.display = "none"; }
  function hide() { hideEl(); S.lastCP = null; S.lastClient = null; }

  // ---- map events ------------------------------------------------------------------------
  function onMove(e) {
    if (!S.on || !S.grid || S.hidden) { hideEl(); return; }
    var oe = e.originalEvent;
    if (oe && oe.isTrusted === false) return;   // map_bounds.js fires a synthetic mousemove
    if (toolBusy()) { hideEl(); return; }
    S.lastCP = e.containerPoint;
    S.lastClient = oe ? [oe.clientX, oe.clientY] : null;
    show(e.latlng ? valueAt(e.latlng.lat, e.latlng.lng) : null);
  }

  function onZoomStart() { S.hidden = true; hideEl(); }   // keep lastCP for the settle re-show

  function onSettle() {
    S.hidden = false;
    // Wheel zooms don't move the mouse: recompute the value under the resting cursor.
    if (!S.on || !S.grid || !S.lastCP || !S.map) return;
    var ll;
    try { ll = S.map.containerPointToLatLng(S.lastCP); } catch (e) { return; }
    show(valueAt(ll.lat, ll.lng));
  }

  function bind(m) {
    if (S.map === m) return;
    if (S.map) {
      try { S.map.off("mousemove", onMove); } catch (e) { /* gone */ }
      try { S.map.off("mouseout", hide); } catch (e) { /* gone */ }
      try { S.map.off("zoomstart zoomanim", onZoomStart); } catch (e) { /* gone */ }
      try { S.map.off("zoomend moveend viewreset", onSettle); } catch (e) { /* gone */ }
    }
    S.map = m;
    m.on("mousemove", onMove);
    m.on("mouseout", hide);
    m.on("zoomstart zoomanim", onZoomStart);
    m.on("zoomend moveend viewreset", onSettle);
  }

  function startWatch() {
    // No rAF loop to piggyback a rebind check on (path_anim rebinds per frame): a slow
    // identity watch catches the map widget being rebuilt under us.
    if (S.watchTimer) return;
    S.watchTimer = setInterval(function () {
      if (!S.on) return;
      var live = window.__hypeMap;
      if (live && live !== S.map) bind(live);
    }, 2000);
  }

  function stopWatch() {
    if (S.watchTimer) { clearInterval(S.watchTimer); S.watchTimer = 0; }
    if (S.waitTimer) { clearInterval(S.waitTimer); S.waitTimer = 0; }
  }

  function kick() {
    var m = window.__hypeMap;
    if (m) {
      if (m !== S.map) bind(m);
      startWatch();
      return;
    }
    if (S.waitTimer) return;
    S.waitTimer = setInterval(function () {  // a restore-burst push can beat the map
      if (!S.on) { clearInterval(S.waitTimer); S.waitTimer = 0; return; }
      if (window.__hypeMap) { clearInterval(S.waitTimer); S.waitTimer = 0; kick(); }
    }, 200);
  }

  // ---- settings message ------------------------------------------------------------------
  function apply(msg) {
    if (!msg) return;
    if (!msg.on) {
      S.on = false; S.url = null; S.grid = null; S.key = null;
      hide(); stopWatch();
      return;
    }
    S.on = true;
    S.key = msg.key || null;
    S.label = msg.label || "";
    S.units = msg.units || "m";
    S.decimals = (typeof msg.decimals === "number") ? msg.decimals : 2;
    S.w = msg.w; S.h = msg.h;
    var b = msg.bounds || [];
    S.b = { s: +b[0], w: +b[1], n: +b[2], e: +b[3] };
    if (msg.url && msg.url !== S.url) { S.url = msg.url; fetchGrid(msg); }
    kick();
  }

  function register() {
    if (!(window.Shiny && window.Shiny.addCustomMessageHandler)) return false;
    try {                                    // reconnects re-run this; duplicates THROW
      window.Shiny.addCustomMessageHandler("hype_probe", apply);
    } catch (e) { /* already registered */ }
    return true;
  }
  if (!register()) document.addEventListener("shiny:connected", register);

  window.__hypeRasterProbe = S;              // debug/E2E handle (the __hypeMesh3d convention)
  S.valueAt = valueAt;                       // E2E: assert values without synthetic events
})();
