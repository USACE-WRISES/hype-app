// Desktop shell bridge — present only in HYPE Desktop (WebView2). Relays the app's
// hype_desktop custom messages to the C# shell (native file pickers, window title) and
// forwards the shell's replies back into Shiny. In a plain browser (cloud, dev server)
// window.chrome.webview doesn't exist and this file is a no-op; the app falls back to
// typed-path modals.
//
// Delivery is belt-and-suspenders: the page includes this file via a <script> tag AND the
// shell injects the same source into every document (AddScriptToExecuteOnCreatedDocumentAsync,
// cache-proof) — the __hypeBridgeLoaded flag makes the second arrival a no-op.
//
// Attachment is deliberately paranoid: Shiny.setInputValue does NOT exist until Shiny
// initializes (unlike addCustomMessageHandler, which is static), so a head-parse attempt
// fails; event timing in WebView2 isn't worth betting on either. A short poll guarantees
// attachment; the shiny:connected listener re-asserts after every reconnect (each reconnect
// is a fresh server session that needs the desktop_shell flag again).
//
// Contract notes: postMessage MUST send a STRING — the shell reads messages via
// TryGetWebMessageAsString and throws on raw objects (same as launcher.js). And
// addCustomMessageHandler THROWS on a duplicate type (see mesh3d.js) — always try/catch it.
(function () {
  "use strict";
  if (window.__hypeBridgeLoaded) return;
  window.__hypeBridgeLoaded = true;

  var host = window.chrome && window.chrome.webview;
  if (!host) return;

  var handlerRegistered = false;

  function attach() {
    if (!(window.Shiny && Shiny.setInputValue && Shiny.addCustomMessageHandler)) return false;
    if (!handlerRegistered) {
      try {
        Shiny.addCustomMessageHandler("hype_desktop", function (msg) {
          try {
            host.postMessage(JSON.stringify(msg || {}));
          } catch (e) { /* shell gone mid-flight — the app's fallback still works */ }
        });
      } catch (e) { /* duplicate registration (reconnect) — handler already live */ }
      handlerRegistered = true;
    }
    // Re-sent on every attach: each (re)connected session starts with empty inputs.
    Shiny.setInputValue("desktop_shell", { present: true, nonce: Date.now() },
                        { priority: "event" });
    try { console.log("[hype] desktop bridge attached"); } catch (e) { /* no console */ }
    return true;
  }

  host.addEventListener("message", function (e) {
    var d = e.data || {};
    if (d.type === "projectPathPicked" && window.Shiny && Shiny.setInputValue) {
      Shiny.setInputValue("desktop_pick",
        { purpose: d.purpose || "", path: d.path || null, cancelled: !!d.cancelled,
          nonce: Date.now() },
        { priority: "event" });
    }
  });

  // Poll until Shiny is ready (~20 s cap); listeners handle reconnects thereafter.
  if (!attach()) {
    var tries = 0;
    var timer = setInterval(function () {
      if (attach() || ++tries > 130) clearInterval(timer);
    }, 150);
    document.addEventListener("DOMContentLoaded", attach);
  }
  document.addEventListener("shiny:connected", attach);
})();
