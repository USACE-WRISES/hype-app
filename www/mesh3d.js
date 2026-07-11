/* HYPE 3D mesh viewer (vtk.js, client-side).
 *
 * The server (app.py `_mesh_done`) sends a "hype_mesh" custom message with the decimated grid
 * geometry built in pure NumPy (hype_app/mesh.py) — de-duplicated corner points + active
 * hexahedra (8 point-indices each) + a per-cell layer scalar, all in local metres. This module
 * renders it with vtk.js: orbit/zoom (trackball), a clip-plane slider to slice through and reveal
 * interior layers (X/Y/Z axis), and a vertical-exaggeration slider (groundwater grids are thin).
 * Everything here is client-side — no server round-trip for interaction, and no server rendering.
 *
 * vtk.js is loaded as the monolithic UMD bundle (global `vtk`) from a CDN in app.py's head.
 */
(function () {
  "use strict";

  var CID = "hype-mesh3d";
  window.__hypeMesh3d = null;                              // debug/QA handle (set to S below)
  var S = { grw: null, ren: null, rw: null, mappers: [], actors: [], plane: null,
            clipping: false, axis: 0, t: 0, vexag: 1, bounds: null, bar: null, hint: null,
            scalarBar: null, omw: null,
            drapeActor: null, drapeOpacity: 0.55, drapeReady: false,  // basemap drape (aerial on top)
            labelEls: [], labelAnchors: [], labelPts: null,   // floating boundary labels
            labelOffset: [0, 0],                              // gw_mesh scene offset (labels bake it)
            ctf: null, topMapper: null,                       // elevation coloring (re-rangeable)
            topCellPts: null, topCellElev: null,              // top-cell centers, for range-from-view
            // ---- named-layer scene (hype3d_layer / hype3d_vis) ----
            layers: {},                                       // key -> {actors, mappers, bounds, origin}
            vis: {},                                          // key -> desired visibility (default true)
            origin: null,                                     // scene anchor [X, Y] (first layer wins)
            terrainGeom: null,                                // {pts, polys, x0, y0, w, h} for drapes
            pendingDrapes: {},                                // drapes that arrived before the terrain
            drapeTexReady: {},                                // raster-drape key -> texture loaded (gate vis)
            // ---- viewer tools (wireframe / view presets / measure) ----
            wireframe: false, meshSurfActors: [],             // grid body+top actors (repr toggle)
            projMode: "persp", viewPreset: null,              // "parallel" in top/side presets
            cube: null, cubeBox: null,                        // HTML view cube (camera-synced)
            measure: { armed: false, pts: [], actor: null, mapper: null,
                       labelActor: null, labelEl: null, dist: null } };
  window.__hypeMesh3d = S;

  function container() { return document.getElementById(CID); }
  function V() { return window.vtk; }

  function axisExtent(b, axis) {           // [min,max] of the (vexag-scaled) bounds along an axis
    var lo = b[axis * 2], hi = b[axis * 2 + 1];
    if (axis === 2) { lo *= S.vexag; hi *= S.vexag; }
    return [lo, hi];
  }

  function applyClip() {
    if (!S.mappers || !S.mappers.length || !S.plane || !S.bounds) return;
    var ext = axisExtent(S.bounds, S.axis);
    var origin = [(S.bounds[0] + S.bounds[1]) / 2,
                  (S.bounds[2] + S.bounds[3]) / 2,
                  (S.bounds[4] + S.bounds[5]) / 2 * S.vexag];
    origin[S.axis] = ext[0] + S.t * (ext[1] - ext[0]);
    var normal = [0, 0, 0]; normal[S.axis] = 1;
    S.plane.setOrigin(origin);
    S.plane.setNormal(normal);
    var want = S.t > 0.001;
    if (want && !S.clipping) { S.mappers.forEach(function (m) { m.addClippingPlane(S.plane); }); S.clipping = true; }
    else if (!want && S.clipping) { S.mappers.forEach(function (m) { m.removeClippingPlane(S.plane); }); S.clipping = false; }
  }

  function applyVexag() {
    (S.actors || []).forEach(function (a) { a.setScale(1, 1, S.vexag); });
    // Label anchors are projected from DATASET coords (the pixel-space mapper ignores actor
    // transforms), so bake the exaggeration into the anchor points themselves.
    if (S.labelPts && S.labelAnchors.length) {
      var arr = new Float32Array(S.labelAnchors.length * 3);
      S.labelAnchors.forEach(function (p, i) {
        arr[i * 3] = p[0] + S.labelOffset[0];          // scene offset baked in (pixel-space
        arr[i * 3 + 1] = p[1] + S.labelOffset[1];      // mapper ignores actor transforms)
        arr[i * 3 + 2] = p[2] * S.vexag;
      });
      S.labelPts.setData(arr, 3);
    }
    applyClip();
  }

  function render() { if (S.rw) S.rw.render(); }

  // ---- named-layer registry --------------------------------------------------------------
  // Every visual belongs to a named layer ("gw_mesh", "terrain", "paths", "wse", …). Layers
  // carry their own local-frame origin [X, Y] in the model CRS; the first layer to arrive
  // anchors the scene and later layers are offset by their origin delta (z uses ONE shared
  // datum server-side, so vertical exaggeration keeps everything aligned).

  function layerOffset(origin) {
    if (!S.origin || !origin) return [0, 0];
    return [origin[0] - S.origin[0], origin[1] - S.origin[1]];
  }

  function removeLayer3d(key) {
    var L = S.layers[key];
    if (!L) return;
    L.actors.forEach(function (a) {
      try { S.ren.removeActor(a); } catch (e) { /**/ }
      var i = S.actors.indexOf(a);
      if (i >= 0) S.actors.splice(i, 1);
    });
    L.mappers.forEach(function (m) {
      if (S.clipping) { try { m.removeClippingPlane(S.plane); } catch (e) { /**/ } }
      var i = S.mappers.indexOf(m);
      if (i >= 0) S.mappers.splice(i, 1);
    });
    delete S.layers[key];
    recomputeSceneBounds();
  }

  function applyLayerVis(key) {
    if (key === "basemap") { applyDrapeOpacity(); render(); return; }  // aerial drape (no S.layers entry)
    var L = S.layers[key];
    if (!L) return;
    var on = S.vis[key] !== false;
    // A raster drape must stay hidden until its texture image loads — otherwise the untextured
    // actor renders as a white/translucent plane (color 1,1,1) washing out the terrain.
    if (key in S.drapeTexReady && !S.drapeTexReady[key]) on = false;
    L.actors.forEach(function (a) { a.setVisibility(on); });
    if (key === "gw_mesh") {
      (S.labelEls || []).forEach(function (el) { el.style.display = on ? "" : "none"; });
      applyDrapeOpacity();                      // the aerial drape also gates on texture+opacity
    }
    render();
  }

  function registerLayer3d(key, actors, mappers, bounds, origin) {
    // actors/mappers are ALREADY added to the renderer/global lists by the builders; this
    // records ownership, anchors the scene, applies offsets + visibility, unions bounds.
    var first = S.origin === null;
    if (first && origin) S.origin = [origin[0], origin[1]];
    var off = layerOffset(origin);
    actors.forEach(function (a) {
      a.setPosition(off[0], off[1], 0);
      a.setScale(1, 1, S.vexag);
    });
    if (key === "gw_mesh") S.labelOffset = off;  // pixel-space labels ignore actor transforms
    S.layers[key] = { actors: actors, mappers: mappers, bounds: bounds || null,
                      origin: origin || null };
    if (S.clipping) mappers.forEach(function (m) { try { m.addClippingPlane(S.plane); } catch (e) { /**/ } });
    recomputeSceneBounds();
    applyLayerVis(key);
    if (first) { S.ren.resetCamera(); }
    S.ren.resetCameraClippingRange();
    render();
  }

  function recomputeSceneBounds() {
    var u = null;
    Object.keys(S.layers).forEach(function (k) {
      var L = S.layers[k];
      if (!L.bounds) return;
      var off = layerOffset(L.origin);
      var b = [L.bounds[0] + off[0], L.bounds[1] + off[0],
               L.bounds[2] + off[1], L.bounds[3] + off[1], L.bounds[4], L.bounds[5]];
      if (!u) { u = b.slice(); return; }
      u[0] = Math.min(u[0], b[0]); u[1] = Math.max(u[1], b[1]);
      u[2] = Math.min(u[2], b[2]); u[3] = Math.max(u[3], b[3]);
      u[4] = Math.min(u[4], b[4]); u[5] = Math.max(u[5], b[5]);
    });
    if (u) S.bounds = u;
  }

  // Camera bindings: the stock trackball style keeps left-drag = orbit and wheel = zoom;
  // MIDDLE- and RIGHT-drag panning is implemented here directly (translate the camera and
  // focal point along the view plane by the pixel delta). Handlers run in the CAPTURE phase
  // and stop propagation, because the interactor rotates on ANY move whose `buttons` bit is
  // set — it must never see these drags. (vtkInteractorStyleManipulator would be the tidy
  // way, but in this UMD build it silently swallows all interaction.)
  function setupInteraction(vtk, el) {
    var drag = null;                                     // {x, y} of the last pan position

    function panBy(dx, dy) {
      if (!S.ren || !S.rw) return;
      var cam = S.ren.getActiveCamera();
      var canvas = el.querySelector("canvas");
      var hPx = (canvas && canvas.clientHeight) || el.clientHeight || 700;
      // world units per screen pixel: parallelScale is the half viewport HEIGHT in
      // world units (ortho presets); otherwise perspective at the focal distance
      var upp = (cam.getParallelProjection && cam.getParallelProjection())
        ? 2 * cam.getParallelScale() / hPx
        : 2 * cam.getDistance() *
          Math.tan((cam.getViewAngle() * Math.PI / 180) / 2) / hPx;
      var dop = cam.getDirectionOfProjection();
      var up = cam.getViewUp();
      var right = [dop[1] * up[2] - dop[2] * up[1],      // dop × up
                   dop[2] * up[0] - dop[0] * up[2],
                   dop[0] * up[1] - dop[1] * up[0]];
      var n = Math.hypot(right[0], right[1], right[2]) || 1;
      var mx = -dx * upp, my = dy * upp;
      var move = [right[0] / n * mx + up[0] * my,
                  right[1] / n * mx + up[1] * my,
                  right[2] / n * mx + up[2] * my];
      var fp = cam.getFocalPoint(), pos = cam.getPosition();
      cam.setFocalPoint(fp[0] + move[0], fp[1] + move[1], fp[2] + move[2]);
      cam.setPosition(pos[0] + move[0], pos[1] + move[1], pos[2] + move[2]);
      S.ren.resetCameraClippingRange();
      render();
    }

    el.addEventListener("pointerdown", function (e) {
      if (e.button !== 1 && e.button !== 2) return;      // middle / right only
      if (e.target.closest && e.target.closest(".hype-mesh3d-bar")) return;
      drag = { x: e.clientX, y: e.clientY };
      try { el.setPointerCapture(e.pointerId); } catch (err) { /**/ }
      e.stopPropagation(); e.preventDefault();
    }, true);
    el.addEventListener("pointermove", function (e) {
      if (!drag) return;
      panBy(e.clientX - drag.x, e.clientY - drag.y);
      drag = { x: e.clientX, y: e.clientY };
      e.stopPropagation(); e.preventDefault();
    }, true);
    function endPan(e) {
      if (!drag) return;
      drag = null;
      try { el.releasePointerCapture(e.pointerId); } catch (err) { /**/ }
      e.stopPropagation();
    }
    el.addEventListener("pointerup", endPan, true);
    el.addEventListener("pointercancel", endPan, true);
    // right-drag must not pop the browser menu over the 3D view
    el.addEventListener("contextmenu", function (e) { e.preventDefault(); });
  }

  function applyDrapeOpacity() {
    if (!S.drapeActor) return;
    var v = S.drapeOpacity;
    // Stay hidden until the aerial texture has actually loaded — a visible untextured
    // actor paints as a translucent WHITE sheet that washes out the terrain colours on
    // the first render (the "initial white-transparency" bug). Also gated by the
    // gw_mesh layer's tree checkbox, and hidden entirely in wireframe mode (a textured
    // top would occlude the interior volumes the wireframe exists to reveal).
    // Gated by BOTH the model-grid checkbox (drape needs the mesh under it) and the Basemaps →
    // USGS Imagery checkbox (the drape IS that aerial layer); hidden in wireframe mode.
    var layerOn = S.vis.gw_mesh !== false && S.vis.basemap !== false;
    S.drapeActor.setVisibility(layerOn && S.drapeReady && v > 0.01 && !S.wireframe);
    S.drapeActor.getProperty().setOpacity(v);
  }

  // ---- wireframe -----------------------------------------------------------------------
  // Swap the model-grid surface actors to wireframe so the zone volumes inside are visible;
  // the aerial drape hides and the DEM terrain dims to a ghost while active. Style state,
  // not view state: survives mesh re-sends (buildScene re-applies) and Reset view.
  function applyWireframe() {
    (S.meshSurfActors || []).forEach(function (a) {
      var p = a.getProperty();
      if (S.wireframe) {
        if (p.setRepresentationToWireframe) p.setRepresentationToWireframe();
        if (p.setLighting) p.setLighting(false);       // uniform-brightness lines
      } else {
        if (p.setRepresentationToSurface) p.setRepresentationToSurface();
        if (p.setLighting) p.setLighting(true);
      }
    });
    var terr = S.layers.terrain;
    if (terr) terr.actors.forEach(function (a) {
      a.getProperty().setOpacity(S.wireframe ? 0.25 : 1.0);
    });
    applyDrapeOpacity();
    render();
  }

  // ---- view presets + HTML view cube -----------------------------------------------------
  // Top/side presets switch to PARALLEL projection (exact plan/section geometry — required
  // by the measure tool); Iso restores perspective. World coords are vexag-scaled, so the
  // preset bakes the CURRENT exaggeration into its framing (re-click a face after changing
  // it). parallelScale is set directly — resetCamera() would overwrite it with a sphere fit.
  var PRESETS = {
    top:    { pos: [0, 0, 1],  up: [0, 1, 0] },
    bottom: { pos: [0, 0, -1], up: [0, 1, 0] },
    north:  { pos: [0, 1, 0],  up: [0, 0, 1] },
    south:  { pos: [0, -1, 0], up: [0, 0, 1] },
    east:   { pos: [1, 0, 0],  up: [0, 0, 1] },
    west:   { pos: [-1, 0, 0], up: [0, 0, 1] },
  };

  function setViewPreset(name) {
    if (!S.ren || !S.bounds) return;
    var cam = S.ren.getActiveCamera();
    var b = S.bounds;
    var cx = (b[0] + b[1]) / 2, cy = (b[2] + b[3]) / 2, cz = (b[4] + b[5]) / 2 * S.vexag;
    var dx = b[1] - b[0], dy = b[3] - b[2], dz = (b[5] - b[4]) * S.vexag;
    var D = 2 * Math.hypot(dx, dy, dz) || 100;
    var el = container();
    var aspect = (el.clientWidth || 1) / (el.clientHeight || 1);
    clearMeasure();
    if (name === "iso") {
      var u = [-0.45, -0.9, 0.6], n = Math.hypot(u[0], u[1], u[2]);
      cam.setParallelProjection(false);
      cam.setFocalPoint(cx, cy, cz);
      cam.setPosition(cx + u[0] / n * D, cy + u[1] / n * D, cz + u[2] / n * D);
      cam.setViewUp(0, 0, 1);
      S.projMode = "persp"; S.viewPreset = "iso";
      S.ren.resetCamera();                       // keeps direction/up, refits the distance
    } else {
      var P = PRESETS[name];
      if (!P) return;
      var halfW = (name === "east" || name === "west") ? dy / 2 : dx / 2;
      var halfH = (name === "top" || name === "bottom") ? dy / 2 : dz / 2;
      halfW = Math.max(halfW, 0.5); halfH = Math.max(halfH, 0.5);
      cam.setFocalPoint(cx, cy, cz);
      cam.setPosition(cx + P.pos[0] * D, cy + P.pos[1] * D, cz + P.pos[2] * D);
      cam.setViewUp(P.up[0], P.up[1], P.up[2]);
      cam.setParallelProjection(true);
      cam.setParallelScale(Math.max(halfH, halfW / aspect) * 1.06);
      S.projMode = "parallel"; S.viewPreset = name;
    }
    S.ren.resetCameraClippingRange();
    updateMeasureEnabled();
    syncCubeActive();
    render();
  }

  // Left-drag orbit in a parallel preset silently lies (oblique orthographic with
  // exaggerated z and no depth cue) and would leave the measure tool armed in a view
  // where its plane math no longer means anything — so the first orbit DRAG exits to
  // perspective. Wheel zoom and middle/right pan stay parallel-native.
  function exitParallel() {
    if (S.projMode !== "parallel" || !S.ren) return;
    S.ren.getActiveCamera().setParallelProjection(false);
    S.projMode = "persp"; S.viewPreset = null;
    clearMeasure();
    updateMeasureEnabled();
    syncCubeActive();
    S.ren.resetCameraClippingRange();
    render();
  }

  function shieldFromInteractor(node) {
    // vtk.js rotates on ANY pointermove whose `buttons` bit is set — UI chrome inside the
    // canvas container must stop the whole pointer conversation (see buildBar's note).
    ["pointerdown", "pointermove", "pointerup", "mousedown", "mousemove", "mouseup",
     "touchstart", "touchmove", "touchend", "wheel", "dblclick"].forEach(function (t) {
      node.addEventListener(t, function (e) { e.stopPropagation(); });
    });
  }

  function buildCube() {
    if (S.cube) return;
    var el = container();
    var wrap = document.createElement("div");
    wrap.className = "hype-mesh3d-cube";
    wrap.innerHTML =
      '<div class="hype-cube-box">' +
        '<div class="hype-cube-face face-top" data-view="top"><span>Top</span></div>' +
        '<div class="hype-cube-face face-bottom" data-view="bottom"><span>Bottom</span></div>' +
        '<div class="hype-cube-face face-north" data-view="north"><span>N</span></div>' +
        '<div class="hype-cube-face face-south" data-view="south"><span>S</span></div>' +
        '<div class="hype-cube-face face-east" data-view="east"><span>E</span></div>' +
        '<div class="hype-cube-face face-west" data-view="west"><span>W</span></div>' +
      '</div>' +
      '<button type="button" class="hype-cube-home" data-view="iso" title="Isometric view">Iso</button>';
    shieldFromInteractor(wrap);
    wrap.addEventListener("click", function (e) {
      var t = e.target.closest ? e.target.closest("[data-view]") : null;
      if (t) setViewPreset(t.getAttribute("data-view"));
    });
    el.appendChild(wrap);
    S.cube = wrap;
    S.cubeBox = wrap.querySelector(".hype-cube-box");
    S.ren.getActiveCamera().onModified(syncCube);
    syncCube();
  }

  // Rotate the CSS cube to match the camera. The cube's box space B is world with y
  // NEGATED (CSS y points down), which keeps the box transform a pure rotation
  // (det +1 — a direct world->screen map is a reflection and mirrors every label).
  // Face labels get an in-plane counter-rotation each sync so they stay upright.
  var CUBE_FACE_AXES = {                       // face-local x/y axes in box space B
    top:    { x: [1, 0, 0],  y: [0, 1, 0] },
    bottom: { x: [-1, 0, 0], y: [0, 1, 0] },   // rotateY(180) flips local x
    north:  { x: [1, 0, 0],  y: [0, 0, 1] },   // rotateX(90): local y -> B z
    south:  { x: [1, 0, 0],  y: [0, 0, -1] },
    east:   { x: [0, 0, -1], y: [0, 1, 0] },   // rotateY(90): local x -> B -z
    west:   { x: [0, 0, 1],  y: [0, 1, 0] },
  };

  function syncCube() {
    if (!S.cubeBox || !S.ren) return;
    var cam = S.ren.getActiveCamera();
    var dop = cam.getDirectionOfProjection(), up0 = cam.getViewUp();
    var r = [dop[1] * up0[2] - dop[2] * up0[1],
             dop[2] * up0[0] - dop[0] * up0[2],
             dop[0] * up0[1] - dop[1] * up0[0]];
    var rn = Math.hypot(r[0], r[1], r[2]) || 1;
    r = [r[0] / rn, r[1] / rn, r[2] / rn];
    var u = [r[1] * dop[2] - r[2] * dop[1],
             r[2] * dop[0] - r[0] * dop[2],
             r[0] * dop[1] - r[1] * dop[0]];
    var un = Math.hypot(u[0], u[1], u[2]) || 1;
    u = [u[0] / un, u[1] / un, u[2] / un];
    // columns = images of the BOX axes: Bx = world x, By = world -y, Bz = world z;
    // screen rows are [r; -u; -dop] (CSS y down), so col2 is negated once more.
    var c1 = [r[0], -u[0], -dop[0]];
    var c2 = [-r[1], u[1], dop[1]];
    var c3 = [r[2], -u[2], -dop[2]];
    var m = [c1[0], c1[1], c1[2], 0,
             c2[0], c2[1], c2[2], 0,
             c3[0], c3[1], c3[2], 0,
             0, 0, 0, 1];
    S.cubeBox.style.transform = "translateZ(-42px) matrix3d(" + m.join(",") + ")";
    // keep each face's label upright: rotate the span against the face's in-plane spin
    var cols = [c1, c2, c3];
    function toCss(v) {
      return [v[0] * cols[0][0] + v[1] * cols[1][0] + v[2] * cols[2][0],
              v[0] * cols[0][1] + v[1] * cols[1][1] + v[2] * cols[2][1]];
    }
    S.cube.querySelectorAll(".hype-cube-face").forEach(function (f) {
      var ax = CUBE_FACE_AXES[f.getAttribute("data-view")];
      var span = f.querySelector("span");
      if (!ax || !span) return;
      var vx = toCss(ax.x);
      span.style.transform = "rotate(" + (-Math.atan2(vx[1], vx[0])) + "rad)";
    });
  }

  function syncCubeActive() {
    if (!S.cube) return;
    S.cube.querySelectorAll("[data-view]").forEach(function (f) {
      f.classList.toggle("active", f.getAttribute("data-view") === S.viewPreset);
    });
  }

  // ---- measure tool (parallel presets only) ---------------------------------------------
  function updateMeasureEnabled() {
    if (!S.bar) return;
    var btn = S.bar.querySelector('[data-k="measure"]');
    if (!btn) return;
    var ok = S.projMode === "parallel";
    btn.disabled = !ok && !S.measure.actor;
    btn.title = ok ? "Measure a distance (two clicks; Esc cancels)"
                   : "Measuring needs a Top or side view — click the view cube";
    btn.classList.toggle("active", !!S.measure.armed);
  }

  // Screen -> world on the camera's focal plane. Exact in parallel projection: the
  // near->far ray is perpendicular to the view plane, so any shared depth gives the same
  // in-plane delta. Uses the view's displayToWorld (aspect handled internally) with the
  // same device-pixel scaling the label callback proved out.
  function screenToFocalPlane(clientX, clientY) {
    var el = container(), canvas = el && el.querySelector("canvas");
    if (!canvas || !S.rw || !S.ren) return null;
    var rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    var x = (clientX - rect.left) * (canvas.width / rect.width);
    var y = (rect.height - (clientY - rect.top)) * (canvas.height / rect.height);
    var view = S.rw.getViews()[0];
    if (!view || !view.displayToWorld) return null;
    var near = view.displayToWorld(x, y, 0, S.ren);
    var far = view.displayToWorld(x, y, 1, S.ren);
    var cam = S.ren.getActiveCamera();
    var hit = V().Common.DataModel.vtkPlane.intersectWithLine(
      near, far, cam.getFocalPoint(), cam.getDirectionOfProjection());
    return (hit && hit.intersection) ? [hit.x[0], hit.x[1], hit.x[2]] : null;
  }

  function fmtDist(m) {
    if (m >= 1000) return (m / 1000).toFixed(2) + " km";
    return (m >= 100 ? m.toFixed(1) : m.toFixed(2)) + " m";
  }

  function armMeasure() {
    if (S.measure.actor || S.measure.armed) {   // second press = clear
      clearMeasure();
      render();
      return;
    }
    if (S.projMode !== "parallel") return;
    S.measure.armed = true;
    S.measure.pts = [];
    var el = container();
    if (el) el.classList.add("hype-measuring");
    showHint("Click two points to measure (Esc cancels).");
    updateMeasureEnabled();
  }

  function clearMeasure() {
    var M = S.measure;
    if (!M) return;
    var had = M.armed || M.actor;
    M.armed = false; M.pts = []; M.dist = null;
    var el = container();
    if (el) el.classList.remove("hype-measuring");
    if (M.actor) { try { S.ren.removeActor(M.actor); } catch (e) { /**/ } M.actor = null; M.mapper = null; }
    if (M.labelActor) { try { S.ren.removeActor(M.labelActor); } catch (e) { /**/ } M.labelActor = null; }
    if (M.labelEl && M.labelEl.parentNode) M.labelEl.parentNode.removeChild(M.labelEl);
    M.labelEl = null;
    if (had && S.hint && /measure|Esc/.test(S.hint.textContent || "")) showHint("");
    updateMeasureEnabled();
  }

  // Draw the finished measurement: a world-anchored line nudged toward the camera (screen-
  // invisible in ortho, wins the depth test over terrain/mesh) + a pixel-space label chip
  // at the midpoint. The picked points carry vexag-SCALED z, so true meters divide dz out.
  // Deliberately NOT in S.actors/S.layers: no vexag rescale, no clip slicing, no registry.
  function drawMeasure() {
    var vtk = V(), M = S.measure;
    var p1 = M.pts[0], p2 = M.pts[1];
    var dzTrue = (p2[2] - p1[2]) / (S.vexag || 1);
    var dxy = Math.hypot(p2[0] - p1[0], p2[1] - p1[1]);
    M.dist = Math.hypot(p2[0] - p1[0], p2[1] - p1[1], dzTrue);

    var cam = S.ren.getActiveCamera();
    var dop = cam.getDirectionOfProjection();
    var b = S.bounds || [0, 1, 0, 1, 0, 1];
    var lift = 0.02 * (Math.hypot(b[1] - b[0], b[3] - b[2], (b[5] - b[4]) * S.vexag) || 1);
    var q1 = [p1[0] - dop[0] * lift, p1[1] - dop[1] * lift, p1[2] - dop[2] * lift];
    var q2 = [p2[0] - dop[0] * lift, p2[1] - dop[1] * lift, p2[2] - dop[2] * lift];

    var pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from([q1[0], q1[1], q1[2], q2[0], q2[1], q2[2]]), 3);
    pd.getLines().setData(Uint32Array.from([2, 0, 1]));
    var mapper = vtk.Rendering.Core.vtkMapper.newInstance();
    mapper.setInputData(pd);
    var actor = vtk.Rendering.Core.vtkActor.newInstance();
    actor.setMapper(mapper);
    actor.getProperty().setColor(1.0, 0.584, 0.0);          // #ff9500 — the app's select amber
    actor.getProperty().setLineWidth(3);
    if (actor.getProperty().setLighting) actor.getProperty().setLighting(false);
    S.ren.addActor(actor);
    M.actor = actor; M.mapper = mapper;

    var el = container();
    var chip = document.createElement("div");
    chip.className = "hype-mesh3d-measure";
    chip.textContent = fmtDist(M.dist) +
      (Math.abs(dzTrue) > 0.05
        ? "  (Δxy " + fmtDist(dxy) + " · Δz " + fmtDist(Math.abs(dzTrue)) + ")"
        : "");
    el.appendChild(chip);
    M.labelEl = chip;
    if (vtk.Rendering.Core.vtkPixelSpaceCallbackMapper) {
      var mid = [(q1[0] + q2[0]) / 2, (q1[1] + q2[1]) / 2, (q1[2] + q2[2]) / 2];
      var lpd = vtk.Common.DataModel.vtkPolyData.newInstance();
      lpd.getPoints().setData(Float32Array.from(mid), 3);
      var lm = vtk.Rendering.Core.vtkPixelSpaceCallbackMapper.newInstance();
      lm.setInputData(lpd);
      lm.setCallback(function (coords) {
        if (!coords || !coords.length || !M.labelEl) return;
        var canvas = el.querySelector("canvas");
        if (!canvas) return;
        var sx = canvas.clientWidth / (canvas.width || 1);
        var sy = canvas.clientHeight / (canvas.height || 1);
        M.labelEl.style.left = (coords[0][0] * sx) + "px";
        M.labelEl.style.top = (canvas.clientHeight - coords[0][1] * sy) + "px";
      });
      var la = vtk.Rendering.Core.vtkActor.newInstance();
      la.setMapper(lm);
      S.ren.addActor(la);
      M.labelActor = la;
    }
    S.ren.resetCameraClippingRange();
    updateMeasureEnabled();
    render();
  }

  function measurePointerDown(e) {
    if (!S.measure.armed || e.button !== 0) return;
    if (e.target.closest && (e.target.closest(".hype-mesh3d-bar") ||
                             e.target.closest(".hype-mesh3d-cube"))) return;
    e.stopPropagation(); e.preventDefault();
    var w = screenToFocalPlane(e.clientX, e.clientY);
    if (!w) return;
    S.measure.pts.push(w);
    if (S.measure.pts.length >= 2) {
      S.measure.armed = false;
      var el = container();
      if (el) el.classList.remove("hype-measuring");
      showHint("");
      drawMeasure();
    }
  }

  // Terrain color ramp over [lo, hi] (values outside clamp to the end colors).
  function rampCtf(ctf, lo, hi) {
    var d = (hi - lo) || 1;
    ctf.removeAllPoints();
    ctf.addRGBPoint(lo, 0.27, 0.45, 0.29);                 // low  — green
    ctf.addRGBPoint(lo + 0.40 * d, 0.55, 0.60, 0.32);      //      — yellow-green
    ctf.addRGBPoint(lo + 0.65 * d, 0.80, 0.74, 0.46);      //      — tan
    ctf.addRGBPoint(lo + 0.85 * d, 0.58, 0.44, 0.32);      //      — brown
    ctf.addRGBPoint(hi, 0.95, 0.95, 0.92);                 // high — near-white
  }

  function setElevInputs(lo, hi) {
    if (!S.bar) return;
    var mn = S.bar.querySelector('[data-k="emin"]');
    var mx = S.bar.querySelector('[data-k="emax"]');
    if (mn) mn.value = Math.round(lo * 10) / 10;
    if (mx) mx.value = Math.round(hi * 10) / 10;
  }

  // Re-range the elevation legend: recolor the top surface + scalar bar over [lo, hi].
  function applyElevRange(lo, hi) {
    if (!S.ctf || !S.topMapper) return;
    if (!(hi > lo)) hi = lo + 0.1;
    rampCtf(S.ctf, lo, hi);
    S.topMapper.setScalarRange(lo, hi);
    if (S.scalarBar && S.scalarBar.setScalarsToColors) S.scalarBar.setScalarsToColors(S.ctf);
    setElevInputs(lo, hi);
    render();
  }

  // Elevation range of the top-layer cells currently VISIBLE: project each top-cell center
  // to normalized display coords (vertical exaggeration + active slice plane respected;
  // occlusion ignored) and range over those inside the viewport. Null when none land
  // on screen — e.g. hovering over an inactive part of the grid.
  function visibleElevRange() {
    if (!S.topCellPts || !S.ren || typeof S.ren.worldToNormalizedDisplay !== "function") {
      return null;
    }
    var lo = Infinity, hi = -Infinity;
    var n = S.topCellElev.length;
    for (var i = 0; i < n; i++) {
      var x = S.topCellPts[i * 3], y = S.topCellPts[i * 3 + 1];
      var z = S.topCellPts[i * 3 + 2] * S.vexag;
      if (S.clipping && S.plane && S.plane.evaluateFunction([x, y, z]) < 0) continue;
      var d = S.ren.worldToNormalizedDisplay(x, y, z);
      if (!d || d[0] < 0 || d[0] > 1 || d[1] < 0 || d[1] > 1 || d[2] < 0 || d[2] > 1) continue;
      var e = S.topCellElev[i];
      if (e < lo) lo = e;
      if (e > hi) hi = e;
    }
    return lo <= hi ? [lo, hi] : null;
  }

  function clearLabels() {
    (S.labelEls || []).forEach(function (el) { if (el.parentNode) el.parentNode.removeChild(el); });
    S.labelEls = []; S.labelAnchors = []; S.labelPts = null;
  }

  // Floating name chips anchored to each boundary line's midpoint, projected to screen space
  // every render by vtkPixelSpaceCallbackMapper (dataset coords; vexag baked in by applyVexag).
  function buildLabels(vtk, boundaries) {
    clearLabels();
    if (!boundaries.length || !vtk.Rendering.Core.vtkPixelSpaceCallbackMapper) return;
    var el = container();
    var pts = [];
    boundaries.forEach(function (b) {
      var n = b.points.length / 3, m = Math.floor(n / 2) * 3;
      S.labelAnchors.push([b.points[m], b.points[m + 1], b.points[m + 2] + 1.5]);
      pts.push(b.points[m], b.points[m + 1], b.points[m + 2] + 1.5);
      var chip = document.createElement("div");
      chip.className = "hype-mesh3d-label";
      chip.textContent = b.name;
      chip.style.color = b.color;
      chip.style.borderColor = b.color;
      el.appendChild(chip);
      S.labelEls.push(chip);
    });
    var pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from(pts), 3);
    S.labelPts = pd.getPoints();
    var mapper = vtk.Rendering.Core.vtkPixelSpaceCallbackMapper.newInstance();
    mapper.setInputData(pd);
    mapper.setCallback(function (coords) {
      if (!coords || !S.labelEls.length) return;
      var canvas = el.querySelector("canvas");
      if (!canvas) return;
      var sx = canvas.clientWidth / (canvas.width || 1);
      var sy = canvas.clientHeight / (canvas.height || 1);
      for (var i = 0; i < S.labelEls.length && i < coords.length; i++) {
        var c = coords[i];
        S.labelEls[i].style.left = (c[0] * sx) + "px";
        S.labelEls[i].style.top = (canvas.clientHeight - c[1] * sy) + "px";
      }
    });
    var actor = vtk.Rendering.Core.vtkActor.newInstance();
    actor.setMapper(mapper);
    S.ren.addActor(actor);
    S.actors.push(actor);                    // removed with the mesh on rebuild (scale is a no-op)
  }

  // Colored polylines riding the top of each boundary's cells (data z pre-lifted server-side).
  function buildBoundaryLines(vtk, boundaries) {
    boundaries.forEach(function (b) {
      var n = b.points.length / 3;
      if (n < 2) return;
      var pd = vtk.Common.DataModel.vtkPolyData.newInstance();
      pd.getPoints().setData(Float32Array.from(b.points), 3);
      var line = new Uint32Array(n + 1);
      line[0] = n;
      for (var i = 0; i < n; i++) line[i + 1] = i;
      pd.getLines().setData(line);
      var mapper = vtk.Rendering.Core.vtkMapper.newInstance();
      mapper.setInputData(pd);
      var actor = vtk.Rendering.Core.vtkActor.newInstance();
      actor.setMapper(mapper);
      var rgb = [parseInt(b.color.slice(1, 3), 16) / 255,
                 parseInt(b.color.slice(3, 5), 16) / 255,
                 parseInt(b.color.slice(5, 7), 16) / 255];
      actor.getProperty().setColor(rgb[0], rgb[1], rgb[2]);
      actor.getProperty().setLineWidth(4);
      if (actor.getProperty().setLighting) actor.getProperty().setLighting(false);
      S.ren.addActor(actor);
      S.actors.push(actor); S.mappers.push(mapper);
    });
  }

  // Drape the aerial basemap onto the TOP faces: same face quads, points lifted slightly and
  // given texture coordinates spanning the basemap's local extent. vtk.js uploads DOM images
  // with WebGL's Y-flip, so v runs south→north ((y-y0)/Ly).
  function buildDrape(vtk, msg, topPolys, ptsData) {
    S.drapeActor = null;
    S.drapeReady = false;
    var bm = msg.basemap;
    if (!bm || !topPolys.length) return;
    var lift = 0.25;
    var remap = {}, pts2 = [], tc = [], polys2 = [];
    var lx = (bm.x1 - bm.x0) || 1, ly = (bm.y1 - bm.y0) || 1;
    for (var i = 0; i < topPolys.length; i += 5) {
      polys2.push(4);
      for (var j = 1; j <= 4; j++) {
        var pid = topPolys[i + j];
        var nid = remap[pid];
        if (nid === undefined) {
          nid = pts2.length / 3;
          remap[pid] = nid;
          var x = ptsData[pid * 3], y = ptsData[pid * 3 + 1], z = ptsData[pid * 3 + 2];
          pts2.push(x, y, z + lift);
          tc.push((x - bm.x0) / lx, (y - bm.y0) / ly);
        }
        polys2.push(nid);
      }
    }
    var pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from(pts2), 3);
    pd.getPolys().setData(Uint32Array.from(polys2));
    pd.getPointData().setTCoords(vtk.Common.Core.vtkDataArray.newInstance(
      { name: "tc", values: Float32Array.from(tc), numberOfComponents: 2 }));
    var mapper = vtk.Rendering.Core.vtkMapper.newInstance();
    mapper.setInputData(pd);
    mapper.setScalarVisibility(false);
    var actor = vtk.Rendering.Core.vtkActor.newInstance();
    actor.setMapper(mapper);
    actor.getProperty().setColor(1, 1, 1);
    if (actor.getProperty().setLighting) actor.getProperty().setLighting(false);
    actor.setVisibility(false);            // revealed only once the texture image is in (below)
    var texture = vtk.Rendering.Core.vtkTexture.newInstance();
    texture.setInterpolate(true);
    var img = new Image();
    img.onload = function () {
      try {
        texture.setImage(img);
        S.drapeReady = true;               // texture present → the actor's first VISIBLE render
        applyDrapeOpacity();               // (here) already carries the aerial, never white
        render();
      } catch (e) { console.error("[mesh3d] drape texture failed", e); }
    };
    img.src = bm.url;
    actor.addTexture(texture);
    S.ren.addActor(actor);
    S.actors.push(actor); S.mappers.push(mapper);
    S.drapeActor = actor;                  // NOT shown yet — no applyDrapeOpacity() until onload
  }

  function buildBar() {
    if (S.bar) return;
    var bar = document.createElement("div");
    bar.className = "hype-mesh3d-bar";
    bar.innerHTML =
      '<label>Slice <select data-k="axis"><option value="0">X</option>' +
      '<option value="1">Y</option><option value="2">Z</option></select></label>' +
      '<label><input type="range" data-k="clip" min="0" max="1" step="0.01" value="0"></label>' +
      '<label>Vert × <input type="range" data-k="vexag" min="1" max="5" step="1" value="1">' +
      '<span data-k="vexagval">1</span></label>' +
      '<label>Basemap <input type="range" data-k="bmop" min="0" max="1" step="0.05" value="0.55"></label>' +
      '<label>Elev <input type="number" data-k="emin" step="0.5" title="Legend minimum (m)"> – ' +
      '<input type="number" data-k="emax" step="0.5" title="Legend maximum (m)"> m</label>' +
      '<button data-k="efromview" title="Range the legend over the terrain currently on screen">' +
      'Fit to view</button>' +
      '<button data-k="measure" disabled ' +
      'title="Measuring needs a Top or side view — click the view cube">Measure</button>' +
      '<button data-k="reset">Reset view</button>';
    bar.addEventListener("change", function (e) {
      var k = e.target.getAttribute("data-k");
      if (k !== "emin" && k !== "emax") return;
      var mn = parseFloat(bar.querySelector('[data-k="emin"]').value);
      var mx = parseFloat(bar.querySelector('[data-k="emax"]').value);
      if (isFinite(mn) && isFinite(mx)) applyElevRange(mn, mx);
    });
    bar.addEventListener("input", function (e) {
      var k = e.target.getAttribute("data-k");
      if (k === "axis") { S.axis = parseInt(e.target.value, 10); S.t = 0;
                          bar.querySelector('[data-k="clip"]').value = 0; applyClip(); }
      else if (k === "clip") { S.t = parseFloat(e.target.value); applyClip(); }
      else if (k === "vexag") { S.vexag = parseFloat(e.target.value);
                                bar.querySelector('[data-k="vexagval"]').textContent = S.vexag;
                                clearMeasure();     // stored world points would silently mis-scale
                                applyVexag(); if (S.ren) S.ren.resetCameraClippingRange(); }
      else if (k === "bmop") { S.drapeOpacity = parseFloat(e.target.value); applyDrapeOpacity(); }
      render();
    });
    // The bar sits INSIDE the vtk container, whose interactor handles pointer events for the
    // trackball camera. Stopping the press alone is NOT enough: vtk.js rotates on any
    // pointermove whose `buttons` bit is set, no prior pointerdown needed — and a native
    // slider drag implicitly captures the pointer, so every drag move retargets to the
    // slider and bubbles up through the bar. Stop the whole pointer conversation here.
    shieldFromInteractor(bar);
    bar.addEventListener("click", function (e) {
      var key = e.target.getAttribute("data-k");
      if (key === "efromview") {
        var rng = visibleElevRange();
        if (rng) { applyElevRange(rng[0], rng[1]); }
        else {
          showHint("No terrain in view — pan/zoom over the mesh, then try again.");
          setTimeout(function () { showHint(""); }, 2500);
        }
        return;
      }
      if (key === "measure") { armMeasure(); return; }
      if (key !== "reset") return;
      S.axis = 0; S.t = 0; S.vexag = 1;                    // reset slice + vertical-exaggeration state
      var q = function (k) { return bar.querySelector('[data-k="' + k + '"]'); };
      if (q("axis")) q("axis").value = "0";
      if (q("clip")) q("clip").value = 0;
      if (q("vexag")) q("vexag").value = 1;
      if (q("vexagval")) q("vexagval").textContent = "1";
      clearMeasure();                                      // view state resets; wireframe (style) survives
      S.projMode = "persp"; S.viewPreset = null;
      syncCubeActive(); updateMeasureEnabled();
      applyVexag();                                        // re-scale z→1 + applyClip() (t=0 drops the plane)
      if (S.ren) {
        S.ren.getActiveCamera().setParallelProjection(false);
        S.ren.resetCamera(); S.ren.resetCameraClippingRange();
      }
      render();
    });
    container().appendChild(bar);
    S.bar = bar;
  }

  function showHint(text) {
    var el = container();
    if (!el) return;
    if (!S.hint) {
      S.hint = document.createElement("div");
      S.hint.className = "hype-mesh3d-hint";
      el.appendChild(S.hint);
    }
    S.hint.textContent = text || "";
    S.hint.style.display = text ? "block" : "none";
  }

  // Guidance in the (otherwise blank dark) overlay before any 3D content exists.
  function idleHint() {
    if (container() && !S.grw) {
      showHint("Nothing in 3D yet — fetch terrain (Terrain ▸ DEM) or compute the model grid " +
               "(Groundwater ▸ Model grid), then toggle 3D view.");
    }
  }

  function initOnce() {
    if (S.grw) return true;
    var vtk = V(), el = container();
    if (!vtk || !el) return false;
    S.grw = vtk.Rendering.Misc.vtkGenericRenderWindow.newInstance({ background: [0.05, 0.07, 0.09] });
    S.grw.setContainer(el);
    S.ren = S.grw.getRenderer();
    S.rw = S.grw.getRenderWindow();
    setupInteraction(vtk, el);
    buildBar();
    buildCube();
    updateMeasureEnabled();
    // Measure tool: capture-phase so an armed left click never reaches the trackball
    // interactor (the container's capture handlers fire before the canvas's bubble ones).
    el.addEventListener("pointerdown", measurePointerDown, true);
    el.addEventListener("pointermove", function (e) {
      if (S.measure.armed && (e.buttons & 1)) { e.stopPropagation(); e.preventDefault(); }
    }, true);
    el.addEventListener("pointerup", function (e) {
      if (S.measure.armed && e.button === 0) e.stopPropagation();
    }, true);
    // First left-drag ORBIT while in a parallel preset exits to perspective (see exitParallel).
    el.addEventListener("pointermove", function (e) {
      if (S.projMode !== "parallel" || !(e.buttons & 1) || S.measure.armed) return;
      if (e.target.closest && (e.target.closest(".hype-mesh3d-bar") ||
                               e.target.closest(".hype-mesh3d-cube"))) return;
      exitParallel();                      // no stopPropagation — the orbit continues in perspective
    }, true);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && (S.measure.armed || S.measure.actor)) {
        clearMeasure();
        render();
      }
    });
    try {
      var ro = new ResizeObserver(function () { try { S.grw.resize(); render(); } catch (e) { /**/ } });
      ro.observe(el);
    } catch (e) { window.addEventListener("resize", function () { try { S.grw.resize(); } catch (_) {} }); }
    return true;
  }

  function buildScene(msg) {
    if (!initOnce()) { showHint("3D viewer failed to load."); return; }
    var vtk = V();
    showHint("");
    removeLayer3d("gw_mesh");                 // replace the mesh layer only — others persist
    S.drapeActor = null;
    S.meshSurfActors = [];                    // re-captured by addMesh; wireframe re-applies below
    clearLabels();
    var a0 = S.actors.length, m0 = S.mappers.length;   // builders below append to the globals

    // This vtk.js build ships vtkPolyData (not vtkUnstructuredGrid), so expand each active hex
    // (8 corner ids: 0-3 = bottom face, 4-7 = top face) to its 6 quad faces — clips cleanly on a cut.
    // Split into two meshes: the TOP layer (terrain-coloured by elevation) and the deeper BODY (a
    // neutral-gray block), so the ground surface shows topography while the block below stays quiet.
    var src = msg.cells, elevArr = msg.cellElev, layArr = msg.cellLayer, nHex = msg.nHex;
    var topArr = msg.cellTop;                               // 1 = column's shallowest DRAWN cell
    var FACES = [[0, 1, 2, 3], [4, 5, 6, 7], [0, 1, 5, 4],
                 [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]];
    var topPolys = [], topElev = [], bodyPolys = [];
    var topCellPts = [], topCellElev = [];                  // one center + elev per TOP CELL
    for (var h = 0; h < nHex; h++) {
      var base = h * 8, ev = elevArr[h], isTop = topArr ? topArr[h] === 1 : layArr[h] === 0;
      if (isTop) {                                          // top-face center (corner ids 4..7)
        var sx = 0, sy = 0, sz = 0;
        for (var c = 4; c < 8; c++) {
          var pid = src[base + c] * 3;
          sx += msg.points[pid]; sy += msg.points[pid + 1]; sz += msg.points[pid + 2];
        }
        topCellPts.push(sx / 4, sy / 4, sz / 4);
        topCellElev.push(ev);
      }
      for (var f = 0; f < 6; f++) {
        var fc = FACES[f];
        var q = [4, src[base + fc[0]], src[base + fc[1]], src[base + fc[2]], src[base + fc[3]]];
        if (isTop) { topPolys.push(q[0], q[1], q[2], q[3], q[4]); topElev.push(ev); }
        else { bodyPolys.push(q[0], q[1], q[2], q[3], q[4]); }
      }
    }
    var ptsData = Float32Array.from(msg.points);
    S.topCellPts = Float32Array.from(topCellPts);
    S.topCellElev = Float32Array.from(topCellElev);

    // Terrain colour ramp over the elevation range (full resolution; applied to the top mesh
    // only). Kept in S so the bar's min/max inputs + "set from view" can re-range it live.
    var rng = msg.elevRange || [0, 1], elo = rng[0], ehi = rng[1];
    var ctf = vtk.Rendering.Core.vtkColorTransferFunction.newInstance();
    rampCtf(ctf, elo, ehi);
    S.ctf = ctf;
    S.topMapper = null;                                     // set by addMesh (scalars mesh)

    function addMesh(polysArr, scalars, rgb) {
      if (!polysArr.length) return;
      var pd = vtk.Common.DataModel.vtkPolyData.newInstance();
      pd.getPoints().setData(ptsData, 3);
      pd.getPolys().setData(Uint32Array.from(polysArr));
      var mapper = vtk.Rendering.Core.vtkMapper.newInstance();
      mapper.setInputData(pd);
      if (scalars) {
        pd.getCellData().setScalars(vtk.Common.Core.vtkDataArray.newInstance(
          { name: "elev", values: Float32Array.from(scalars), numberOfComponents: 1 }));
        mapper.setScalarVisibility(true);
        if (mapper.setScalarModeToUseCellData) mapper.setScalarModeToUseCellData();
        mapper.setLookupTable(ctf);
        mapper.setScalarRange(elo, ehi);
        S.topMapper = mapper;                               // legend re-ranging hooks in here
      } else {
        mapper.setScalarVisibility(false);
      }
      var actor = vtk.Rendering.Core.vtkActor.newInstance();
      actor.setMapper(mapper);
      var prop = actor.getProperty();
      if (rgb) prop.setColor(rgb[0], rgb[1], rgb[2]);
      if (prop.setEdgeVisibility) { prop.setEdgeVisibility(true); prop.setEdgeColor(0.16, 0.18, 0.22); }
      S.ren.addActor(actor);
      S.actors.push(actor); S.mappers.push(mapper);
      S.meshSurfActors.push(actor);            // wireframe toggle flips these representations
    }
    addMesh(bodyPolys, null, [0.56, 0.58, 0.61]);           // neutral-gray body
    addMesh(topPolys, topElev, null);                       // terrain-coloured top surface
    buildDrape(vtk, msg, topPolys, ptsData);                // aerial basemap on the top faces
    buildBoundaryLines(vtk, msg.boundaries || []);          // Upstream / FPL / Downstream lines
    buildLabels(vtk, msg.boundaries || []);                 // floating name chips

    // Elevation legend (scalar bar) — created once; its colour map tracks the current mesh.
    // Pinned to the middle-upper right (automated layout spans the full right edge, where its
    // NaN swatch used to collide with the bottom-right orientation gizmo).
    if (!S.scalarBar && vtk.Rendering.Core.vtkScalarBarActor) {
      S.scalarBar = vtk.Rendering.Core.vtkScalarBarActor.newInstance();
      if (S.scalarBar.setAxisLabel) S.scalarBar.setAxisLabel("Elevation (m)");
      if (S.scalarBar.setDrawNanAnnotation) S.scalarBar.setDrawNanAnnotation(false);
      if (S.scalarBar.setAutomated) S.scalarBar.setAutomated(false);
      if (S.scalarBar.setBoxPosition) S.scalarBar.setBoxPosition([0.86, -0.38]);   // NDC (-1..1)
      if (S.scalarBar.setBoxSize) S.scalarBar.setBoxSize([0.13, 1.16]);
      S.ren.addActor(S.scalarBar);
    }
    if (S.scalarBar && S.scalarBar.setScalarsToColors) S.scalarBar.setScalarsToColors(ctf);
    setElevInputs(elo, ehi);                                // a fresh mesh resets the legend range

    // X/Y/Z orientation gizmo (bottom-right), created once.
    if (!S.omw && vtk.Interaction.Widgets.vtkOrientationMarkerWidget && S.rw.getInteractor) {
      var axes = vtk.Rendering.Core.vtkAxesActor.newInstance();
      S.omw = vtk.Interaction.Widgets.vtkOrientationMarkerWidget.newInstance(
        { actor: axes, interactor: S.rw.getInteractor() });
      S.omw.setEnabled(true);
      var Corners = vtk.Interaction.Widgets.vtkOrientationMarkerWidget.Corners;
      S.omw.setViewportCorner(Corners ? Corners.BOTTOM_RIGHT : 1);
      S.omw.setViewportSize(0.15);
      if (S.omw.setMinPixelSize) S.omw.setMinPixelSize(80);
    }

    if (!S.plane) {
      S.plane = vtk.Common.DataModel.vtkPlane.newInstance({ normal: [1, 0, 0], origin: [0, 0, 0] });
    }
    S.t = 0;                                   // slice off; applyClip below reconciles planes
    if (S.bar) S.bar.querySelector('[data-k="clip"]').value = 0;
    registerLayer3d("gw_mesh", S.actors.slice(a0), S.mappers.slice(m0),
                    msg.bounds, msg.origin || null);
    applyVexag();
    applyWireframe();                          // toggle state survives mesh re-sends
    try { S.grw.resize(); } catch (e) { /**/ }
    // Frame the mesh ONCE (so the first preview is visible); a REGENERATE must not re-zoom the
    // camera — it keeps whatever view the user set. Per the app-wide rule: only an explicit
    // zoom/view control moves the camera, never a completed operation.
    if (!S.meshFramed) { S.ren.resetCamera(); S.meshFramed = true; }
    render();
  }

  // ---- generic scene layers (hype3d_layer) -------------------------------------------------

  // Regular-grid terrain surface: data = {nx, ny, x0, y0, dx, dy, z:[...null for nodata],
  // zRange:[lo,hi], origin:[X,Y]}. Row 0 = SOUTH. Point-data elevation colors via the shared
  // terrain ramp; sits 0.05 m below true z so a computed mesh top never z-fights it.
  function buildTerrain(msg) {
    if (!initOnce()) return;
    var vtk = V();
    showHint("");
    removeLayer3d("terrain");
    var nx = msg.nx, ny = msg.ny, z = msg.z;
    var pts = new Float32Array(nx * ny * 3);
    var scal = new Float32Array(nx * ny);
    var i, j, k = 0;
    for (j = 0; j < ny; j++) {
      for (i = 0; i < nx; i++, k++) {
        var zv = z[k];
        pts[k * 3] = msg.x0 + i * msg.dx;
        pts[k * 3 + 1] = msg.y0 + j * msg.dy;
        pts[k * 3 + 2] = (zv === null || zv === undefined) ? 0 : zv - 0.05;
        scal[k] = (zv === null || zv === undefined) ? NaN : zv;
      }
    }
    var polys = [];
    for (j = 0; j < ny - 1; j++) {
      for (i = 0; i < nx - 1; i++) {
        var p0 = j * nx + i, p1 = p0 + 1, p2 = p0 + nx + 1, p3 = p0 + nx;
        if (isNaN(scal[p0]) || isNaN(scal[p1]) || isNaN(scal[p2]) || isNaN(scal[p3])) continue;
        polys.push(4, p0, p1, p2, p3);
      }
    }
    if (!polys.length) return;
    var pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(pts, 3);
    pd.getPolys().setData(Uint32Array.from(polys));
    pd.getPointData().setScalars(vtk.Common.Core.vtkDataArray.newInstance(
      { name: "elev", values: scal, numberOfComponents: 1 }));
    var rng = msg.zRange || [0, 1];
    var ctf = vtk.Rendering.Core.vtkColorTransferFunction.newInstance();
    rampCtf(ctf, rng[0], rng[1]);
    var mapper = vtk.Rendering.Core.vtkMapper.newInstance();
    mapper.setInputData(pd);
    mapper.setScalarVisibility(true);
    mapper.setLookupTable(ctf);
    mapper.setScalarRange(rng[0], rng[1]);
    var actor = vtk.Rendering.Core.vtkActor.newInstance();
    actor.setMapper(mapper);
    S.ren.addActor(actor);
    S.actors.push(actor); S.mappers.push(mapper);
    S.terrainGeom = { nx: nx, ny: ny, x0: msg.x0, y0: msg.y0, dx: msg.dx, dy: msg.dy,
                      pts: pts, polys: polys, scal: scal, origin: msg.origin || null };
    var b = [msg.x0, msg.x0 + (nx - 1) * msg.dx, msg.y0, msg.y0 + (ny - 1) * msg.dy,
             rng[0] - 1, rng[1] + 1];
    registerLayer3d("terrain", [actor], [mapper], b, msg.origin || null);
    if (S.wireframe) applyWireframe();                     // rebuilt terrain keeps the ghost dim
    Object.keys(S.pendingDrapes).forEach(function (k2) {   // drapes that were waiting on us
      var d = S.pendingDrapes[k2];
      delete S.pendingDrapes[k2];
      buildDrapeLayer(k2, d);
    });
  }

  // 3-D polylines (flow paths, etc.): data = {polylines: [[x,y,z,...], ...], color: "#hex",
  // width, origin:[X,Y]} — ONE polydata for all lines.
  function buildLines3d(key, msg) {
    if (!initOnce()) return;
    var vtk = V();
    removeLayer3d(key);
    var lines = msg.polylines || [];
    if (!lines.length) return;
    var pts = [], conn = [];
    lines.forEach(function (flat) {
      var n = flat.length / 3, base = pts.length / 3;
      if (n < 2) return;
      conn.push(n);
      for (var i2 = 0; i2 < n; i2++) {
        pts.push(flat[i2 * 3], flat[i2 * 3 + 1], flat[i2 * 3 + 2]);
        conn.push(base + i2);
      }
    });
    var pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from(pts), 3);
    pd.getLines().setData(Uint32Array.from(conn));
    var mapper = vtk.Rendering.Core.vtkMapper.newInstance();
    mapper.setInputData(pd);
    var actor = vtk.Rendering.Core.vtkActor.newInstance();
    actor.setMapper(mapper);
    var c = msg.color || "#08306b";
    actor.getProperty().setColor(parseInt(c.slice(1, 3), 16) / 255,
                                 parseInt(c.slice(3, 5), 16) / 255,
                                 parseInt(c.slice(5, 7), 16) / 255);
    actor.getProperty().setLineWidth(msg.width || 2);
    if (actor.getProperty().setLighting) actor.getProperty().setLighting(false);
    S.ren.addActor(actor);
    S.actors.push(actor); S.mappers.push(mapper);
    var bx = [Infinity, -Infinity, Infinity, -Infinity, Infinity, -Infinity];
    for (var p = 0; p < pts.length; p += 3) {
      bx[0] = Math.min(bx[0], pts[p]); bx[1] = Math.max(bx[1], pts[p]);
      bx[2] = Math.min(bx[2], pts[p + 1]); bx[3] = Math.max(bx[3], pts[p + 1]);
      bx[4] = Math.min(bx[4], pts[p + 2]); bx[5] = Math.max(bx[5], pts[p + 2]);
    }
    registerLayer3d(key, [actor], [mapper], bx, msg.origin || null);
  }

  // Translucent closed volume (hyporheic-zone shells): data = {points:[x,y,z,...],
  // polys:[4,a,b,c,d,...] (vtk count-prefixed quads — the terrain grid's convention),
  // color:"#hex", opacity, origin:[X,Y]}. No depth sorting needed: vtk.js's forward pass
  // wires order-independent translucency automatically when translucent actors exist
  // (WebGL1 falls back to add-order blending — fine for mostly-disjoint shells).
  // Backface culling stays OFF so a clip-slider cut shows the interior back wall
  // instead of a hole (the renderer's default two-sided lighting shades it).
  function buildVolume(key, msg) {
    if (!initOnce()) return;
    var vtk = V();
    removeLayer3d(key);
    var pts = msg.points || [], polys = msg.polys || [];
    if (!pts.length || !polys.length) return;
    var pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from(pts), 3);
    pd.getPolys().setData(Uint32Array.from(polys));
    var mapper = vtk.Rendering.Core.vtkMapper.newInstance();
    mapper.setInputData(pd);
    mapper.setScalarVisibility(false);
    var actor = vtk.Rendering.Core.vtkActor.newInstance();
    actor.setMapper(mapper);
    var c = msg.color || "#0d9488";
    var prop = actor.getProperty();
    prop.setColor(parseInt(c.slice(1, 3), 16) / 255,
                  parseInt(c.slice(3, 5), 16) / 255,
                  parseInt(c.slice(5, 7), 16) / 255);
    prop.setOpacity(msg.opacity != null ? msg.opacity : 0.35);
    if (prop.setBackfaceCulling) prop.setBackfaceCulling(false);
    if (prop.setSpecular) prop.setSpecular(0.1);
    S.ren.addActor(actor);
    S.actors.push(actor); S.mappers.push(mapper);   // rides vexag scaling + the clip slider
    var bx = [Infinity, -Infinity, Infinity, -Infinity, Infinity, -Infinity];
    for (var p = 0; p < pts.length; p += 3) {
      bx[0] = Math.min(bx[0], pts[p]); bx[1] = Math.max(bx[1], pts[p]);
      bx[2] = Math.min(bx[2], pts[p + 1]); bx[3] = Math.max(bx[3], pts[p + 1]);
      bx[4] = Math.min(bx[4], pts[p + 2]); bx[5] = Math.max(bx[5], pts[p + 2]);
    }
    registerLayer3d(key, [actor], [mapper], bx, msg.origin || null);
  }

  // Raster drape on the TERRAIN surface: data = {url (PNG data URI), x0, y0, x1, y1 (local
  // metres), lift, opacity, origin:[X,Y]}. Reuses the terrain grid geometry, clipped to the
  // drape extent, with texture coords spanning it (v runs south→north — WebGL Y-flip).
  function buildDrapeLayer(key, msg) {
    if (!initOnce()) return;
    if (!S.terrainGeom) { S.pendingDrapes[key] = msg; return; }
    var vtk = V();
    removeLayer3d(key);
    var g = S.terrainGeom;
    // The drape extent is in ITS origin's frame; the terrain grid is in the terrain's frame —
    // rebase the extent into the terrain frame so the texture lands where it belongs.
    var dOff = [0, 0];
    if (msg.origin && g.origin) {
      dOff = [msg.origin[0] - g.origin[0], msg.origin[1] - g.origin[1]];
    }
    var x0 = msg.x0 + dOff[0], x1 = msg.x1 + dOff[0];
    var y0 = msg.y0 + dOff[1], y1 = msg.y1 + dOff[1];
    var lx = (x1 - x0) || 1, ly = (y1 - y0) || 1;
    var lift = msg.lift || 0.35;
    var pts2 = [], tc = [], polys2 = [], remap = {};
    for (var q = 0; q < g.polys.length; q += 5) {
      var ids = [g.polys[q + 1], g.polys[q + 2], g.polys[q + 3], g.polys[q + 4]];
      var inside = false;
      for (var m2 = 0; m2 < 4; m2++) {
        var px = g.pts[ids[m2] * 3], py = g.pts[ids[m2] * 3 + 1];
        if (px >= x0 && px <= x1 && py >= y0 && py <= y1) { inside = true; break; }
      }
      if (!inside) continue;
      polys2.push(4);
      for (var m3 = 0; m3 < 4; m3++) {
        var pid = ids[m3], nid = remap[pid];
        if (nid === undefined) {
          nid = pts2.length / 3;
          remap[pid] = nid;
          var gx = g.pts[pid * 3], gy = g.pts[pid * 3 + 1], gz = g.pts[pid * 3 + 2];
          pts2.push(gx, gy, gz + 0.05 + lift);
          tc.push((gx - x0) / lx, (gy - y0) / ly);
        }
        polys2.push(nid);
      }
    }
    if (!polys2.length) return;
    var pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from(pts2), 3);
    pd.getPolys().setData(Uint32Array.from(polys2));
    pd.getPointData().setTCoords(vtk.Common.Core.vtkDataArray.newInstance(
      { name: "tc", values: Float32Array.from(tc), numberOfComponents: 2 }));
    var mapper = vtk.Rendering.Core.vtkMapper.newInstance();
    mapper.setInputData(pd);
    mapper.setScalarVisibility(false);
    var actor = vtk.Rendering.Core.vtkActor.newInstance();
    actor.setMapper(mapper);
    actor.getProperty().setColor(1, 1, 1);
    actor.getProperty().setOpacity(msg.opacity != null ? msg.opacity : 0.8);
    if (actor.getProperty().setLighting) actor.getProperty().setLighting(false);
    actor.setVisibility(false);                 // shown once the texture image loads (below)
    S.drapeTexReady[key] = false;               // gate: applyLayerVis keeps it hidden until onload
    var texture = vtk.Rendering.Core.vtkTexture.newInstance();
    texture.setInterpolate(true);
    var img = new Image();
    img.onload = function () {
      try {
        texture.setImage(img);
        S.drapeTexReady[key] = true;            // texture bound -> the gate now allows visibility
        applyLayerVis(key);                     // shows iff the tree checkbox is on; also renders
      } catch (e) { console.error("[mesh3d] drape texture failed", e); }
    };
    img.src = msg.url;
    actor.addTexture(texture);
    S.ren.addActor(actor);
    S.actors.push(actor); S.mappers.push(mapper);
    registerLayer3d(key, [actor], [mapper], null, g.origin || null);  // applyLayerVis honors the gate
  }

  function containerVisible() {
    var el = container();
    return !!(el && el.offsetWidth > 0);
  }

  function buildLayerNow(msg) {
    loadVtk(function () {
      try {
        if (msg.kind === "terrain") buildTerrain(msg.data);
        else if (msg.kind === "lines3d") buildLines3d(msg.key, msg.data);
        else if (msg.kind === "drape") buildDrapeLayer(msg.key, msg.data);
        else if (msg.kind === "volume") buildVolume(msg.key, msg.data);
      } catch (e) {
        console.error("[mesh3d] layer build failed", msg.key, e);
        if (isContextError(e)) handleBuildFailure(msg, "layer");
      }
    });
  }

  function flushPendingLayers() {
    var keys = Object.keys(S.pendingLayers || {});
    if (!keys.length) return;
    keys.forEach(function (k) {
      var m = S.pendingLayers[k];
      delete S.pendingLayers[k];
      buildLayerNow(m);
    });
  }

  // --- deferred build + failure recovery -------------------------------------------------------
  // Never initialize vtk against a hidden / 0-size #hype-mesh3d container: vtk sizes its canvas to
  // the container and get3DContext returns null at 0x0 -> the whole viewer throws and (previously)
  // stayed permanently stuck. Both the mesh (onMessage) and layer (onLayerMessage) paths stash their
  // payloads while hidden; one shared watcher builds them on the first reveal -- mesh FIRST (it
  // creates the render window) then the layers (which reuse it). A context failure resets + retries.
  function isContextError(e) {
    return /proxy|context|webgl|get3DContext/i.test(String((e && e.message) || e || ""));
  }

  function buildSceneNow(msg) {
    loadVtk(function () {
      try { buildScene(msg); S.buildFails = 0; }
      catch (e) {
        console.error("[mesh3d] build failed", e);
        if (isContextError(e)) handleBuildFailure(msg, "mesh");
        else showHint("3D view couldn't start — recompute the grid.");
      }
    });
  }

  function flushPending() {
    if (S.pendingMesh) {
      var mm = S.pendingMesh;
      S.pendingMesh = null;
      buildSceneNow(mm);                       // creates S.grw + the gw_mesh layer against a valid context
      if (S.pendingMesh) return;               // mesh re-stashed itself (failed) -> defer layers to the retry
    }
    flushPendingLayers();                      // layers reuse the good render window
  }

  function armPendingWatch() {
    if (S.pendingWatch) return;
    S.pendingWatch = setInterval(function () {
      if (!containerVisible()) return;         // still hidden -> keep waiting (cheap no-op tick)
      clearInterval(S.pendingWatch);
      S.pendingWatch = null;
      flushPending();
    }, 500);
  }

  function resetRenderWindow() {
    // Tear a half-initialized render window down so the next attempt rebuilds cleanly.
    try { if (S.grw && S.grw.delete) S.grw.delete(); } catch (e) { /**/ }
    S.grw = null; S.ren = null; S.rw = null;
    S.actors = []; S.mappers = []; S.layers = {}; S.origin = null; S.meshFramed = false;
    var el = container();
    if (el) { var cs = el.querySelectorAll("canvas"); for (var i = 0; i < cs.length; i++) cs[i].remove(); }
  }

  function handleBuildFailure(msg, kind) {
    resetRenderWindow();
    S.buildFails = (S.buildFails || 0) + 1;
    if (S.buildFails <= 3) {                    // transient (e.g. built mid-reveal) -> re-stash + retry
      if (kind === "mesh") S.pendingMesh = msg;
      else if (msg && msg.key) { S.pendingLayers = S.pendingLayers || {}; S.pendingLayers[msg.key] = msg; }
      showHint("Preparing 3D view…");
      armPendingWatch();
    } else {
      showHint("3D view couldn't start — switch to 2D and back, or recompute the grid.");
    }
  }

  function onLayerMessage(msg) {
    // While the 3D canvas is hidden (2D mode), stash payloads instead of building — creating a GL
    // context in a display:none overlay wastes memory and has crashed constrained tabs. The shared
    // watcher (armPendingWatch) builds the mesh + all layers on the first reveal.
    if (!containerVisible()) {
      S.pendingLayers = S.pendingLayers || {};
      S.pendingLayers[msg.key] = msg;
      armPendingWatch();
      return;
    }
    buildLayerNow(msg);
  }

  function onVisMessage(msg) {
    if (!msg || !msg.key) return;
    S.vis[msg.key] = msg.on !== false;
    if (msg.key in (S.pendingDrapes || {})) return;   // applies when it builds
    applyLayerVis(msg.key);
  }

  function onClearMessage() {                          // New run → empty the whole scene
    Object.keys(S.layers).forEach(removeLayer3d);
    clearLabels();
    clearMeasure();
    S.terrainGeom = null; S.pendingDrapes = {}; S.origin = null; S.vis = {};
    S.pendingLayers = {}; S.pendingMesh = null; S.buildFails = 0; S.drapeTexReady = {};
    if (S.pendingWatch) { clearInterval(S.pendingWatch); S.pendingWatch = null; }
    S.drapeActor = null; S.topMapper = null; S.topCellPts = null; S.topCellElev = null;
    S.wireframe = false; S.meshSurfActors = [];
    S.projMode = "persp"; S.viewPreset = null;
    if (S.ren) { try { S.ren.getActiveCamera().setParallelProjection(false); } catch (e) { /**/ } }
    syncCubeActive();
    updateMeasureEnabled();
    render();
    idleHint();
  }

  // vtk.js is the monolithic UMD at the package root (modern builds dropped dist/vtk.js). Load it on
  // demand with the page's AMD loader (jupyter-widgets' RequireJS) temporarily disabled, so the UMD
  // exports to window.vtk instead of registering as an anonymous AMD module.
  var VTK_URL = "https://cdn.jsdelivr.net/npm/vtk.js@36.2.1/vtk.js";
  function loadVtk(cb) {
    if (window.vtk) { cb(); return; }
    S.loadCbs = S.loadCbs || [];
    S.loadCbs.push(cb);
    if (S.loading) return;
    S.loading = true;
    showHint("Loading 3D viewer…");
    var savedDefine = window.define;
    try { window.define = undefined; } catch (e) { /**/ }
    var s = document.createElement("script");
    s.src = VTK_URL;
    s.onload = function () {
      window.define = savedDefine; S.loading = false;
      var cbs = S.loadCbs; S.loadCbs = [];
      cbs.forEach(function (f) { try { f(); } catch (e) { console.error(e); } });
    };
    s.onerror = function () {
      window.define = savedDefine; S.loading = false; S.loadCbs = [];
      showHint("Could not load the 3D library (vtk.js) from the CDN.");
    };
    document.head.appendChild(s);
  }

  function onMessage(msg) {
    // Defer until the 3D canvas is visible/sized. Building against the hidden (0x0) container makes
    // vtk's get3DContext return null and throw; the shared watcher builds it on the first reveal.
    S.buildFails = 0;                          // fresh compute -> fresh retry budget
    if (!containerVisible()) {
      S.pendingMesh = msg;
      armPendingWatch();
      return;
    }
    buildSceneNow(msg);
  }

  // QA/debug handle: drive the scene from the console (fake payloads, presets, vis) —
  // the live-verification recipes use this; it is NOT part of the app's message flow.
  S.debug = {
    pushLayer: onLayerMessage,
    vis: onVisMessage,
    clear: onClearMessage,
    setPreset: function (name) { setViewPreset(name); },
    measure: function () { return S.measure; },
    toFocalPlane: function (cx, cy) { return screenToFocalPlane(cx, cy); },
    wire: function (on) { S.wireframe = !!on; applyWireframe(); return S.wireframe; },
  };

  function register() {
    if (!(window.Shiny && Shiny.addCustomMessageHandler)) return false;
    // shiny:connected fires again on every reconnect, and Shiny's addCustomMessageHandler
    // THROWS on a duplicate type. Registering the set without isolation lets that throw abort
    // the pass partway, leaving later handlers (hype3d_wire — the wireframe control) dead.
    // Per-handler try/catch registers every type independently, correct whether Shiny keeps
    // handlers across a reconnect (dupes throw, harmlessly caught) or clears them (all re-add).
    function add(name, fn) {
      try { Shiny.addCustomMessageHandler(name, fn); } catch (e) { /* already registered */ }
    }
    add("hype_mesh", onMessage);
    add("hype3d_layer", onLayerMessage);
    add("hype3d_vis", onVisMessage);
    add("hype3d_clear", onClearMessage);
    // Model-grid pane checkbox (app.py grid_wireframe) — the sole wireframe control
    add("hype3d_wire", function (msg) {
      S.wireframe = !!(msg && msg.on);
      applyWireframe();
    });
    return true;
  }
  if (!register()) document.addEventListener("shiny:connected", register);
  document.addEventListener("shiny:connected", register);   // re-assert on every reconnect
  document.addEventListener("shiny:connected", idleHint);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", idleHint);
  else idleHint();
})();
