using System.Diagnostics;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using Hype.Desktop.Core;
using Hype.Desktop.Core.Manifest;
using Hype.Desktop.Core.Navigation;
using Hype.Desktop.Core.Payload;
using Hype.Desktop.Core.Processes;

namespace Hype.Desktop;

/// <summary>
/// The one window. A single WebView2 hosts either the launcher page (first-run setup,
/// starting/error states — served from the bundled launcher/ folder via a virtual host) or the
/// running app itself (http://127.0.0.1:&lt;port&gt;/). Unlike STAF's launcher-plus-app-windows
/// split, HYPE is a single app, so the shell boots straight into it; update notices surface in
/// a native banner strip above the WebView (visible over both pages), and installing a payload
/// update swaps back to the launcher page for progress, then relaunches the app.
/// </summary>
internal sealed class MainForm : Form
{
    private static readonly string LauncherAssetsDir = Path.Combine(AppContext.BaseDirectory, "launcher");
    private const string LauncherOrigin = "https://launcher.hype/";

    private readonly ShellServices _services;
    private readonly WebView2 _webView;
    private readonly ShellUpdater _shellUpdater;

    // Native update banner (WebView content can't be trusted to host it once the app page loads).
    private readonly Panel _banner;
    private readonly Label _bannerText;
    private readonly Button _bannerInstall;
    private readonly Button _bannerLater;
    private Func<Task>? _bannerAction;

    private CoreWebView2Environment? _environment;
    private AppSupervisor? _supervisor;
    private AppDescriptor? _app;
    private int? _port;
    private (LatestManifest Manifest, UpdatePlan Plan)? _pendingUpdate;
    private bool _payloadBusy;
    private bool _shutdownComplete;
    private bool _shuttingDown;
    private readonly System.Windows.Forms.Timer _updateTimer;

    public ShellConfig Config => _services.Config;

