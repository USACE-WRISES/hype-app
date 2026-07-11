// 2-D terrain cross-section tool: draw a line on the map; the server samples the DEM (and
// the carved terrain, if a channel modification is applied) along it and pops the profile
// in a modal (app.py `xsect_line` handler).
//
// Adds a ⛰ button BELOW the measure ruler in the map's top-right control corner — this file
// waits for .hype-measure2d to exist first, so the stacking order is deterministic. The
// button is display:none by default; app.py's xsect_style gate shows it while the DEM layer
// is available and checked. Click to arm: crosshair cursor, vector layers release the
// pointer, each map click drops a vertex; double-click or Esc finishes and posts
// {latlngs, nonce} to Shiny. The section line stays on the map; re-clicking the button (or
// Esc again) clears it. The map object is window.__hypeMap, captured by map_bounds.js.
(function () {
  "use strict";

  // Shared flag read by tree.js hookMapClear (an armed section click must not deselect).
  window.__hypeXSect = { active: false };

  var armed = false;
  var pts = [];                    // L.LatLng vertices placed so far
  var line = null;                 // the growing/finished section line
  var dots = [];                   // vertex marker circles
  var preview = null;              // rubber-band segment from the last vertex to the cursor
  var tip = null;                  // "Double-click to end XS" hint that follows the cursor
  var btn = null;
  var guard = null;                // visibility watchdog (gate may hide us) — runs while a line lives

  function drawBusy() {
    // A live Leaflet.draw DRAW (reach centerline / boundary) sets its actions bar visible.
    var w = document.querySelector(".hype-map-wrap");
    if (!w) return false;
    var anchors = w.querySelectorAll(".leaflet-draw-draw-polyline, .leaflet-draw-draw-polygon");
    for (var i = 0; i < anchors.length; i++) {
      var sec = anchors[i].closest(".leaflet-draw-section");
      var a = sec && sec.querySelector(".leaflet-draw-actions");
      if (a && a.style.display === "block") return true;
    }
    return false;
  }

  function post() {
    if (pts.length >= 2 && window.Shiny && window.Shiny.setInputValue) {
      window.Shiny.setInputValue("xsect_line", {
        latlngs: pts.map(function (p) { return { lat: p.lat, lng: p.lng }; }),
        nonce: Date.now()
      }, { priority: "event" });
    }
  }

  // The button reads active when ARMED or when a finished section is on the map — so after a
  // double-click the user still sees it highlighted and knows a click removes the section.
  function refreshBtn() { if (btn) btn.classList.toggle("active", armed || !!line); }

  function startGuard(map) {                // the DEM gate can hide the button mid-draw or after
    if (guard) return;
    guard = setInterval(function () {
      if (!btn || btn.offsetParent === null) { setArmed(map, false); clearGraphics(map); }
    }, 400);
  }
  function stopGuard() { if (guard) { clearInterval(guard); guard = null; } }

  function hidePreview(map) {               // rubber-band + hint only make sense while drawing
    if (preview) { map.removeLayer(preview); preview = null; }
    if (tip) { map.removeLayer(tip); tip = null; }
  }

  function showPreview(map, cursor) {
    var L = window.L;
    var seg = [pts[pts.length - 1], cursor];
    if (!preview) {
      preview = L.polyline(seg, { color: "#0b7285", weight: 2, opacity: 0.8,
                                  dashArray: "3 6", interactive: false }).addTo(map);
    } else {
      preview.setLatLngs(seg);
    }
    if (!tip) {
      tip = L.tooltip({ permanent: true, direction: "right", offset: [12, 0],
                        className: "hype-xsect-tip", interactive: false });
    }
    tip.setLatLng(cursor).setContent("Double-click to end XS");   // position BEFORE addTo
    if (!tip._map) tip.addTo(map);                                // else onAdd projects undefined
  }

  function clearGraphics(map) {
    if (line) { map.removeLayer(line); line = null; }
    dots.forEach(function (d) { map.removeLayer(d); });
    dots = [];
    hidePreview(map);
    pts = [];
    stopGuard();
    refreshBtn();
  }

  function redraw(map) {
    var L = window.L;
    if (!line) {
      line = L.polyline(pts, { color: "#0b7285", weight: 3, opacity: 0.95,
                               dashArray: "2 6", interactive: false }).addTo(map);
    } else {
      line.setLatLngs(pts);
    }
    var last = pts[pts.length - 1];
    var dot = L.circleMarker(last, { radius: 4, color: "#053b46", weight: 1.5,
                                     fillColor: "#15aabf", fillOpacity: 1,
                                     interactive: false }).addTo(map);
    dots.push(dot);
  }

  function setArmed(map, on) {
    armed = on;
    window.__hypeXSect.active = on;
    map.getContainer().classList.toggle("hype-xsecting", on);
    if (on) {
      clearGraphics(map);                    // fresh start (also stops any old guard)
      startGuard(map);
    } else {
      hidePreview(map);                      // stop the rubber-band; a finished line stays drawn
      if (!line) stopGuard();                // keep watching while a finished section lives
    }
    refreshBtn();
  }

  function attach(map) {
    var corner = map._controlCorners && map._controlCorners.topright;
    var cont = map.getContainer();
    if (!corner || !cont || cont.querySelector(".hype-xsect")) return;

    btn = document.createElement("div");
    btn.className = "leaflet-bar leaflet-control hype-xsect";
    var a = document.createElement("a");
    a.href = "#";
    a.title = "Terrain cross-section (click points along a line; double-click or Esc to plot)";
    a.setAttribute("role", "button");
    a.innerHTML =                            // section cut-line: horizontal cut + end ticks (A–A′),
      '<svg viewBox="0 0 24 24" aria-hidden="true">' +   // matches the straight line drawn on the map
      '<path d="M4 7V17M4 12H20M20 7V17" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/></svg>';
    btn.appendChild(a);
    corner.appendChild(btn);

    a.addEventListener("click", function (ev) {
      ev.preventDefault(); ev.stopPropagation();
      if (armed || line) {                   // second press clears + disarms
        setArmed(map, false);
        clearGraphics(map);
        return;
      }
      if (drawBusy()) return;                // a live draw owns the map
      if (window.__hypeMeasure2D && window.__hypeMeasure2D.active) return;  // ruler owns clicks
      setArmed(map, true);
    });
    ["mousedown", "dblclick", "pointerdown"].forEach(function (t) {
      btn.addEventListener(t, function (ev) { ev.stopPropagation(); });
    });

    map.on("click", function (e) {
      if (!armed) return;
      pts.push(e.latlng);
      redraw(map);
    });
    map.on("mousemove", function (e) {       // live rubber-band + hint from the last vertex
      if (armed && pts.length >= 1) showPreview(map, e.latlng);
      else hidePreview(map);
    });
    map.on("dblclick", function (e) {
      if (!armed) return;
      if (e.originalEvent) { e.originalEvent.preventDefault(); }
      setArmed(map, false);                  // freeze the line, then hand it to the server
      post();
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key !== "Escape") return;
      if (armed) {
        setArmed(map, false);
        post();                              // Esc finishes like a double-click
      } else if (line) {
        clearGraphics(map);                  // Esc after finishing clears the section
      }
    });
  }

  var tries = 0;
  var t = setInterval(function () {          // wait for the map AND the measure button (order)
    tries += 1;
    var m = window.__hypeMap;
    if (m && m.getContainer() && m.getContainer().parentNode &&
        document.querySelector(".hype-measure2d")) {
      attach(m);
      clearInterval(t);
    } else if (tries > 300) {
      if (m) attach(m);                      // measure never appeared — attach anyway
      clearInterval(t);
    }
  }, 200);
})();
