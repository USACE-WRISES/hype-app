// 2-D length ruler for the map (client-only — no server round-trip).
//
// Adds a ruler button to the map's top-right control corner. Click to arm: the cursor turns
// to a crosshair, vector layers stop catching the pointer (so clicks land on the map, never
// selecting a path/boundary), and each map click drops a vertex. A live polyline + vertex
// dots draw as you go, with a tooltip showing the cumulative GEODESIC length
// (L.latLng.distanceTo — metres, switching to km past 1 km). Double-click or Esc finishes
// (freezes the line); re-clicking the button clears + disarms. Refuses to arm while a
// Leaflet.draw actions bar is open (a live reach/boundary draw owns the map). The map object
// is window.__hypeMap, captured by map_bounds.js.
(function () {
  "use strict";

  // Shared flag read by tree.js hookMapClear (an armed measure click must not deselect).
  window.__hypeMeasure2D = { active: false };

  var armed = false;
  var pts = [];                    // L.LatLng vertices placed so far
  var line = null;                 // the growing/finished polyline
  var dots = [];                   // vertex marker circles
  var tip = null;                  // length tooltip (bound to the last vertex)
  var btn = null;
  var guard = null;                // visibility watchdog while armed

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

  function fmt(m) {
    if (m >= 1000) return (m / 1000).toFixed(2) + " km";
    return (m >= 100 ? m.toFixed(0) : m.toFixed(1)) + " m";
  }

  function totalLength() {
    var d = 0;
    for (var i = 1; i < pts.length; i++) d += pts[i - 1].distanceTo(pts[i]);
    return d;
  }

  function clearGraphics(map) {
    if (line) { map.removeLayer(line); line = null; }
    dots.forEach(function (d) { map.removeLayer(d); });
    dots = [];
    if (tip) { map.removeLayer(tip); tip = null; }
    pts = [];
  }

  function redraw(map) {
    var L = window.L;
    if (!line) {
      line = L.polyline(pts, { color: "#ff9500", weight: 3, opacity: 0.95,
                               dashArray: "6 4", interactive: false }).addTo(map);
    } else {
      line.setLatLngs(pts);
    }
    var last = pts[pts.length - 1];
    var dot = L.circleMarker(last, { radius: 4, color: "#b35f00", weight: 1.5,
                                     fillColor: "#ff9500", fillOpacity: 1,
                                     interactive: false }).addTo(map);
    dots.push(dot);
    if (pts.length >= 2) {
      if (!tip) {
        tip = L.tooltip({ permanent: true, direction: "right", offset: [8, 0],
                          className: "hype-measure2d-tip", interactive: false });
      }
      tip.setLatLng(last).setContent(fmt(totalLength()));
      if (!tip._map) tip.addTo(map);
    }
  }

  function setArmed(map, on) {
    armed = on;
    window.__hypeMeasure2D.active = on;
    var cont = map.getContainer();
    if (btn) btn.classList.toggle("active", on);
    // crosshair + vectors ignore the pointer so a click never selects a path/boundary
    cont.classList.toggle("hype-measuring-2d", on);
    if (on) {
      clearGraphics(map);
      guard = setInterval(function () {
        if (!btn || btn.offsetParent === null) { setArmed(map, false); }
      }, 400);
    } else if (guard) {
      clearInterval(guard); guard = null;
    }
  }

  function attach(map) {
    var L = window.L;
    var corner = map._controlCorners && map._controlCorners.topright;
    var cont = map.getContainer();
    if (!corner || !cont || cont.querySelector(".hype-measure2d")) return;

    btn = document.createElement("div");
    btn.className = "leaflet-bar leaflet-control hype-measure2d";
    var a = document.createElement("a");
    a.href = "#";
    a.title = "Measure a distance (click points; double-click or Esc to finish)";
    a.setAttribute("role", "button");
    a.innerHTML =                            // ruler icon (matches xsection.js's SVG style)
      '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor"' +
      ' stroke-width="1.8" stroke-linecap="round">' +
      '<rect x="3" y="9" width="18" height="7" rx="1"/>' +
      '<path d="M7.5 9v3M11.5 9v3M15.5 9v3"/></svg>';
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
      if (window.__hypeXSect && window.__hypeXSect.active) return;  // section tool owns clicks
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
    map.on("dblclick", function (e) {
      if (!armed) return;
      if (e.originalEvent) { e.originalEvent.preventDefault(); }
      setArmed(map, false);                  // freeze the finished line (graphics persist)
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key !== "Escape") return;
      if (armed) setArmed(map, false);
      else if (line) clearGraphics(map);     // Esc after finishing clears the ruler
    });
  }

  var tries = 0;
  var t = setInterval(function () {          // map_bounds.js captures the map; wait for it
    tries += 1;
    if (window.__hypeMap) {
      attach(window.__hypeMap);
      clearInterval(t);
    } else if (tries > 300) {
      clearInterval(t);
    }
  }, 200);
})();
