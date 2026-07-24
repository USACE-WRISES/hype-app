/* HYPE layer tree (left panel) + floating-panel chrome.
 *
 * The server (app.py `_push_tree_state`) sends a "hype_tree" custom message — a flat, ordered
 * node list ({id, label, parent, depth, group, status, check, disabled} + the selected id) —
 * and this module renders/reconciles it into #hype-tree-body. There are deliberately ZERO
 * Shiny inputs in this DOM: a server-rendered tree would re-register ~35 guarded inputs on
 * every re-render (the remount-misfire footgun documented all over app.py). Events flow back
 * through ONE channel, `tree_event` ({type, id, on, nonce}, event priority):
 *
 *   • select   → row/label click (server moves sel_node; the view stays put)
 *   • check    → visibility checkbox toggle (server cascades group toggles to descendants)
 *   • zoom     → the props header's Zoom-to-extent button (server answers with hype_fly)
 *   • deselect → the properties panel's × button
 *   • mapclear → empty-map click (server deselects unless a layer pick consumed the click)
 *   • ready    → posted on connect so the server (re-)pushes the full tree state
 *
 * Expand/collapse state is CLIENT-owned (this DOM is never re-rendered by Shiny, so it just
 * persists in memory); the whole-panel collapse chevrons are also handled here — BOTH panels
 * start collapsed and a selection change re-opens the props card. The "hype_fly" message
 * animates the map to a node's extent via window.__hypeMap.flyToBounds with padding measured
 * from the live panel widths.
 */
