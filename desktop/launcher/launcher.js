// HYPE Desktop launcher page. Shown while the shell sets up the payload or starts the app;
// once the app server answers, the shell navigates this same WebView2 to the app itself, so
// this page has no card grid — just setup / starting / error states.
// Outbound commands: { type } — see LauncherProtocol.cs. Update notices are native (shell banner).
(function () {
  "use strict";

  var host = window.chrome && window.chrome.webview;
  if (!host) {
    document.body.insertAdjacentHTML(
      "afterbegin",
      '<p style="padding:1rem;color:#c0392b">This page only works inside HYPE Desktop.</p>');
    return;
  }

  function send(type) {
    host.postMessage(JSON.stringify({ type: type }));
  }

  var startingView = document.getElementById("starting-view");
  var startingMessage = document.getElementById("starting-message");
  var startingDetail = document.getElementById("starting-detail");
  var setupView = document.getElementById("setup-view");
  var setupMessage = document.getElementById("setup-message");
  var setupBar = document.getElementById("setup-bar");
  var setupProgress = document.getElementById("setup-progress");
  var setupDetail = document.getElementById("setup-detail");
  var setupActions = document.getElementById("setup-actions");
  var setupRetry = document.getElementById("setup-retry");
  var footerMode = document.getElementById("footer-mode");

  function show(view) {
    startingView.hidden = view !== "starting";
    setupView.hidden = view !== "setup";
  }

  function showStarting(msg) {
    show("starting");
    startingMessage.textContent = msg.message || "Starting…";
    startingDetail.textContent = msg.detail || "";
  }

  function showSetup(msg) {
    show("setup");
    setupView.classList.remove("error");
    setupMessage.textContent = msg.message || "";
    setupDetail.textContent = msg.detail || "";
    setupActions.hidden = true;
    if (typeof msg.percent === "number" && msg.percent >= 0) {
      setupProgress.hidden = false;
      setupBar.style.width = msg.percent + "%";
    } else {
      setupProgress.hidden = true;
    }
  }

  function showSetupError(msg) {
    show("setup");
    setupView.classList.add("error");
    setupMessage.textContent = msg.message || "Setup failed.";
    setupDetail.textContent = "";
    setupProgress.hidden = true;
    setupActions.hidden = false;
    setupRetry.hidden = msg.canRetry === false;
  }

  // ── Shell → page ───────────────────────────────────────────────────────

  host.addEventListener("message", function (event) {
    var msg = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
    if (!msg || !msg.type) { return; }
    switch (msg.type) {
      case "info":
        footerMode.textContent =
          "HYPE Desktop " + msg.version +
          (msg.mode === "dev" ? "  ·  dev mode (repo .venv)" : "") +
          "  ·  data: " + msg.dataRoot;
        break;
      case "starting":
        showStarting(msg);
        break;
      case "setup":
        showSetup(msg);
        break;
      case "setupError":
        showSetupError(msg);
        break;
    }
  });

  // ── Page → shell ───────────────────────────────────────────────────────

  document.getElementById("open-logs").addEventListener("click", function (e) {
    e.preventDefault();
    send("openLogsFolder");
  });
  setupRetry.addEventListener("click", function () {
    showStarting({ message: "Retrying…" });
    send("setupRetry");
  });
  document.getElementById("setup-from-file").addEventListener("click", function () {
    send("installFromFile");
  });

  send("ready");
})();
