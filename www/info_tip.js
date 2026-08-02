/* Help tooltips for .hype-info-tip (app.py _info_tip).
 *
 * The app used to lean on the native `title` attribute. That renders as OS chrome after
 * roughly a second, on a 14px target, in a style the page cannot touch -- which reads as
 * "the tooltip is broken" long before anyone calls it ugly. This replaces it with one
 * shared chip in the same dark family as the map tooltips (.hype-probe-tip et al).
 *
 * TWO THINGS ARE LOAD-BEARING:
 *   1. The chip lives on document.body at position:fixed. .hype-props-body is
 *      overflow-y:auto (styles.css), so a tip parented inside the pane is clipped at the
 *      pane edge as soon as the anchor is near the bottom.
 *   2. It opens LEFT by default. .hype-props-panel is pinned to the right edge, so a chip
 *      opening right would land off-screen for every tip in the properties pane.
 *
 * Listeners are delegated from document, so nothing needs rewiring when Shiny re-renders
 * a pane. There is exactly one chip element for the whole page.
 */
(function () {
  "use strict";

  var SHOW_MS = 120;   // long enough not to fire while the cursor is just passing through
  var GAP = 10;        // chip-to-icon gap
  var EDGE = 8;        // keep-off-the-viewport-edge margin

  var pop = null, timer = null, current = null;

  function chip() {
    if (!pop) {
      pop = document.createElement("div");
      pop.className = "hype-tip-pop";
      pop.setAttribute("role", "tooltip");
      document.body.appendChild(pop);
    }
    return pop;
  }

  function place(el) {
    var p = chip(), r = el.getBoundingClientRect();
    // Measured while the chip is still hidden (visibility:hidden keeps its layout box), so
    // there is no one-frame flash at the origin.
    p.style.left = "0px";
    p.style.top = "0px";
    var w = p.offsetWidth, h = p.offsetHeight;
    var vw = document.documentElement.clientWidth;
    var vh = document.documentElement.clientHeight;

    var left = r.left - GAP - w;
    if (left < EDGE) {                       // no room on the left: flip, then clamp
      left = (r.right + GAP + w <= vw - EDGE) ? r.right + GAP
                                             : Math.max(EDGE, vw - EDGE - w);
    }
    var top = r.top + r.height / 2 - h / 2;
    top = Math.min(Math.max(EDGE, top), Math.max(EDGE, vh - EDGE - h));

    p.style.left = Math.round(left) + "px";
    p.style.top = Math.round(top) + "px";
  }

  function show(el) {
    if (!el || !el.isConnected) return;
    // Two channels, as EASI has: a structured card, or a plain one-line string.
    var card = el.getAttribute("data-tip-html");
    var text = el.getAttribute("data-tip");
    if (!card && !text) return;
    current = el;
    var p = chip();
    if (card) {
      p.innerHTML = card;              // app-generated and escaped in app.py _tip_html
      p.classList.add("is-card");
    } else {
      p.textContent = text;            // plain variant keeps white-space:pre-line for \n
      p.classList.remove("is-card");
    }
    p.classList.remove("is-on");
    place(el);
    p.classList.add("is-on");
  }

  function hide() {
    current = null;
    if (timer) { clearTimeout(timer); timer = null; }
    if (pop) pop.classList.remove("is-on");
  }

  function arm(el) {
    if (current === el) return;
    hide();
    timer = setTimeout(function () { timer = null; show(el); }, SHOW_MS);
  }

  function tipFor(node) {
    return (node && node.closest)
      ? node.closest(".hype-info-tip[data-tip],.hype-info-tip[data-tip-html]") : null;
  }

  document.addEventListener("pointerover", function (e) {
    var el = tipFor(e.target);
    if (el) arm(el);
    else if (current || timer) hide();
  }, true);

  // Keyboard parity: the span carries tabindex, so focus is a first-class way in.
  document.addEventListener("focusin", function (e) {
    var el = tipFor(e.target);
    if (el) show(el); else hide();
  }, true);

  document.addEventListener("focusout", hide, true);
  document.addEventListener("pointerdown", hide, true);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") hide();
  }, true);
  // Capture phase, so this also catches the props pane's own scroll and not just the window.
  window.addEventListener("scroll", hide, true);
  window.addEventListener("wheel", hide, true);
  window.addEventListener("resize", hide);
  window.addEventListener("blur", hide);
  document.documentElement.addEventListener("mouseleave", hide);
  // A Shiny re-render can replace the anchor while the chip is up, which would leave the chip
  // pointing at a detached node. Only drop it when that actually happened: shiny:value fires on
  // every output update, and dismissing a tooltip because some unrelated output redrew would be
  // its own bug.
  document.addEventListener("shiny:value", function () {
    if (current && !current.isConnected) hide();
  }, true);

  window.__hypeInfoTip = {
    show: show,
    hide: hide,
    el: function () { return pop; },
    text: function () { return pop ? pop.textContent : null; },
    visible: function () { return !!(pop && pop.classList.contains("is-on")); },
    rect: function () { return pop ? pop.getBoundingClientRect() : null; }
  };
})();