(function () {
  "use strict";

  var nodes = [];              // last payload node list
  var byId = {};               // id -> node (from the last payload)
  // Top-level groups start COLLAPSED on load (client-owned; user expands as needed). reach is a leaf.
  var collapsed = { terrain: true, bnd: true, sw: true, gw: true, base: true };
  var sig = "";                // id-list signature of the rendered rows

  function post(type, extra) {
    if (!(window.Shiny && Shiny.setInputValue)) return;
    var msg = { type: type, nonce: Date.now() };
    if (extra) for (var k in extra) msg[k] = extra[k];
    Shiny.setInputValue("tree_event", msg, { priority: "event" });
  }

  // --- dead-widget detector -----------------------------------------------------------
  // jupyter-widgets' embed manager console.error()s "Could not process update msg for
  // model id: <id>" when an update targets a widget whose comm-open never materialized —
  // the wedge that leaves map layers alive server-side but permanently invisible (hit
  // live 2026-07-15: everything after the reach stage was missing while the run itself
  // was fine). Sniff that exact message and nudge the server once per cooldown;
  // app.py's _widget_heal rebuilds the layers as fresh widgets (dead models cannot be
  // revived). Installed at script load so it precedes the widget libraries.
  var dwTimer = null;
  var dwLast = 0;
  var dwOrigError = console.error;
  console.error = function () {
    try {
      if (String(arguments[0] || "").indexOf(
            "Could not process update msg for model id") !== -1 &&
          !dwTimer && Date.now() - dwLast > 30000) {
        dwTimer = setTimeout(function () {   // debounce: one nudge per error burst
          dwTimer = null;
          dwLast = Date.now();
          if (window.Shiny && Shiny.setInputValue) {
            Shiny.setInputValue("hype_widget_dead", Date.now(), { priority: "event" });
          }
        }, 2000);
      }
    } catch (e) { /* the sniffer must never break console.error itself */ }
    return dwOrigError.apply(console, arguments);
  };

  function body() { return document.getElementById("hype-tree-body"); }

  function hiddenByCollapse(n) {
    var p = n.parent;
    while (p) {
      if (collapsed[p]) return true;
      var pn = byId[p];
      p = pn ? pn.parent : null;
    }
    return false;
  }

  function rowHtml(n) {
    var el = document.createElement("div");
    el.className = "hype-tree-row";
    el.setAttribute("data-node", n.id);
    el.style.paddingLeft = (8 + n.depth * 16) + "px";

    var caret = document.createElement("span");
    caret.className = "hype-tree-caret";
    if (n.group) caret.setAttribute("data-caret", "1");
    el.appendChild(caret);

    if (n.check !== null && n.check !== undefined) {
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "hype-tree-check";
      cb.setAttribute("data-check", "1");
      el.appendChild(cb);
    } else {
      var sp = document.createElement("span");
      sp.className = "hype-tree-nocheck";
      el.appendChild(sp);
    }

    var st = document.createElement("span");
    st.className = "hype-tree-status";
    el.appendChild(st);

    var lb = document.createElement("span");
    lb.className = "hype-tree-label";
    el.appendChild(lb);
    return el;
  }

  function patchRow(el, n, selected) {
    el.classList.toggle("selected", n.id === selected);
    el.classList.toggle("disabled", !!n.disabled);
    el.classList.toggle("group", !!n.group);
    el.classList.toggle("veiled", !!n.dim);    // checked but hidden by an unchecked ancestor
    el.classList.toggle("hidden", hiddenByCollapse(n));
    var caret = el.querySelector(".hype-tree-caret");
    if (caret) {                              // drawn chevron (CSS ::before), not a font glyph
      caret.classList.toggle("is-group", !!n.group);
      caret.classList.toggle("open", !!n.group && !collapsed[n.id]);
    }
    var cb = el.querySelector(".hype-tree-check");
    if (cb) cb.checked = !!n.check;
    var st = el.querySelector(".hype-tree-status");
    if (st) st.className = "hype-tree-status s-" + (n.status || "none");
    var lb = el.querySelector(".hype-tree-label");
    if (lb && lb.textContent !== n.label) lb.textContent = n.label;
  }

  function render(payload) {
    var host = body();
    if (!host) return;
    nodes = payload.nodes || [];
    byId = {};
    nodes.forEach(function (n) { byId[n.id] = n; });
    var ids = nodes.map(function (n) { return n.id; }).join("|");
    if (ids !== sig) {                      // structure changed → rebuild rows
      sig = ids;
      var frag = document.createDocumentFragment();
      nodes.forEach(function (n) { frag.appendChild(rowHtml(n)); });
      host.textContent = "";
      host.appendChild(frag);
    }
    var rows = host.children;
    for (var i = 0; i < rows.length; i++) {
      var n = byId[rows[i].getAttribute("data-node")];
      if (n) patchRow(rows[i], n, payload.selected);
    }
  }

  function refreshCollapse(selected) {      // caret toggled → re-evaluate visibility only
    var host = body();
    if (!host) return;
    var rows = host.children;
    for (var i = 0; i < rows.length; i++) {
      var n = byId[rows[i].getAttribute("data-node")];
      if (n) patchRow(rows[i], n, selected);
    }
  }

  var lastSelected = null;
  var firstPayload = true;

  function syncViewButtons(view) {
    document.querySelectorAll(".hype-view-toggle [data-view]").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-view") === view);
    });
  }

  // Props-card chrome: the card shows only while the propspane output has content. It sits
  // LEFT of the leaflet control column (CSS right: 60px), so the controls never move.
  function syncPropsChrome() {
    var pp = document.getElementById("hype-props-panel");
    if (!pp) return;
    var out = pp.querySelector(".shiny-html-output");
    pp.classList.toggle("has-content", !!(out && out.childElementCount > 0));
  }

  function expandProps() {
    var pp = document.getElementById("hype-props-panel");
    if (pp) pp.classList.remove("collapsed");
    syncPropsChrome();
  }

  function onMessage(payload) {
    var prev = lastSelected;
    lastSelected = payload.selected || null;
    render(payload);
    if (payload.view) syncViewButtons(payload.view);
    // A selection CHANGE (tree click, map pick, run-flow jump) opens the props card; the very
    // first payload never does — the app loads with both panels collapsed until interaction.
    if (!firstPayload && lastSelected && lastSelected !== prev) expandProps();
    firstPayload = false;
    if (payload.fly) flyTo(payload.fly);
  }

  // ---- fly-to with live panel-aware padding ----
  function flyTo(bounds) {
    var map = window.__hypeMap;
    if (!map || !bounds) return;
    var padL = 40, padR = 56;                 // default right pad clears the control column
    var tp = document.getElementById("hype-tree-panel");
    if (tp && !tp.classList.contains("collapsed")) {
      padL = tp.getBoundingClientRect().right + 24;
    }
    var pp = document.getElementById("hype-props-panel");
    if (pp && pp.offsetParent !== null && !pp.classList.contains("collapsed") &&
        pp.getBoundingClientRect().width > 40) {
      padR = (window.innerWidth - pp.getBoundingClientRect().left) + 24;
    }
    // Deferred so the selection's layer updates land BEFORE the animation starts — layer
    // messages applied mid-flyTo are when the jupyter-leaflet client drops views. When the
    // flight ENDS (moveend), tell the server so it can re-assert any views the animation ate;
    // guessing the timing server-side raced the animation and lost.
    setTimeout(function () {
      try {
        // NOTE: no `duration` — this leaflet build NaNs out flyToBounds when it's set; the
        // default auto-computed flight is smooth anyway.
        map.once("moveend", function () { post("flydone"); });
        map.flyToBounds(bounds, { paddingTopLeft: [padL, 70], paddingBottomRight: [padR, 40],
                                  maxZoom: 17 });
      } catch (e) { /* map not ready — the selection still applied */ }
    }, 650);
  }

  // ---- one delegated click handler for the tree ----
  function onTreeClick(e) {
    var row = e.target.closest ? e.target.closest(".hype-tree-row") : null;
    if (!row) return;
    var id = row.getAttribute("data-node");
    var n = byId[id];
    if (!n) return;
    if (e.target.hasAttribute && e.target.hasAttribute("data-check")) {
      post("check", { id: id, on: !!e.target.checked });
      return;                                        // a checkbox click never selects
    }
    if (e.target.hasAttribute && e.target.hasAttribute("data-caret")) {
      collapsed[id] = !collapsed[id];
      refreshCollapse(lastSelected);
      return;
    }
    if (n.group) {                                   // group label: select AND make sure open
      if (collapsed[id]) { collapsed[id] = false; refreshCollapse(lastSelected); }
    }
    post("select", { id: id });
    expandProps();                                   // re-clicking the same row also re-opens
  }

  // ---- empty-map click → deselect (clears the props context) ----
  function drawBusy() {
    // Leaflet.draw sets a draw section's actions bar to display:block while a DRAW is live —
    // those clicks place vertices and must never deselect. Vertex EDITING is deliberately
    // not "busy": clicking away commits the edit through the slot-change machinery and then
    // deselects, which is the desired click-away behavior.
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

  function hookMapClear(tries) {
    var map = window.__hypeMap;                      // set by map_bounds.js once the widget mounts
    if (!map) {
      if ((tries || 0) < 80) setTimeout(function () { hookMapClear((tries || 0) + 1); }, 300);
      return;
    }
    map.on("click", function (e) {
      var t = e.originalEvent && e.originalEvent.target;
      // Clicks on stroke-only vectors (reach / boundary lines / flow paths) are SELECTIONS —
      // their own layer handlers fire; anything else (tiles, rasters, polygon fills) clears.
      if (t && t.tagName === "path" && t.getAttribute("fill") === "none") return;
      var rs = window.__hypeReachState;
      // The Reach step owns map clicks outright (point picks / centerline draw) — and the
      // picking flag alone lags the zoom threshold, so gate on the step. An armed draw
      // (rs.arm) covers the window before Leaflet.draw engages (drawBusy sees it after).
      if (rs && (rs.step === "reach" || rs.picking || rs.arm)) return;
      if (drawBusy()) return;
      // The 2-D ruler (www/measure2d.js) and the cross-section tool (www/xsection.js) own
      // clicks while armed — they add vertices, not deselect (their guard classes also make
      // vectors pointer-events:none).
      if (window.__hypeMeasure2D && window.__hypeMeasure2D.active) return;
      if (window.__hypeXSect && window.__hypeXSect.active) return;
      post("mapclear");
    });
  }

  // ---- floating-panel chrome (collapse chevrons; props panel auto-open/close) ----
  function initChrome() {
    document.addEventListener("click", function (e) {
      var t = e.target;
      if (!t.closest) return;
      if (t.closest(".hype-props-clear")) { post("clearresults"); return; }
      if (t.closest(".hype-props-zoom")) { post("zoom"); return; }
      if (t.closest(".hype-props-close")) { post("deselect"); return; }
      var j = t.closest("[data-jump]");
      if (j) { post("select", { id: j.getAttribute("data-jump") }); expandProps(); return; }
      var vb = t.closest(".hype-view-toggle [data-view]");
      if (vb) { post("view", { view: vb.getAttribute("data-view") }); return; }
      var head = t.closest(".hype-panel-head, .hype-props-head");
      if (head) {
        var panel = head.closest(".hype-tree-panel, .hype-props-panel");
        if (panel) { panel.classList.toggle("collapsed"); syncPropsChrome(); return; }
      }
    });
    var host = body();
    if (host) host.addEventListener("click", onTreeClick);
    // Both panels load OPEN — the first thing a new user sees is the Layers panel and the
    // Get-started card, not a bare map. Either can still be collapsed from its header.
    var pp = document.getElementById("hype-props-panel");
    if (pp) {
      // The card's visibility follows its server-rendered content: empty output (nothing
      // selected) hides the whole card; content shows it (collapsed or open per the user).
      new MutationObserver(syncPropsChrome).observe(pp, { childList: true, subtree: true });
      syncPropsChrome();
    }
    hookMapClear(0);
  }

  function register() {
    if (window.Shiny && Shiny.addCustomMessageHandler) {
      Shiny.addCustomMessageHandler("hype_tree", onMessage);
      Shiny.addCustomMessageHandler("hype_fly", function (msg) { flyTo(msg && msg.bounds); });
      // Tab title mirrors the project name; the page's own title returns when unset.
      // Captured lazily: at script parse the <title> element may not exist yet (this
      // file loads from head_content, ahead of the title tag), so document.title is "".
      var baseTitle = null;
      Shiny.addCustomMessageHandler("hype_doc_title", function (msg) {
        if (baseTitle === null) baseTitle = document.title;
        var t = msg && msg.title;
        document.title = t ? t + " - HYPE" : baseTitle;
      });
      // Gradient-points table: patch the computed cells in place. The table output cannot
      // re-render per keystroke (that would remount the numeric being typed in and drop
      // focus), so the server pushes fresh WSE/Dist/Head strings instead. Rows not in the
      // DOM (just removed, or render still in flight) are skipped — the structural render
      // paints fresh values itself.
      Shiny.addCustomMessageHandler("hype_gpt_cells", function (msg) {
        var cells = (msg && msg.cells) || {};
        Object.keys(cells).forEach(function (uid) {
          var tr = document.querySelector('.hype-gpt-table tr[data-uid="' + uid + '"]');
          if (!tr) return;
          ["wse", "dist", "head"].forEach(function (k) {
            var td = tr.querySelector(".gpt-" + k);
            if (td) td.textContent = String(cells[uid][k]);
          });
          var w = tr.querySelector(".gpt-warn");
          if (w) w.style.display = cells[uid].warn ? "" : "none";
        });
      });
      return true;
    }
    return false;
  }

  function ready() { post("ready"); }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initChrome);
  } else {
    initChrome();
  }
  if (!register()) document.addEventListener("shiny:connected", register);
  document.addEventListener("shiny:connected", ready);
})();
