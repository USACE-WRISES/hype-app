using Hype.Desktop.Core.Logging;
using Hype.Desktop.Core.Manifest;

namespace Hype.Desktop.Core.Processes;

public enum AppStatus
{
    Stopped,
    Starting,
    Running,
    Stopping,
    Crashed,
}

public sealed record AppRuntimeState
{
    public required AppStatus Status { get; init; }
    public int? Port { get; init; }
    public int? Pid { get; init; }

    /// <summary>Short human-readable context: "exit code 1", "still starting…".</summary>
    public string? Detail { get; init; }
}

public sealed record SupervisorOptions
{
    public TimeSpan PollInterval { get; init; } = TimeSpan.FromMilliseconds(500);

    /// <summary>First-ever start on this machine: HYPE's geopandas/rasterio/flopy import chain is slow.</summary>
    public TimeSpan FirstStartSoftTimeout { get; init; } = TimeSpan.FromSeconds(180);
    public TimeSpan SoftTimeout { get; init; } = TimeSpan.FromSeconds(90);

    /// <summary>Beyond this the process is presumed wedged: killed and reported.</summary>
    public TimeSpan StartHardCap { get; init; } = TimeSpan.FromSeconds(300);

    public TimeSpan StopHelperTimeout { get; init; } = TimeSpan.FromSeconds(5);
    public TimeSpan GracefulStopWait { get; init; } = TimeSpan.FromSeconds(10);
    public TimeSpan KillWait { get; init; } = TimeSpan.FromSeconds(5);
    public TimeSpan ExitPollInterval { get; init; } = TimeSpan.FromMilliseconds(100);
    public int MaxSpawnAttempts { get; init; } = 3;

    public static readonly string[] BindErrorMarkers =
    [
        "error while attempting to bind",
        "address already in use",
        "winerror 10048",
    ];
}

/// <summary>
/// Owns the lifecycle of the app server process(es): spawn with the desktop environment,
/// health-poll until the Shiny server answers, detect crashes and port-bind races, stop
/// gracefully (Ctrl+C via the stop helper) with a tree-kill escalation. All state transitions
/// surface through <see cref="StateChanged"/>; public methods never throw for app-level failures —
/// failures land in the app's state so the UI has one rendering path.
/// </summary>
public sealed class AppSupervisor : IAsyncDisposable
{
    private sealed class Entry(AppDescriptor app, ILineLog log)
    {
        public AppDescriptor App { get; } = app;
        public ILineLog Log { get; } = log;
        public SemaphoreSlim Gate { get; } = new(1, 1);
        public BoundedLineBuffer Recent { get; } = new(100);
        public AppRuntimeState State { get; set; } = new() { Status = AppStatus.Stopped };
        public IAppProcess? Process { get; set; }
        public CancellationTokenSource? MonitorCts { get; set; }
        public Task? MonitorTask { get; set; }
        public PayloadPaths? ActivePayload { get; set; }
    }

    private readonly ShellConfig _config;
    private readonly IPayloadLocator _locator;
    private readonly IProcessRunner _runner;
    private readonly IHealthProbe _probe;
    private readonly StateStore _stateStore;
    private readonly SupervisorOptions _options;
    private readonly ILineLog _shellLog;
    private readonly Dictionary<string, Entry> _entries;

    public IReadOnlyList<AppDescriptor> Apps { get; }

    public event Action<string, AppRuntimeState>? StateChanged;

    public AppSupervisor(
        ShellConfig config,
        IPayloadLocator locator,
        IReadOnlyList<AppDescriptor> apps,
        IProcessRunner runner,
        IHealthProbe probe,
        LogFactory logs,
        StateStore stateStore,
        ILineLog shellLog,
        SupervisorOptions? options = null)
    {
        _config = config;
        _locator = locator;
        _runner = runner;
        _probe = probe;
        _stateStore = stateStore;
        _options = options ?? new SupervisorOptions();
        _shellLog = shellLog;
        Apps = apps;
        _entries = apps.ToDictionary(a => a.Id, a => new Entry(a, logs.For(a.Id)), StringComparer.OrdinalIgnoreCase);
    }

    public AppRuntimeState GetState(string appId) => GetEntry(appId).State;