    public MainForm(ShellServices services)
    {
        _services = services;
        _shellUpdater = new ShellUpdater(services.ShellLog);

        Text = "HYPE Desktop";
        StartPosition = FormStartPosition.CenterScreen;
        Size = new Size(1360, 880);
        MinimumSize = new Size(780, 520);
        ShellIcon.Apply(this);

        _webView = new WebView2 { Dock = DockStyle.Fill };
        Controls.Add(_webView);

        // Banner strip: hidden until an update is ready. Added after the fill control so the
        // WinForms layout engine docks it first (top) and the WebView fills the remainder.
        _bannerText = new Label { AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(10, 8, 10, 8) };
        _bannerInstall = new Button { AutoSize = true, Margin = new Padding(4, 4, 4, 4) };
        _bannerInstall.Click += async (_, _) => { if (_bannerAction is { } act) { await act(); } };
        _bannerLater = new Button { AutoSize = true, Text = "Later", Margin = new Padding(0, 4, 8, 4) };
        _bannerLater.Click += (_, _) => HideBanner();
        var bannerFlow = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.LeftToRight,
            WrapContents = false,
            AutoSize = true,
        };
        bannerFlow.Controls.AddRange([_bannerText, _bannerInstall, _bannerLater]);
        _banner = new Panel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            Visible = false,
            BackColor = Color.FromArgb(255, 248, 225),   // soft amber — matches the app's warn tone
            Padding = new Padding(4, 0, 4, 0),
        };
        _banner.Controls.Add(bannerFlow);
        Controls.Add(_banner);

        _updateTimer = new System.Windows.Forms.Timer { Interval = (int)TimeSpan.FromHours(4).TotalMilliseconds };
        _updateTimer.Tick += (_, _) =>
        {
            _ = BackgroundUpdateCheckAsync();
            _ = BackgroundShellUpdateCheckAsync();
        };

        Load += async (_, _) => await InitializeWebViewAsync();
        FormClosing += OnClosing;
    }

    private async Task InitializeWebViewAsync()
    {
        try
        {
            _environment = await CoreWebView2Environment.CreateAsync(
                browserExecutableFolder: null,
                userDataFolder: Config.WebViewDataDir);
            await _webView.EnsureCoreWebView2Async(_environment);
        }
        catch (Exception ex) when (ex is WebView2RuntimeNotFoundException or InvalidOperationException or System.Runtime.InteropServices.COMException)
        {
            _services.ShellLog.WriteLine($"[shell] webview2 init failed: {ex.Message}");
            MessageBox.Show(this,
                "HYPE Desktop needs the Microsoft WebView2 Runtime (installed with Microsoft Edge on Windows 10/11).\n\n" +
                $"Details: {ex.Message}",
                "HYPE Desktop", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close();
            return;
        }

        var core = _webView.CoreWebView2;
        core.Settings.AreDevToolsEnabled = Config.IsDevMode;
        core.Settings.IsStatusBarEnabled = false;
        core.SetVirtualHostNameToFolderMapping(
            "launcher.hype", LauncherAssetsDir, CoreWebView2HostResourceAccessKind.Allow);

        core.WebMessageReceived += OnWebMessage;
        core.NewWindowRequested += OnNewWindowRequested;
        core.NavigationStarting += OnNavigationStarting;
        core.DownloadStarting += OnDownloadStarting;

        NavigateToLauncher();
    }

    // ── Navigation between the two pages ───────────────────────────────────

    private void NavigateToLauncher() => _webView.CoreWebView2?.Navigate(LauncherOrigin + "index.html");

    private void NavigateToApp(int port) => _webView.CoreWebView2?.Navigate($"http://127.0.0.1:{port}/");

    private void OnNavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs e)
    {
        // The bundled launcher page is always fine.
        if (e.Uri.StartsWith(LauncherOrigin, StringComparison.OrdinalIgnoreCase))
        {
            return;
        }
        var decision = NavigationPolicy.Decide(e.Uri, _port, GetPortMap());
        if (decision.Action == NavAction.Allow)
        {
            return;
        }
        e.Cancel = true;
        Apply(decision);
    }

    private void OnNewWindowRequested(object? sender, CoreWebView2NewWindowRequestedEventArgs e)
    {
        e.Handled = true; // never spawn an uncontrolled WebView2 popup
        Apply(NavigationPolicy.Decide(e.Uri, _port, GetPortMap()));
    }

    private void Apply(NavDecision decision)
    {
        switch (decision.Action)
        {
            case NavAction.OpenExternal when decision.Url is { } url:
                OpenExternal(url);
                break;
            case NavAction.FocusLauncher:
            case NavAction.OpenApp:
                // Single window, single app — "go to the app/home" just means "this window".
                Activate();
                break;
        }
    }

    private IReadOnlyDictionary<int, string> GetPortMap()
    {
        var map = new Dictionary<int, string>();
        if (_app is { } app && _supervisor?.GetState(app.Id) is { Status: AppStatus.Running, Port: { } port })
        {
            map[port] = app.Id;
        }
        return map;
    }

    /// <summary>
    /// Shiny's @render.download responses land here. A real save dialog replaces WebView2's
    /// default silent-download bar; cancel in the dialog cancels the download.
    /// </summary>
    private void OnDownloadStarting(object? sender, CoreWebView2DownloadStartingEventArgs e)
    {
        var deferral = e.GetDeferral();
        try
        {
            using var dialog = new SaveFileDialog
            {
                FileName = Path.GetFileName(e.ResultFilePath),
                Title = "Save from HYPE",
            };
            if (dialog.ShowDialog(this) == DialogResult.OK)
            {
                e.ResultFilePath = dialog.FileName;
                e.Handled = true; // suppress the default download UI
            }
            else
            {
                e.Cancel = true;
            }
        }
        finally
        {
            deferral.Complete();
        }
    }

    // ── Launcher page → shell ──────────────────────────────────────────────

    private async void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        string json;
        try
        {
            json = e.TryGetWebMessageAsString();
        }
        catch (ArgumentException)
        {
            return;
        }

        var command = LauncherProtocol.ParseCommand(json);
        if (command is null)
        {
            _services.ShellLog.WriteLine($"[shell] launcher sent unparseable message: {json}");
            return;
        }

        switch (command.Type)
        {
            case "ready":
                PostInfo();
                if (_payloadBusy)
                {
                    break;      // a setup/update flow owns the page; its progress posts keep coming
                }
                if (_app is { } app && _supervisor?.GetState(app.Id) is { Status: AppStatus.Running, Port: { } port })
                {
                    NavigateToApp(port);
                    break;
                }
                await InitializeAppAsync();
                break;

            case "setupRetry":
                // One retry button, two meanings: payload never resolved → run setup again;
                // the app itself crashed → just relaunch it.
                if (_supervisor is null)
                {
                    await RunFirstRunSetupAsync();
                }
                else
                {
                    await StartAppAsync();
                }
                break;

            case "installFromFile":
                await InstallFromFileAsync();
                break;

            case "openLogsFolder":
                OpenInShell("explorer.exe", Config.LogsDir);
                break;

            // ── App page (www/desktop_bridge.js): project-folder support ──
            // WebMessageReceived fires on the UI thread, so modal dialogs are safe here
            // (same as OnDownloadStarting). Cancel still posts a reply — the app clears
            // its pending-pick state either way.

            case "pickProjectSave":
            {
                using var dialog = new SaveFileDialog
                {
                    Title = "HYPE project",
                    Filter = "HYPE project (*.hype)|*.hype",
                    DefaultExt = "hype",
                    AddExtension = true,
                    OverwritePrompt = true,    // picking an existing .hype = replacing it — warn natively
                    FileName = string.IsNullOrWhiteSpace(command.FileName) ? "Project1.hype" : command.FileName,
                };
                var ok = dialog.ShowDialog(this) == DialogResult.OK;
                Post(new
                {
                    type = "projectPathPicked",
                    purpose = command.Purpose,
                    path = ok ? dialog.FileName : null,
                    cancelled = !ok,
                });
                break;
            }

            case "pickProjectOpen":
            {
                using var dialog = new OpenFileDialog
                {
                    Title = "Open HYPE project",
                    Filter = "HYPE project (*.hype)|*.hype|Project archives (*.hype;*.zip)|*.hype;*.zip",
                    CheckFileExists = true,
                };
                var ok = dialog.ShowDialog(this) == DialogResult.OK;
                Post(new
                {
                    type = "projectPathPicked",
                    purpose = command.Purpose,
                    path = ok ? dialog.FileName : null,
                    cancelled = !ok,
                });
                break;
            }

            case "pickProjectsMultiple":
            {
                using var dialog = new OpenFileDialog
                {
                    Title = "Select HYPE projects to compare",
                    Filter = "HYPE project (*.hype)|*.hype",
                    CheckFileExists = true,
                    Multiselect = true,
                };
                var ok = dialog.ShowDialog(this) == DialogResult.OK;
                Post(new
                {
                    type = "projectPathsPicked",
                    purpose = command.Purpose,
                    paths = ok ? dialog.FileNames : Array.Empty<string>(),
                    cancelled = !ok,
                });
                break;
            }

            case "pickComparisonOpen":
            {
                using var dialog = new OpenFileDialog
                {
                    Title = "Open HYPE comparison",
                    Filter = "HYPE comparison (*.hypecompare)|*.hypecompare",
                    CheckFileExists = true,
                };
                var ok = dialog.ShowDialog(this) == DialogResult.OK;
                Post(new
                {
                    type = "comparisonPathPicked",
                    purpose = command.Purpose,
                    path = ok ? dialog.FileName : null,
                    cancelled = !ok,
                });
                break;
            }

            case "pickComparisonSave":
            {
                using var dialog = new SaveFileDialog
                {
                    Title = "Save HYPE comparison",
                    Filter = "HYPE comparison (*.hypecompare)|*.hypecompare",
                    DefaultExt = "hypecompare",
                    AddExtension = true,
                    OverwritePrompt = true,
                    FileName = string.IsNullOrWhiteSpace(command.FileName)
                        ? "Hydraulic comparison.hypecompare"
                        : command.FileName,
                };
                var ok = dialog.ShowDialog(this) == DialogResult.OK;
                Post(new
                {
                    type = "comparisonPathPicked",
                    purpose = command.Purpose,
                    path = ok ? dialog.FileName : null,
                    cancelled = !ok,
                });
                break;
            }

            case "pickComparisonExport":
            {
                using var dialog = new FolderBrowserDialog
                {
                    Description = "Choose a folder for the comparison export",
                    UseDescriptionForTitle = true,
                    ShowNewFolderButton = true,
                };
                var ok = dialog.ShowDialog(this) == DialogResult.OK;
                Post(new
                {
                    type = "comparisonPathPicked",
                    purpose = command.Purpose,
                    path = ok ? dialog.SelectedPath : null,
                    cancelled = !ok,
                });
                break;
            }

            case "captureView":
            {
                // Header capture control: snapshot the shell's own rendered page (panes
                // and the current animation frame included), auto-copy it to the
                // clipboard like the snipping tool, and hand the app a temp-file path.
                // The app shows its own preview modal with Copy and Save from there.
                if (_webView.CoreWebView2 is not { } captureCore)
                {
                    Post(new { type = "captureDone", ok = false, reason = "no webview" });
                    break;
                }
                try
                {
                    using var ms = new MemoryStream();
                    await captureCore.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, ms);
                    var tempPath = Path.Combine(Path.GetTempPath(), "hype_capture.png");
                    await File.WriteAllBytesAsync(tempPath, ms.ToArray());
                    ms.Position = 0;
                    using var img = Image.FromStream(ms);
                    Clipboard.SetImage(img);    // SetImage copies, so disposing img after is fine
                    Post(new { type = "captureDone", ok = true, path = tempPath });
                }
                catch (Exception ex)
                {
                    _services.ShellLog.WriteLine($"[shell] captureView failed: {ex.Message}");
                    Post(new { type = "captureDone", ok = false, err = ex.Message });
                }
                break;
            }

            case "setTitle":
                Text = string.IsNullOrWhiteSpace(command.Title)
                    ? "HYPE Desktop"
                    : $"{command.Title} — HYPE Desktop";
                break;
        }
    }

    private void PostInfo() => Post(new
    {
        type = "info",
        version = _services.ShellVersion.ToString(3),
        mode = Config.IsDevMode && _services.PayloadManager is null ? "dev" : "installed",
        dataRoot = Config.DataRoot,
    });

    // ── Startup: resolve payload → supervisor → launch the app ─────────────

    private async Task InitializeAppAsync()
    {
        if (_supervisor is not null)
        {
            await StartAppAsync();
            return;
        }

        try
        {
            var payload = _services.Locator.Resolve();
            var manifest = DesktopManifest.Load(payload.ManifestFile);
            _app = manifest.Apps[0];
            _supervisor = _services.SupervisorFactory(manifest.Apps);
            _supervisor.StateChanged += OnAppStateChanged;
            _services.ShellLog.WriteLine($"[shell] app ready to launch (payload manifest {manifest.Version})");
            await RegisterBridgeInjectionAsync(Path.Combine(payload.AppsRoot, _app.Dir, "www", "desktop_bridge.js"));

            await StartAppAsync();

            _ = BackgroundUpdateCheckAsync();
            _ = BackgroundShellUpdateCheckAsync();
            _updateTimer.Start();
        }
        catch (ShellException ex) when (_services.PayloadManager is not null)
        {
            _services.ShellLog.WriteLine($"[shell] payload not ready ({ex.Message}) - starting first-run setup");
            await RunFirstRunSetupAsync();
        }
        catch (ShellException ex)
        {
            // Dev mode with a broken venv — nothing to download; explain instead.
            Post(new { type = "setupError", message = ex.Message, canRetry = false });
        }
    }

    private async Task StartAppAsync()
    {
        if (_supervisor is not { } supervisor || _app is not { } app)
        {
            return;
        }
        Post(new { type = "starting", message = $"Starting {app.Name}…", detail = "" });
        await supervisor.StartAsync(app.Id);
    }

    /// <summary>
    /// Guaranteed delivery of the app↔shell bridge: inject www/desktop_bridge.js into every
    /// document this WebView creates (AddScriptToExecuteOnDocumentCreatedAsync). The served
    /// page also carries a script tag for it, but a stale cached page (webview-data profile)
    /// would silently lose the bridge — and with it the native file dialogs. The script's own
    /// __hypeBridgeLoaded flag makes the duplicate arrival a no-op. Single source of truth:
    /// the app's file, read from the resolved payload.
    /// </summary>
    private bool _bridgeInjected;

    private async Task RegisterBridgeInjectionAsync(string bridgePath)
    {
        if (_bridgeInjected || _webView.CoreWebView2 is not { } core)
        {
            return;
        }
        try
        {
            var source = await File.ReadAllTextAsync(bridgePath);
            await core.AddScriptToExecuteOnDocumentCreatedAsync(source);
            _bridgeInjected = true;
            _services.ShellLog.WriteLine("[shell] desktop bridge injection registered");
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or FileNotFoundException or DirectoryNotFoundException)
        {
            // Non-fatal: the page's own <script> include still delivers the bridge.
            _services.ShellLog.WriteLine($"[shell] bridge injection skipped ({ex.Message})");
        }
    }

    private void OnAppStateChanged(string appId, AppRuntimeState state)
    {
        if (IsDisposed || _shuttingDown)
        {
            return;
        }
        RunOnUi(() =>
        {
            switch (state.Status)
            {
                case AppStatus.Running when state.Port is { } port:
                    _port = port;
                    if (!_payloadBusy)
                    {
                        NavigateToApp(port);
                    }
                    break;

                case AppStatus.Starting:
                    Post(new
                    {
                        type = "starting",
                        message = $"Starting {_app?.Name ?? "HYPE"}…",
                        detail = state.Detail ?? "",
                    });
                    break;

                case AppStatus.Crashed when !_payloadBusy:
                    _port = null;
                    // The app page (if showing) just died with it — swap back to the launcher
                    // page so the user sees what happened and can retry.
                    NavigateToLauncher();
                    Post(new
                    {
                        type = "setupError",
                        message = $"HYPE stopped unexpectedly: {state.Detail ?? "unknown error"}",
                        canRetry = true,
                    });
                    break;
            }
        });
    }

    // ── First-run setup / offline install ──────────────────────────────────

    private async Task RunFirstRunSetupAsync()
    {
        if (_services.PayloadManager is not { } manager || _services.ManifestUrl is not { } manifestUrl || _payloadBusy)
        {
            return;
        }
        _payloadBusy = true;
        manager.Progress += OnPayloadProgress;
        try
        {
            Post(new { type = "setup", message = "Checking what needs to be installed…", percent = -1, detail = "" });
            using var source = _services.SourceFactory();
            var result = await Task.Run(() => manager.CheckAsync(source, manifestUrl, CancellationToken.None));
            switch (result)
            {
                case CheckResult.ShellTooOld tooOld:
                    Post(new
                    {
                        type = "setupError",
                        message = $"This version of HYPE Desktop is too old for the current runtime (needs {tooOld.RequiredShellVersion}). Please install the latest HYPE Desktop from the releases page.",
                        canRetry = false,
                    });
                    break;

                case CheckResult.UpToDate:
                    // Pointer said installed but resolve failed earlier → something is missing on disk.
                    Post(new
                    {
                        type = "setupError",
                        message = "The installed runtime looks damaged. Use 'Install from file…' with an offline bundle, or contact support.",
                        canRetry = true,
                    });
                    break;

                case CheckResult.UpdateAvailable update:
                    await Task.Run(() => manager.ApplyAsync(source, update.Manifest, update.Plan, CancellationToken.None));
                    _payloadBusy = false;
                    await InitializeAppAsync();
                    break;
            }
        }
        catch (Exception ex) when (ex is ShellException or HttpRequestException or IOException or TaskCanceledException)
        {
            _services.ShellLog.WriteLine($"[shell] first-run setup failed: {ex.Message}");
            Post(new
            {
                type = "setupError",
                message = $"Could not download the HYPE runtime: {ex.Message}",
                canRetry = true,
            });
        }
        finally
        {
            manager.Progress -= OnPayloadProgress;
            _payloadBusy = false;
        }
    }

    private async Task InstallFromFileAsync()
    {
        if (_services.PayloadManager is not { } manager || _payloadBusy)
        {
            return;
        }
        using var dialog = new FolderBrowserDialog
        {
            Description = "Select the folder containing the HYPE offline bundle (latest-desktop.json + zip files)",
            UseDescriptionForTitle = true,
        };
        if (dialog.ShowDialog(this) != DialogResult.OK)
        {
            return;
        }

        _payloadBusy = true;
        manager.Progress += OnPayloadProgress;
        try
        {
            await Task.Run(() => manager.InstallFromDirectoryAsync(dialog.SelectedPath, CancellationToken.None));
            _payloadBusy = false;
            await InitializeAppAsync();
        }
        catch (ShellException ex)
        {
            Post(new { type = "setupError", message = ex.Message, canRetry = true });
        }
        finally
        {
            manager.Progress -= OnPayloadProgress;
            _payloadBusy = false;
        }
    }

    private void OnPayloadProgress(PayloadProgress progress) => RunOnUi(() =>
        Post(new
        {
            type = "setup",
            message = progress.Message,
            percent = PercentOf(progress),
            detail = progress.Component is null ? "" : $"{progress.Component} · {progress.BytesDone / 1_000_000} / {Math.Max(1, progress.BytesTotal / 1_000_000)} MB",
        }));

    private static int PercentOf(PayloadProgress p) =>
        p.BytesTotal > 0 ? (int)Math.Clamp(p.BytesDone * 100 / p.BytesTotal, 0, 100) : -1;

    // ── Routine update checks (app already usable) ─────────────────────────

    private async Task BackgroundUpdateCheckAsync()
    {
        if (_services.PayloadManager is not { } manager || _services.ManifestUrl is not { } manifestUrl || _payloadBusy)
        {
            return;
        }
        try
        {
            using var source = _services.SourceFactory();
            var result = await Task.Run(() => manager.CheckAsync(source, manifestUrl, CancellationToken.None));
            if (result is CheckResult.UpdateAvailable update)
            {
                _pendingUpdate = (update.Manifest, update.Plan);
                var mb = Math.Max(1, update.Plan.DownloadBytes / 1_000_000);
                RunOnUi(() => ShowBanner($"A HYPE update is ready ({mb} MB).", "Install & restart app",
                    ApplyPendingUpdateAsync));
            }
        }
        catch (Exception ex) when (ex is ShellException or HttpRequestException or IOException or TaskCanceledException)
        {
            // Offline or blocked — routine checks fail silently; no banner appears.
            _services.ShellLog.WriteLine($"[shell] update check failed quietly: {ex.Message}");
        }
    }

    private async Task ApplyPendingUpdateAsync()
    {
        if (_services.PayloadManager is not { } manager || _payloadBusy)
        {
            return;
        }
        if (_pendingUpdate is not { } pending)
        {
            HideBanner();
            return;
        }
        _payloadBusy = true;
        HideBanner();
        manager.Progress += OnPayloadProgress;
        try
        {
            // Progress needs the launcher page; the app page is about to lose its server anyway.
            NavigateToLauncher();
            Post(new { type = "setup", message = "Installing the update…", percent = -1, detail = "" });
            _port = null;
            if (_supervisor is { } supervisor)
            {
                await supervisor.StopAllAsync();
            }

            using var source = _services.SourceFactory();
            await Task.Run(() => manager.ApplyAsync(source, pending.Manifest, pending.Plan, CancellationToken.None));
            _pendingUpdate = null;
            _payloadBusy = false;
            // The supervisor re-resolves the payload on every start, so the fresh app tree is
            // picked up by simply launching again.
            await InitializeAppAsync();
        }
        catch (ShellException ex)
        {
            Post(new { type = "setupError", message = $"Update failed: {ex.Message}", canRetry = true });
        }
        finally
        {
            manager.Progress -= OnPayloadProgress;
            _payloadBusy = false;
        }
    }

    private async Task BackgroundShellUpdateCheckAsync()
    {
        var version = await _shellUpdater.CheckAsync();
        if (version is not null)
        {
            RunOnUi(() => ShowBanner($"A new HYPE Desktop ({version}) is ready.", "Restart & update",
                ApplyShellUpdateAsync));
        }
    }

    private async Task ApplyShellUpdateAsync()
    {
        if (_payloadBusy)
        {
            return;
        }
        _payloadBusy = true;
        try
        {
            _bannerInstall.Enabled = false;
            _bannerText.Text = "Downloading the HYPE Desktop update…";

            // Stop the app server cleanly before Velopack restarts the process (the job object
            // would otherwise hard-kill it mid-session).
            _port = null;
            if (_supervisor is { } supervisor)
            {
                await supervisor.StopAllAsync();
            }

            await _shellUpdater.DownloadAndRestartAsync(percent => RunOnUi(() =>
                _bannerText.Text = $"Downloading the HYPE Desktop update… {percent}%"));
            // Not reached on success: the process restarts into the new version.
        }
        catch (Exception ex) when (ex is not OutOfMemoryException)
        {
            _services.ShellLog.WriteLine($"[shell-update] apply failed: {ex.Message}");
            _bannerInstall.Enabled = true;
            _bannerText.Text = $"Shell update failed: {ex.Message}";
            _payloadBusy = false;
        }
    }

    // ── Native update banner ───────────────────────────────────────────────

    private void ShowBanner(string text, string buttonLabel, Func<Task> action)
    {
        _bannerText.Text = text;
        _bannerInstall.Text = buttonLabel;
        _bannerInstall.Enabled = true;
        _bannerAction = action;
        _banner.Visible = true;
    }

    private void HideBanner()
    {
        _banner.Visible = false;
        _bannerAction = null;
    }

    // ── Plumbing ───────────────────────────────────────────────────────────

    private void Post(object message)
    {
        if (_webView.CoreWebView2 is { } core)
        {
            core.PostWebMessageAsJson(JsonSerializer.Serialize(message, DesktopJson.Options));
        }
    }

    private void RunOnUi(Action action)
    {
        if (IsDisposed)
        {
            return;
        }
        try
        {
            if (InvokeRequired)
            {
                BeginInvoke(action);
            }
            else
            {
                action();
            }
        }
        catch (InvalidOperationException)
        {
            // Window handle torn down during shutdown.
        }
    }

    private void OpenExternal(string url)
    {
        if (url.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
            || url.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
        {
            OpenInShell(url, arguments: null);
        }
    }

    private void OpenInShell(string fileName, string? arguments)
    {
        try
        {
            var psi = arguments is null
                ? new ProcessStartInfo(fileName) { UseShellExecute = true }
                : new ProcessStartInfo(fileName, arguments) { UseShellExecute = true };
            Process.Start(psi);
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException or FileNotFoundException)
        {
            _services.ShellLog.WriteLine($"[shell] failed to open '{fileName}': {ex.Message}");
        }
    }

    // ── Shutdown ───────────────────────────────────────────────────────────

    private async void OnClosing(object? sender, FormClosingEventArgs e)
    {
        if (_shutdownComplete)
        {
            return;
        }
        e.Cancel = true;
        _shuttingDown = true;
        Enabled = false;
        Text = "HYPE Desktop - stopping…";
        _updateTimer.Stop();

        try
        {
            if (_supervisor is { } supervisor)
            {
                await supervisor.StopAllAsync();
            }
        }
        finally
        {
            _shutdownComplete = true;
            Close();
        }
    }
}
