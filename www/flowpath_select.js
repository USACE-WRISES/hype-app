// Box-select control for flow paths on the Results step.
//
// Adds a small button to the map's top-right control corner (hidden by default; app.py's
// map_edit_style reveals it on Results only). Click to arm: the map stops panning, the cursor
// turns to a crosshair, and dragging draws a rubber-band box. On release the box corners are
// converted with containerPointToLatLng (the map_bounds.js technique — immune to the 0×0
// lumino-size bug) and posted to Shiny as `fp_select_box` {west, south, east, north}. The app
// selects every flow path the box touches (crossing-window semantics). Esc or re-clicking the
// button cancels. The map object is window.__hypeMap, captured by map_bounds.js.
(function () {
  "use strict";

  var armed = false;
  var dragging = false;
  var start = null;                       // {x, y} client coords at mousedown
  var band = null;                        // the rubber-band div
  var btn = null;                         // the control button (outer div)
  var guard = null;                       // visibility watchdog while armed

  function post(name, value) {
    if (window.Shiny && window.Shiny.setInputValue) {
      window.Shiny.setInputValue(name, value, {priority: "event"});
    }
  }

  function syncModeButtons() {
    // The sidebar "Select: Single | Multiple" buttons re-render with the pane; re-apply state.
    var single = document.querySelector(".hype-fpsel-single");
    var multi = document.querySelector(".hype-fpsel-multi");
    if (single) single.classList.toggle("active", !armed);
    if (multi) multi.classList.toggle("active", armed);
  }

  function setArmed(map, on) {
    armed = on;
    dragging = false;
    start = null;
    var cont = map.getContainer();
    if (btn) btn.classList.toggle("active", on);
    cont.classList.toggle("hype-fpsel-arming", on);   // crosshair + paths ignore the pointer
    if (band) band.style.display = "none";
    try { if (on) { map.dragging.disable(); } else { map.dragging.enable(); } }
    catch (e) { /* dragging handler unavailable — box still works */ }
    if (on) {                             // auto-cancel if the step changes (button hidden)
      guard = setInterval(function () {
        if (!btn || btn.offsetParent === null) { setArmed(map, false); return; }
        syncModeButtons();                // pane re-renders drop the active class — re-apply
      }, 400);
    } else if (guard) {
      clearInterval(guard); guard = null;
    }
    syncModeButtons();
  }

  function bandRect(a, b) {
    return {left: Math.min(a.x, b.x), top: Math.min(a.y, b.y),
            w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y)};
  }

  function attach(map) {
    var corner = map._controlCorners && map._controlCorners.topright;
    var cont = map.getContainer();
    if (!corner || !cont || cont.querySelector(".hype-fpsel")) return;

    btn = document.createElement("div");
    btn.className = "leaflet-bar leaflet-control hype-fpsel";
    var a = document.createElement("a");
    a.href = "#";
    a.title = "Select flow paths (drag a box)";
    a.setAttribute("role", "button");
    a.innerHTML = "&#x2b1a;";             // ⬚ dotted square
    btn.appendChild(a);
    corner.appendChild(btn);

    band = document.createElement("div");
    band.className = "hype-fpsel-band";
    band.style.display = "none";
    cont.appendChild(band);

    a.addEventListener("click", function (ev) {
      ev.preventDefault(); ev.stopPropagation();
      setArmed(map, !armed);
    });
    // Sidebar "Select: Single | Multiple" buttons (Shiny re-renders the pane, so delegate).
    document.addEventListener("click", function (ev) {
      if (!ev.target || !ev.target.closest) return;
      if (ev.target.closest(".hype-fpsel-multi")) {
        ev.preventDefault();
        setArmed(map, !armed);            // click again to cancel back to Single
      } else if (ev.target.closest(".hype-fpsel-single")) {
        ev.preventDefault();
        if (armed) setArmed(map, false);
      }
    });
    // Leaflet must not treat button presses as map interaction.
    ["mousedown", "dblclick", "pointerdown"].forEach(function (t) {
      btn.addEventListener(t, function (ev) { ev.stopPropagation(); });
    });

    cont.addEventListener("mousedown", function (ev) {
      if (!armed || ev.button !== 0) return;
      ev.preventDefault(); ev.stopPropagation();
      dragging = true;
      start = {x: ev.clientX, y: ev.clientY};
      var r = cont.getBoundingClientRect();
      band.style.display = "block";
      band.style.left = (start.x - r.left) + "px";
      band.style.top = (start.y - r.top) + "px";
      band.style.width = "0px";
      band.style.height = "0px";
    }, true);

    document.addEventListener("mousemove", function (ev) {
      if (!armed || !dragging) return;
      ev.preventDefault();
      var r = cont.getBoundingClientRect();
      var b = bandRect(start, {x: ev.clientX, y: ev.clientY});
      band.style.left = (b.left - r.left) + "px";
      band.style.top = (b.top - r.top) + "px";
      band.style.width = b.w + "px";
      band.style.height = b.h + "px";
    }, true);

    document.addEventListener("mouseup", function (ev) {
      if (!armed || !dragging) return;
      ev.preventDefault(); ev.stopPropagation();
      var s = start;                      // setArmed() nulls `start` — keep the anchor
      var end = {x: ev.clientX, y: ev.clientY};
      var b = bandRect(s, end);
      setArmed(map, false);               // disarm first — hides the band, re-enables panning
      if (b.w < 3 && b.h < 3) return;     // a bare click, not a box: treat as cancel
      try {
        var r = cont.getBoundingClientRect();
        var p1 = map.containerPointToLatLng([s.x - r.left, s.y - r.top]);
        var p2 = map.containerPointToLatLng([end.x - r.left, end.y - r.top]);
        post("fp_select_box", {
          west: Math.min(p1.lng, p2.lng), south: Math.min(p1.lat, p2.lat),
          east: Math.max(p1.lng, p2.lng), north: Math.max(p1.lat, p2.lat),
          nonce: Date.now()
        });
      } catch (e) { /* projection not ready — nothing selected */ }
    }, true);

    document.addEventListener("keydown", function (ev) {
      if (armed && ev.key === "Escape") setArmed(map, false);
    });
  }

  var tries = 0;
  var t = setInterval(function () {       // map_bounds.js captures the map; we just wait for it
    tries += 1;
    if (window.__hypeMap) {
      attach(window.__hypeMap);
      clearInterval(t);
    } else if (tries > 300) {             // give up after ~60 s
      clearInterval(t);
    }
  }, 200);
})();