    /// <summary>
    /// Payload directories that live processes are executing from (env dir via python.exe, apps
    /// root). Prune must never delete these; superseded payloads are cleaned once idle.
    /// </summary>
    public IReadOnlyCollection<string> GetInUsePayloadDirs()
    {
        var dirs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in _entries.Values)
        {
            if (entry.State.Status is AppStatus.Starting or AppStatus.Running or AppStatus.Stopping
                && entry.ActivePayload is { } payload)
            {
                // python.exe sits at <envDir>\python\python.exe → keep <envDir>.
                var pythonDir = Path.GetDirectoryName(payload.PythonExe);
                if (Path.GetDirectoryName(pythonDir) is { } envDir)
                {
                    dirs.Add(envDir);
                }
                dirs.Add(payload.AppsRoot);
            }
        }
        return dirs;
    }

    public IReadOnlyList<string> GetRecentOutput(string appId) => GetEntry(appId).Recent.Snapshot();

    public string GetLogPath(string appId) => GetEntry(appId).Log.Path;

    public async Task StartAsync(string appId)
    {
        var entry = GetEntry(appId);
        await entry.Gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (entry.State.Status is AppStatus.Starting or AppStatus.Running or AppStatus.Stopping)
            {
                return;
            }

            PayloadPaths payload;
            try
            {
                payload = _locator.Resolve();
            }
            catch (ShellException ex)
            {
                SetState(entry, new AppRuntimeState { Status = AppStatus.Crashed, Detail = ex.Message });
                return;
            }

            entry.Recent.Clear();
            CleanupMonitor(entry);
            entry.ActivePayload = payload;
            SetState(entry, new AppRuntimeState { Status = AppStatus.Starting, Detail = "starting…" });

            int port;
            try
            {
                port = Spawn(entry, payload, attempt: 1);
            }
            catch (Exception ex) when (ex is ShellException or System.ComponentModel.Win32Exception or IOException)
            {
                SetState(entry, new AppRuntimeState { Status = AppStatus.Crashed, Detail = ex.Message });
                return;
            }

            var cts = new CancellationTokenSource();
            entry.MonitorCts = cts;
            entry.MonitorTask = Task.Run(() => MonitorAsync(entry, payload, port, cts.Token));
        }
        finally
        {
            entry.Gate.Release();
        }
    }

    public async Task StopAsync(string appId)
    {
        var entry = GetEntry(appId);
        await entry.Gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (entry.State.Status is not (AppStatus.Starting or AppStatus.Running))
            {
                return;
            }

            SetState(entry, entry.State with { Status = AppStatus.Stopping, Detail = "stopping…" });

            if (entry.MonitorCts is { } cts)
            {
                await cts.CancelAsync().ConfigureAwait(false);
            }
            if (entry.MonitorTask is { } monitor)
            {
                try
                {
                    await monitor.ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                }
            }

            CleanupMonitor(entry);
            var process = entry.Process;
            string detail = "stopped";
            if (process is not null && !process.HasExited)
            {
                var helperExit = await _runner.RunToExitAsync(
                    new ProcessSpec
                    {
                        ExePath = _config.SelfExePath,
                        Arguments = ["--stop-helper", process.Pid.ToString()],
                    },
                    _options.StopHelperTimeout).ConfigureAwait(false);
                entry.Log.WriteLine($"[shell] stop helper exit: {(helperExit?.ToString() ?? "timeout")}");

                if (!await WaitForExitAsync(process, _options.GracefulStopWait).ConfigureAwait(false))
                {
                    entry.Log.WriteLine("[shell] graceful stop timed out - killing process tree");
                    process.KillTree();
                    await WaitForExitAsync(process, _options.KillWait).ConfigureAwait(false);
                    detail = "stopped (forced)";
                }
                else
                {
                    detail = $"stopped (exit code {process.ExitCode?.ToString() ?? "?"})";
                }
            }

            entry.Log.WriteLine($"[shell] {detail}");
            CleanupProcess(entry);
            entry.ActivePayload = null;
            SetState(entry, new AppRuntimeState { Status = AppStatus.Stopped, Detail = detail });
        }
        finally
        {
            entry.Gate.Release();
        }
    }

    public Task StopAllAsync() => Task.WhenAll(Apps.Select(a => StopAsync(a.Id)));

    public async ValueTask DisposeAsync() => await StopAllAsync().ConfigureAwait(false);

    private async Task MonitorAsync(Entry entry, PayloadPaths payload, int port, CancellationToken ct)
    {
        var attempt = 1;
        var softTimeout = _stateStore.HasStartedOk(entry.App.Id) ? _options.SoftTimeout : _options.FirstStartSoftTimeout;
        var startedAt = DateTimeOffset.UtcNow;
        var softNotified = false;

        // Phase 1: wait for the server to answer HTTP.
        while (!ct.IsCancellationRequested)
        {
            var process = entry.Process!;
            if (process.HasExited)
            {
                var exitCode = process.ExitCode;
                if (attempt < _options.MaxSpawnAttempts && entry.Recent.ContainsAny(SupervisorOptions.BindErrorMarkers))
                {
                    attempt++;
                    entry.Log.WriteLine($"[shell] port bind conflict - retrying with a new port (attempt {attempt})");
                    entry.Recent.Clear();
                    try
                    {
                        port = Spawn(entry, payload, attempt);
                        startedAt = DateTimeOffset.UtcNow;
                        continue;
                    }
                    catch (Exception ex) when (ex is ShellException or System.ComponentModel.Win32Exception or IOException)
                    {
                        SetStateUnlessCancelled(entry, new AppRuntimeState { Status = AppStatus.Crashed, Detail = ex.Message }, ct);
                        return;
                    }
                }

                SetStateUnlessCancelled(entry, new AppRuntimeState
                {
                    Status = AppStatus.Crashed,
                    Detail = $"exited during startup (code {exitCode?.ToString() ?? "?"})",
                }, ct);
                return;
            }

            bool healthy;
            try
            {
                healthy = await _probe.IsHealthyAsync(port, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }

            if (healthy)
            {
                SetStateUnlessCancelled(entry, new AppRuntimeState
                {
                    Status = AppStatus.Running,
                    Port = port,
                    Pid = entry.Process!.Pid,
                }, ct);
                _stateStore.MarkStartedOk(entry.App.Id);
                entry.Log.WriteLine($"[shell] running on http://127.0.0.1:{port}/ (pid {entry.Process!.Pid})");
                break;
            }

            var elapsed = DateTimeOffset.UtcNow - startedAt;
            if (!softNotified && elapsed > softTimeout)
            {
                softNotified = true;
                SetStateUnlessCancelled(entry, new AppRuntimeState
                {
                    Status = AppStatus.Starting,
                    Port = port,
                    Pid = entry.Process!.Pid,
                    Detail = "first start can take a few minutes",
                }, ct);
            }
            if (elapsed > _options.StartHardCap)
            {
                entry.Log.WriteLine("[shell] startup exceeded the hard cap - killing process");
                entry.Process!.KillTree();
                SetStateUnlessCancelled(entry, new AppRuntimeState
                {
                    Status = AppStatus.Crashed,
                    Detail = "startup timed out",
                }, ct);
                return;
            }

            try
            {
                await Task.Delay(_options.PollInterval, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }

        // Phase 2: watch for unexpected exit while Running.
        while (!ct.IsCancellationRequested)
        {
            var process = entry.Process!;
            if (process.HasExited)
            {
                var tail = string.Join(Environment.NewLine, entry.Recent.Snapshot().TakeLast(5));
                entry.Log.WriteLine($"[shell] process exited unexpectedly (code {process.ExitCode?.ToString() ?? "?"})");
                SetStateUnlessCancelled(entry, new AppRuntimeState
                {
                    Status = AppStatus.Crashed,
                    Detail = $"exited unexpectedly (code {process.ExitCode?.ToString() ?? "?"}){(tail.Length > 0 ? Environment.NewLine + tail : "")}",
                }, ct);
                return;
            }
            try
            {
                await Task.Delay(_options.PollInterval, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }
    }

    private int Spawn(Entry entry, PayloadPaths payload, int attempt)
    {
        var port = PortAllocator.GetFreeLoopbackPort();
        // GetFullPath normalizes the manifest's "." dir (HYPE's app sits at the payload root).
        var appDir = Path.GetFullPath(Path.Combine(payload.AppsRoot, entry.App.Dir));
        if (!Directory.Exists(appDir))
        {
            throw new ShellException($"App folder not found: {appDir}");
        }

        var spec = new ProcessSpec
        {
            ExePath = payload.PythonExe,
            Arguments = ["-u", "-m", "shiny", "run", "--host", "127.0.0.1", "--port", port.ToString(), entry.App.Entry],
            WorkingDirectory = appDir,
            Environment = AppEnvironment.Build(_config, payload, entry.App),
        };

        entry.Log.WriteLine($"[shell] === start attempt {attempt} ===");
        entry.Log.WriteLine($"[shell] exe: {spec.ExePath}");
        entry.Log.WriteLine($"[shell] args: {string.Join(' ', spec.Arguments)}");
        entry.Log.WriteLine($"[shell] cwd: {spec.WorkingDirectory}");
        entry.Log.WriteLine($"[shell] env overrides: {string.Join(", ", spec.Environment.Keys)}");

        CleanupProcess(entry);
        entry.Process = _runner.Start(spec, line =>
        {
            entry.Recent.Add(line);
            entry.Log.WriteLine(line);
        });
        _shellLog.WriteLine($"[{entry.App.Id}] spawned pid {entry.Process.Pid} on port {port}");
        return port;
    }

    private async Task<bool> WaitForExitAsync(IAppProcess process, TimeSpan timeout)
    {
        var deadline = DateTimeOffset.UtcNow + timeout;
        while (DateTimeOffset.UtcNow < deadline)
        {
            if (process.HasExited)
            {
                return true;
            }
            await Task.Delay(_options.ExitPollInterval).ConfigureAwait(false);
        }
        return process.HasExited;
    }

    private static void CleanupProcess(Entry entry)
    {
        entry.Process?.Dispose();
        entry.Process = null;
    }

    /// <summary>Only call when no monitor task is running (before a fresh start, or after awaiting it in Stop).</summary>
    private static void CleanupMonitor(Entry entry)
    {
        entry.MonitorCts?.Dispose();
        entry.MonitorCts = null;
        entry.MonitorTask = null;
    }

    private void SetStateUnlessCancelled(Entry entry, AppRuntimeState state, CancellationToken ct)
    {
        if (!ct.IsCancellationRequested)
        {
            SetState(entry, state);
        }
    }

    private void SetState(Entry entry, AppRuntimeState state)
    {
        entry.State = state;
        StateChanged?.Invoke(entry.App.Id, state);
    }

    private Entry GetEntry(string appId) =>
        _entries.TryGetValue(appId, out var entry)
            ? entry
            : throw new ArgumentException($"Unknown app id '{appId}'.", nameof(appId));
}
