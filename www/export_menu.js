// Snipping-tool-style capture control in the header: an icon button that EXECUTES
// the capture plus a slim arrow that opens the options (mode: Camera or Video,
// target: Specified Window or Full View Extent).
//
// Targets:
//  - Specified Window: a full-screen rubber-band overlay; the user drags the
//    rectangle to capture. 2D rects re-render server-side at the rect's map
//    bounds (a full-quality render, not a pixel crop); 3D rects crop the vtk
//    canvas (camera) or ride the recorder's offscreen crop (video).
//  - Full View Extent: the whole current view. 2D goes to the server renderer
//    (tiles lack crossorigin, any DOM composite taints); 3D captures the vtk
//    canvas locally (not tainted, its drapes are data URIs).
// Video mode records the ACTIVE view: 2D = the pathline animation, 3D = a
// static-camera recording of the live scene (particles animating).
// Camera captures land in the preview modal (Copy image / Save image); the 3D
// local path auto-copies to the clipboard while the click gesture is live and
// encodes the outcome in the upload's file name so the modal note stays honest.
// Also owns the "Copy image" button inside the preview modal (.hype-copy-still).
(function () {
  "use strict";

  var state = { mode: "camera", target: "view", secs: 8, fps: 30 };

  var CAMERA_SVG =
    '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor"' +
    ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M4 8h3l2-2.5h6L17 8h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/>' +
    '<circle cx="12" cy="13" r="3.4"/></svg>';
  var VIDEO_SVG =
    '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor"' +
    ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="3" y="7" width="12" height="10" rx="1.5"/>' +
    '<path d="M15 11l6-3v8l-6-3z"/></svg>';
  var CHEV_SVG =
    '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor"' +
    ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M7 10l5 5 5-5"/></svg>';

  function $(sel) { return document.querySelector(sel); }
  function is3d() {
    var b = $('.hype-view-btn[data-view="3d"]');
    return !!(b && b.classList.contains("active"));
  }
  function post(inputId, value) {
    if (window.Shiny && Shiny.setInputValue)
      Shiny.setInputValue(inputId, value, { priority: "event" });
  }
  function note(msg, warn) {
    post("export_client_note", { msg: msg, warn: !!warn, n: Date.now() });
  }

  function syncUi() {
    var btn = $("#hype-export-btn");
    var arrow = $("#hype-export-arrow");
    if (arrow && !arrow.innerHTML) arrow.innerHTML = CHEV_SVG;
    if (btn) {
      btn.innerHTML = state.mode === "video" ? VIDEO_SVG : CAMERA_SVG;
      btn.title = state.mode === "video" ? "Record the view" : "Capture the view";
    }
    var menu = $("#hype-export-menu");
    if (!menu) return;
    menu.querySelectorAll("[data-mode]").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-mode") === state.mode);
    });
    menu.querySelectorAll(".hype-export-target").forEach(function (b) {
      b.classList.toggle("sel", b.getAttribute("data-target") === state.target);
    });
    // Video settings (clip length + fps) only matter in Video mode.
    var vs = menu.querySelector(".hype-export-vidset");
    if (vs) {
      vs.style.display = state.mode === "video" ? "" : "none";
      var secs = vs.querySelector('input[data-k="secs"]');
      if (secs && document.activeElement !== secs) secs.value = state.secs;
      vs.querySelectorAll("[data-fps]").forEach(function (b) {
        b.classList.toggle("active", Number(b.getAttribute("data-fps")) === state.fps);
      });
    }
  }

  function openMenu(open) {
    var root = $("#hype-export");
    if (!root) return;
    root.classList.toggle("open", !!open);
    if (open) syncUi();
  }

  // ---- rubber-band snip overlay (Specified Window) ------------------------
  // Full-window fixed overlay above everything but notifications; it swallows
  // every pointer event, so no Leaflet arming or drag-disable is needed.
  var snip = null;   // {el, band, mode, sx, sy, dragging}

  function endSnip(rect) {
    if (!snip) return;
    var mode = snip.mode;
    document.removeEventListener("keydown", snipKey, true);
    try { snip.el.remove(); } catch (e) { /**/ }
    snip = null;
    if (rect) dispatchRect(mode, rect);
  }
  function snipKey(ev) {
    if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); endSnip(null); }
  }

  function startSnip(mode) {
    if (snip) return;
    var el = document.createElement("div");
    el.className = "hype-snip";
    el.innerHTML = '<div class="hype-snip-hint">Drag to select an area. Esc cancels.</div>' +
                   '<div class="hype-snip-band" style="display:none;"></div>';
    document.body.appendChild(el);
    snip = { el: el, band: el.querySelector(".hype-snip-band"), mode: mode,
             sx: 0, sy: 0, dragging: false };
    document.addEventListener("keydown", snipKey, true);
    el.addEventListener("mousedown", function (ev) {
      if (ev.button !== 0) { endSnip(null); return; }
      ev.preventDefault();
      snip.dragging = true;
      snip.sx = ev.clientX; snip.sy = ev.clientY;
      snip.band.style.display = "block";
      paintBand(ev.clientX, ev.clientY);
    });
    el.addEventListener("mousemove", function (ev) {
      if (snip && snip.dragging) paintBand(ev.clientX, ev.clientY);
    });
    el.addEventListener("mouseup", function (ev) {
      if (!snip || !snip.dragging) return;
      ev.preventDefault();
      var r = { left: Math.min(snip.sx, ev.clientX), top: Math.min(snip.sy, ev.clientY),
                w: Math.abs(ev.clientX - snip.sx), h: Math.abs(ev.clientY - snip.sy) };
      endSnip(r.w >= 8 && r.h >= 8 ? r : null);
    });
  }

  function paintBand(x, y) {
    var b = snip.band;
    b.style.left = Math.min(snip.sx, x) + "px";
    b.style.top = Math.min(snip.sy, y) + "px";
    b.style.width = Math.abs(x - snip.sx) + "px";
    b.style.height = Math.abs(y - snip.sy) + "px";
  }

  function intersect(rect, box) {
    var left = Math.max(rect.left, box.left);
    var top = Math.max(rect.top, box.top);
    var right = Math.min(rect.left + rect.w, box.right);
    var bottom = Math.min(rect.top + rect.h, box.bottom);
    if (right - left < 8 || bottom - top < 8) return null;
    return { left: left, top: top, w: right - left, h: bottom - top };
  }

  function dispatchRect(mode, rect) {
    if (is3d()) {
      var el = document.getElementById("hype-mesh3d");
      var canvas = el && el.querySelector("canvas");
      if (!canvas) { note("The 3D view is not ready to capture.", true); return; }
      var cr = canvas.getBoundingClientRect();
      var r = intersect(rect, cr);
      if (!r) { note("Drag the rectangle over the 3D view.", true); return; }
      var kx = canvas.width / cr.width, ky = canvas.height / cr.height;
      var crop = { x: Math.round((r.left - cr.left) * kx),
                   y: Math.round((r.top - cr.top) * ky),
                   w: Math.round(r.w * kx), h: Math.round(r.h * ky) };
      if (mode === "camera") { capture3dLocal(crop); }
      else {
        post("export_evt", { a: "record_3d", crop: crop,
                             secs: state.secs, fps: state.fps, n: Date.now() });
      }
      return;
    }
    var map = window.__hypeMap;
    var cont = map && map.getContainer();
    if (!cont) { note("The map is not ready to capture.", true); return; }
    var mr = cont.getBoundingClientRect();
    var r2 = intersect(rect, mr);
    if (!r2) { note("Drag the rectangle over the map.", true); return; }
    var b;
    try {
      var p1 = map.containerPointToLatLng([r2.left - mr.left, r2.top - mr.top]);
      var p2 = map.containerPointToLatLng([r2.left + r2.w - mr.left,
                                           r2.top + r2.h - mr.top]);
      b = { west: Math.min(p1.lng, p2.lng), south: Math.min(p1.lat, p2.lat),
            east: Math.max(p1.lng, p2.lng), north: Math.max(p1.lat, p2.lat) };
    } catch (e) { note("The map is not ready to capture.", true); return; }
    post("export_evt", { a: mode === "camera" ? "still_view" : "save_anim",
                         b: b, w: Math.round(r2.w),
                         secs: state.secs, fps: state.fps, n: Date.now() });
  }

  // ---- browser-local 3D capture -------------------------------------------
  function grab3d(crop) {
    var el = document.getElementById("hype-mesh3d");
    var canvas = el && el.querySelector("canvas");
    if (!canvas) return null;
    try {
      // Fresh render right before toDataURL: without preserveDrawingBuffer the
      // buffer is only valid inside the same task as the draw.
      var S = window.__hypeMesh3d;
      if (S && S.rw && S.rw.render) S.rw.render();
      if (!crop) return canvas.toDataURL("image/png");
      var off = document.createElement("canvas");
      off.width = crop.w; off.height = crop.h;
      off.getContext("2d").drawImage(canvas, crop.x, crop.y, crop.w, crop.h,
                                     0, 0, crop.w, crop.h);
      return off.toDataURL("image/png");
    } catch (e) { return null; }
  }
  function capture3dLocal(crop) {
    var url = grab3d(crop);
    if (!url) { note("The 3D view is not ready to capture.", true); return; }
    fetch(url).then(function (r) { return r.blob(); }).then(function (bl) {
      // Auto-copy first, while this is still the click's task (the gesture); a
      // rejection is fine, the preview modal has its own Copy button. The copy
      // outcome rides in the upload's FILE NAME so the modal's "Copied to the
      // clipboard." note stays honest.
      var copy = navigator.clipboard && window.ClipboardItem
        ? navigator.clipboard.write([new ClipboardItem({ "image/png": bl })])
            .then(function () { return true; })
            .catch(function () { return false; })
        : Promise.resolve(false);
      return copy.then(function (copied) { return { bl: bl, copied: copied }; });
    }).then(function (r) {
      var inp = document.getElementById("capture_png");
      if (!inp) { note("The capture input is missing.", true); return; }
      var dt = new DataTransfer();
      dt.items.add(new File([r.bl], r.copied ? "view3d.png" : "view3d-nocopy.png",
                            { type: "image/png" }));
      inp.files = dt.files;
      inp.dispatchEvent(new Event("change", { bubbles: true }));
    }).catch(function () { note("The 3D capture failed in this browser.", true); });
  }

  function capture() {
    // Refresh the server's map_bounds first: the last report can predate a pane
    // or window resize, and a still/video sized from it letterboxes. The refresh
    // rides the same input batch as export_evt, so the server reads it in order.
    if (window.__hypeReportBounds) {
      try { window.__hypeReportBounds(); } catch (e) { /* map not ready */ }
    }
    if (state.target === "view") { startSnip(state.mode); return; }
    if (state.mode === "video") {
      post("export_evt", { a: is3d() ? "record_3d" : "save_anim",
                           secs: state.secs, fps: state.fps, n: Date.now() });
      return;
    }
    if (is3d()) { capture3dLocal(null); return; }
    post("export_evt", { a: "still_view", n: Date.now() });
  }

  // ---- delegated wiring (the control exists from first paint, no init order) --
  document.addEventListener("click", function (ev) {
    var t = ev.target;
    if (!t || !t.closest) return;
    var copyStill = t.closest(".hype-copy-still");
    if (copyStill) {
      var url = copyStill.getAttribute("data-url");
      if (url) {
        fetch(url).then(function (r) { return r.blob(); }).then(function (bl) {
          return navigator.clipboard.write([new ClipboardItem({ "image/png": bl })]);
        }).then(function () { copyStill.textContent = "Copied"; })
          .catch(function () { copyStill.textContent = "Copy failed"; });
      }
      return;
    }
    var root = $("#hype-export");
    if (!root) return;
    if (t.closest("#hype-export-btn")) { openMenu(false); capture(); return; }
    if (t.closest("#hype-export-arrow")) {
      openMenu(!root.classList.contains("open"));
      return;
    }
    var mode = t.closest("#hype-export-menu [data-mode]");
    if (mode) { state.mode = mode.getAttribute("data-mode"); syncUi(); return; }
    var fps = t.closest("#hype-export-menu [data-fps]");
    if (fps) {
      state.fps = Number(fps.getAttribute("data-fps")) === 15 ? 15 : 30;
      syncUi();
      return;
    }
    var target = t.closest("#hype-export-menu .hype-export-target");
    if (target) {
      state.target = target.getAttribute("data-target");
      syncUi();
      openMenu(false);
      return;
    }
    if (t.closest("#hype-export-menu")) return;   // header text etc: keep open
    if (root.classList.contains("open")) openMenu(false);
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") openMenu(false);
  });
  document.addEventListener("change", function (ev) {
    var t = ev.target;
    if (!t || !t.closest || !t.closest('.hype-export-vidset')) return;
    if (t.getAttribute("data-k") === "secs") {
      var v = Math.max(2, Math.min(Math.round(Number(t.value) || 8), 30));
      state.secs = v;
      t.value = v;
    }
  });

  // First paint: the buttons render empty server-side; fill the icons as soon as
  // the DOM exists (script loads in <head>, so wait for it).
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", syncUi);
  } else {
    syncUi();
  }
})();
