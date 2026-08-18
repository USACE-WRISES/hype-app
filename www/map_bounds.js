// Report the Leaflet map's view bounds to Shiny as input `map_bounds`
// ({west, south, east, north}, EPSG:4326), refreshed on every moveend/zoomend.
//
// Why this exists: ipyleaflet's server-side `Map.bounds` trait arrives DEGENERATE in this
// stack — ((center, center)) instead of the real extent — so server features that need the
// current view (e.g. the DEM step's "Recalculate legend from view") can't use it. The map
// instance lives inside the jupyter-leaflet bundle with no global registry, so it is
// captured here via L.Map.addInitHook (public Leaflet API), with a late-capture fallback
// (a transient L.Evented.fire hook + synthetic mousemove) in case the map was constructed
// before the hook registered.
(function () {
  "use strict";

  function report(map) {
    try {
      // NOT map.getBounds(): in this widget layout the container's clientWidth/Height
      // are 0 (absolutely-positioned children), so Leaflet's cached _size — and with it
      // getBounds() and ipyleaflet's bounds trait — collapse to the center point. The
      // rendered box from getBoundingClientRect() is correct, and containerPointToLatLng
      // only needs the pane offset, not the cached size.
      var r = map.getContainer().getBoundingClientRect();
      var w = r.width, h = r.height;
      if (!(w > 0 && h > 0)) {           // hidden/backgrounded tab: lumino layout is 0×0
        w = window.innerWidth || 1000;   // → approximate the view with the window size
        h = window.innerHeight || 700;
      }
      var a = map.containerPointToLatLng([0, 0]);
      var b = map.containerPointToLatLng([w, h]);
      if (window.Shiny && window.Shiny.setInputValue) {
        window.Shiny.setInputValue("map_bounds", {
          west: Math.min(a.lng, b.lng), south: Math.min(a.lat, b.lat),
          east: Math.max(a.lng, b.lng), north: Math.max(a.lat, b.lat),
          w: Math.round(w), h: Math.round(h)   // px, so the video matches the viewport
        });
      }
    } catch (e) { /* map not ready — next moveend will report */ }
  }

  function attach(map, tries) {
    if (window.__hypeMap === map) return;
    // Only THE main map (inside #map) may claim __hypeMap: the service modals (USGS flow,
    // NRCS soils) build their own throwaway Leaflet maps, and the init hook fires for those
    // too — binding one would point the heal/fly/measure machinery at a dead detached map.
    var el;
    try { el = map.getContainer(); } catch (e) { return; }
    if (!el) return;
    if (!el.isConnected) {                 // init hook can fire before the DOM insert
      if ((tries || 0) < 25) setTimeout(function () { attach(map, (tries || 0) + 1); }, 200);
      return;
    }
    if (!el.closest || !el.closest("#map")) return;   // a modal map — never bind
    window.__hypeMap = map;
    // The main map exists in the DOM: this is "the map view is up". Reveal the app behind
    // the boot veil on the next paint and tell the server, which opens the start page.
    map.whenReady(function () {
      requestAnimationFrame(function () { bootReady(); });
    });
    map.on("moveend zoomend", function () { report(map); });
    report(map);
    guardVectors(map);
    // On-demand refresh for consumers that read map_bounds server-side at a click
    // (the Export menu): a pane/window resize with no pan since leaves the last
    // report stale, and an export sized from it letterboxes. Same-batch ordering
    // means a synchronous re-report lands before the click's own event input.
    window.__hypeReportBounds = function () { report(map); };
  }

  // ---- big-zoom vector guard --------------------------------------------------------------
  // During a zoom animation Leaflet CSS-scales the vector panes instead of re-projecting the
  // paths. Flying from the far-out default view to a site (project open → hype_fly) scales
  // every stroke by 2^Δzoom, so the few-pixel reach centerline becomes a screen-filling wash
  // of pink until the flight settles. Hide the vector renderers (SVG/canvas panes) whenever
  // the in-flight zoom is ≥3 levels from the last settled zoom; on settle the paths
  // re-project and reappear crisp. Tiles and divIcon labels live in other panes and keep
  // animating, so the fly-in still reads as a flight. Single-step zooming (Δ1) never trips it.
  function guardVectors(map) {
    var settled = map.getZoom();
    function setVis(v) {
      var els = map.getContainer().querySelectorAll(
        ".leaflet-pane > svg, .leaflet-pane > canvas");
      for (var i = 0; i < els.length; i++) els[i].style.visibility = v;
    }
    map.on("zoom zoomanim", function (e) {     // `zoom` fires per frame during flyTo
      var z = (e && typeof e.zoom === "number") ? e.zoom : map.getZoom();
      if (Math.abs(z - settled) >= 3) setVis("hidden");
    });
    map.on("moveend zoomend viewreset", function () {
      settled = map.getZoom();
      // Deferred past this event's dispatch: the renderers' own settle listeners re-project
      // the paths in the same turn, so the reveal lands after geometry is correct again.
      setTimeout(function () { setVis(""); }, 0);
    });
  }

  function lateCapture() {
    if (window.__hypeMap || !window.L || !window.L.Evented) return;
    var orig = window.L.Evented.prototype.fire;
    window.L.Evented.prototype.fire = function () {
      if (this instanceof window.L.Map) attach(this);
      return orig.apply(this, arguments);
    };
    var cont = document.querySelector(".leaflet-container");
    if (cont) {
      cont.dispatchEvent(new MouseEvent("mousemove",
        {bubbles: true, clientX: 5, clientY: 5}));
    }
    window.L.Evented.prototype.fire = orig;
  }

  // ---- boot veil + map-ready ping ---------------------------------------------------------
  // #hype-boot covers the shell from the first byte. It lifts once the main map is in the DOM
  // (attach() above) or, failing that, 6 s after Shiny connects, so a widget that never comes
  // up cannot trap the user behind a spinner. Either way the server hears `hype_map_ready`
  // exactly once, and opens the start page off it (app.py _welcome_gate).
  var bootDone = false;
  function bootReady() {
    if (bootDone) return;
    bootDone = true;
    var veil = document.getElementById("hype-boot");
    if (veil) {
      veil.classList.add("is-done");                       // opacity transition (CSS)
      setTimeout(function () { veil.hidden = true; }, 320);
    }
    var post = function () {
      if (window.Shiny && window.Shiny.setInputValue) {
        window.Shiny.setInputValue("hype_map_ready", Date.now(), {priority: "event"});
        return true;
      }
      return false;
    };
    if (!post()) {                                         // Shiny not up yet: retry briefly
      var n = 0;
      var r = setInterval(function () { if (post() || ++n > 50) clearInterval(r); }, 100);
    }
  }
  window.__hypeBootReady = bootReady;                      // E2E + manual escape hatch
  document.addEventListener("shiny:connected", function () { setTimeout(bootReady, 6000); });
  // Belt and braces: a page that never connects at all (server down) still lifts the veil,
  // so Shiny's own grey disconnect overlay is what the user sees, not ours.
  setTimeout(bootReady, 15000);

  var hooked = false;
  var tries = 0;
  var t = setInterval(function () {
    tries += 1;
    if (!hooked && window.L && window.L.Map && window.L.Map.addInitHook) {
      hooked = true;
      window.L.Map.addInitHook(function () { attach(this); });
      lateCapture();                       // in case the map beat the hook
    }
    if (hooked && !window.__hypeMap) lateCapture();
    if (window.__hypeMap || tries > 150) clearInterval(t);   // give up after ~30 s
  }, 200);

  // ---- hype_map_sweep: drop orphaned vector layers by their hz_lyr feature tag ----------
  // jupyter-leaflet sometimes loses a layer REMOVAL under bursty updates: the widget is gone
  // server-side but its leaflet group keeps rendering, unreachable by any checkbox (ghost
  // flow paths). The server tags every hz feature with its layer key (properties.hz_lyr) and
  // sends the keys it wants gone — always BEFORE re-adding fresh widgets, so a sweep can
  // never eat a live view's layers.
  function tagOf(l) {
    // Leaf feature layers carry .feature directly — an orphaned CHILD (its group died but
    // the child stayed on the map) is invisible to a groups-only scan. For groups, find the
    // first tagged kid anywhere: kids[0] alone hides a whole group behind one untagged feature.
    var f = l && l.feature;
    var tag = f && f.properties && f.properties.hz_lyr;
    if (tag) return tag;
    if (l instanceof window.L.GeoJSON && l.getLayers) {
      var kids = l.getLayers();
      for (var i = 0; i < kids.length; i++) {
        f = kids[i] && kids[i].feature;
        tag = f && f.properties && f.properties.hz_lyr;
        if (tag) return tag;
      }
    }
    return null;
  }

  function sweep(msg) {
    var m = window.__hypeMap;
    if (!m || !window.L || !msg || !msg.keys || !msg.keys.length) return;
    var want = {};
    msg.keys.forEach(function (k) { want[k] = true; });
    var doomed = [];
    m.eachLayer(function (l) {
      var tag = tagOf(l);
      if (tag && want[tag]) doomed.push(l);
    });
    doomed.forEach(function (l) { try { m.removeLayer(l); } catch (e) { /* already gone */ } });
  }
  if (window.Shiny && window.Shiny.addCustomMessageHandler) {
    window.Shiny.addCustomMessageHandler("hype_map_sweep", sweep);
  }

  // ---- hype_map_verify: report which expected hz_lyr keys are MISSING from the map ------------
  // Targeted heal for the rare layer a bursty add drops client-side. Replaces the old relayer that
  // blindly re-added EVERY layer (a guaranteed multi-second flicker). The server sends the keys it
  // expects visible; we reply with only the ones actually absent, so it re-adds just those.
  function verify(msg) {
    var m = window.__hypeMap;
    if (!m || !window.L || !msg || !msg.keys) return;
    var present = {};
    m.eachLayer(function (l) {
      // same net as sweep(): a drawn orphan counts as PRESENT — healing on top of it would
      // stack a duplicate the user can't remove; the orphan itself stays sweepable.
      var tag = tagOf(l);
      if (tag) present[tag] = true;
    });
    var missing = msg.keys.filter(function (k) { return !present[k]; });
    if (window.Shiny && window.Shiny.setInputValue) {
      window.Shiny.setInputValue("hype_map_missing", { keys: missing, nonce: msg.nonce },
                                 { priority: "event" });
    }
  }
  if (window.Shiny && window.Shiny.addCustomMessageHandler) {
    window.Shiny.addCustomMessageHandler("hype_map_verify", verify);
  }
})();
