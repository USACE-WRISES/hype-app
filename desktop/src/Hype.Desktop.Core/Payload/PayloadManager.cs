using Hype.Desktop.Core.Logging;
using Hype.Desktop.Core.Manifest;

namespace Hype.Desktop.Core.Payload;

public enum PayloadPhase
{
    Checking,
    Downloading,
    Verifying,
    Extracting,
    Committing,
    Pruning,
    Done,
    Failed,
}

public sealed record PayloadProgress(
    PayloadPhase Phase,
    string? Component,
    long BytesDone,
    long BytesTotal,
    string Message);

public abstract record CheckResult
{
    public sealed record UpToDate : CheckResult;

    public sealed record ShellTooOld(string RequiredShellVersion) : CheckResult;

    public sealed record UpdateAvailable(LatestManifest Manifest, UpdatePlan Plan) : CheckResult;
}

/// <summary>What needs installing: null component = already current.</summary>
public sealed record UpdatePlan(ComponentInfo? Env, ComponentInfo? Apps)
{
    public long DownloadBytes => (Env?.SizeBytes ?? 0) + (Apps?.SizeBytes ?? 0);
    public long InstalledBytes => (Env?.InstalledSizeBytes ?? 0) + (Apps?.InstalledSizeBytes ?? 0);
    public bool IsEmpty => Env is null && Apps is null;
}

/// <summary>
/// The two-tier update engine: check → plan → download (resumable) → verify (sha256) → extract
/// (staging) → commit (atomic pointer) → prune. First run, routine updates, and install-from-file
/// are the same machine with different sources. Never stops a running app: superseded payload
/// dirs stay on disk until prune finds them idle.
/// </summary>
public sealed class PayloadManager(
    LocalState state,
    string downloadsDir,
    Version shellVersion,
    IReadOnlyList<string> allowedUrlPrefixes,
    Func<IReadOnlyCollection<string>> inUsePayloadDirs,
    ILineLog log,
    TimeProvider? time = null)
{
    private const long DiskSlackBytes = 200L * 1024 * 1024;

    private readonly TimeProvider _time = time ?? TimeProvider.System;

    public event Action<PayloadProgress>? Progress;

    public CurrentPointer? Current => state.Load();

    public bool IsInstalled => state.Load() is { Env: not null, Apps: not null };

    /// <summary>Fetch + validate the manifest, then diff against what's installed.</summary>
    public async Task<CheckResult> CheckAsync(IPayloadSource source, string manifestUrl, CancellationToken ct)
    {
        Report(PayloadPhase.Checking, null, 0, 0, "Checking for updates…");
        string json;
        using (var stream = await source.OpenAsync(manifestUrl, 0, ct).ConfigureAwait(false))
        using (var reader = new StreamReader(stream.Stream))
        {
            json = await reader.ReadToEndAsync(ct).ConfigureAwait(false);
        }
        var manifest = LatestManifest.Parse(json, allowedUrlPrefixes, manifestUrl);

        if (Version.TryParse(manifest.MinShellVersion, out var minShell) && shellVersion < minShell)
        {
            log.WriteLine($"[payload] shell {shellVersion} below required {minShell}");
            return new CheckResult.ShellTooOld(manifest.MinShellVersion);
        }

        var current = state.Load();
        var envNeeded = current?.Env?.Version != manifest.Components.Env.Version;
        var appsNeeded = current?.Apps?.Version != manifest.Components.Apps.Version;
        var plan = new UpdatePlan(
            envNeeded ? manifest.Components.Env : null,
            appsNeeded ? manifest.Components.Apps : null);

        if (plan.IsEmpty)
        {
            log.WriteLine("[payload] up to date");
            return new CheckResult.UpToDate();
        }
        log.WriteLine($"[payload] update available: env={plan.Env?.Version ?? "current"} apps={plan.Apps?.Version ?? "current"} ({plan.DownloadBytes / 1_000_000} MB)");
        return new CheckResult.UpdateAvailable(manifest, plan);
    }

    /// <summary>Download, verify, extract, and commit everything in the plan. Throws ShellException with a user-facing message on failure.</summary>
    public async Task ApplyAsync(IPayloadSource source, LatestManifest manifest, UpdatePlan plan, CancellationToken ct)
    {
        if (plan.IsEmpty)
        {
            return;
        }

        try
        {
            PreflightDiskSpace(plan);

            // Env before apps: an apps payload is only committed alongside the env it requires.
            foreach (var component in new[] { ("env", plan.Env), ("apps", plan.Apps) })
            {
                if (component.Item2 is { } info)
                {
                    await InstallComponentAsync(source, component.Item1, info, ct).ConfigureAwait(false);
                }
            }

            Report(PayloadPhase.Committing, null, 0, 0, "Finishing installation…");
            var env = plan.Env is { } e
                ? new InstalledComponent(e.Version, e.Version)
                : state.Load()?.Env ?? throw new ShellException("Internal error: no env component to commit.");
            var apps = plan.Apps is { } a
                ? new InstalledComponent(a.Version, a.Version)
                : state.Load()?.Apps ?? throw new ShellException("Internal error: no apps component to commit.");
            state.Commit(env, apps, _time.GetUtcNow());
            log.WriteLine($"[payload] committed env={env.Version} apps={apps.Version}");

            Report(PayloadPhase.Pruning, null, 0, 0, "Cleaning up old versions…");
            var pruned = state.Prune(inUsePayloadDirs());
            if (pruned.Count > 0)
            {
                log.WriteLine($"[payload] pruned: {string.Join(", ", pruned)}");
            }

            Report(PayloadPhase.Done, null, 0, 0, "HYPE is ready.");
        }
        catch (Exception ex) when (ex is not (OperationCanceledException or ShellException))
        {
            log.WriteLine($"[payload] apply failed: {ex}");
            Report(PayloadPhase.Failed, null, 0, 0, ex.Message);
            throw new ShellException($"Installing the HYPE runtime failed: {ex.Message}", ex);
        }
        catch (ShellException ex)
        {
            log.WriteLine($"[payload] apply failed: {ex.Message}");
            Report(PayloadPhase.Failed, null, 0, 0, ex.Message);
            throw;
        }
    }

    /// <summary>Offline bundle / sneakernet path: a directory containing latest-desktop.json plus the referenced zips.</summary>
    public async Task InstallFromDirectoryAsync(string bundleDir, CancellationToken ct)
    {
        var manifestPath = Path.Combine(bundleDir, "latest-desktop.json");
        if (!File.Exists(manifestPath))
        {
            throw new ShellException($"No latest-desktop.json found in {bundleDir}.");
        }
        using var source = new FilePayloadSource(bundleDir);

        // File-mode manifests reference zips by filename; allow anything (the files are local and hash-verified).
        var json = await File.ReadAllTextAsync(manifestPath, ct).ConfigureAwait(false);
        var manifest = LatestManifest.Parse(json, allowedUrlPrefixes: [""], manifestPath);

        // An offline bundle is usually just the ONLINE manifest plus its zips dropped into one
        // folder — component URLs still point at GitHub. Resolve each to its local filename.
        manifest = manifest with
        {
            Components = new ManifestComponents
            {
                Env = manifest.Components.Env with { Url = LocalAssetName(manifest.Components.Env.Url) },
                Apps = manifest.Components.Apps with { Url = LocalAssetName(manifest.Components.Apps.Url) },
            },
        };

        var current = state.Load();
        var plan = new UpdatePlan(
            current?.Env?.Version != manifest.Components.Env.Version ? manifest.Components.Env : null,
            current?.Apps?.Version != manifest.Components.Apps.Version ? manifest.Components.Apps : null);
        if (plan.IsEmpty)
        {
            Report(PayloadPhase.Done, null, 0, 0, "Already up to date.");
            return;
        }
        await ApplyAsync(source, manifest, plan, ct).ConfigureAwait(false);
    }

    public bool RevertToPrevious() => state.RevertToPrevious(_time.GetUtcNow());

    private static string LocalAssetName(string url) =>
        Uri.TryCreate(url, UriKind.Absolute, out var uri) && uri.Scheme is "http" or "https"
            ? Path.GetFileName(uri.LocalPath)
            : url;

    public void StartupSweep()
    {
        var staging = state.SweepStaging();
        if (staging > 0)
        {
            log.WriteLine($"[payload] swept {staging} orphaned staging dir(s)");
        }
        var pruned = state.Prune(inUsePayloadDirs());
        if (pruned.Count > 0)
        {
            log.WriteLine($"[payload] startup prune: {string.Join(", ", pruned)}");
        }
    }

    private async Task InstallComponentAsync(IPayloadSource source, string name, ComponentInfo info, CancellationToken ct)
    {
        var finalDir = state.DirFor(info.Version);
        if (Directory.Exists(finalDir))
        {
            // Rename is atomic, so an existing version dir is complete (e.g. crash after extract,
            // before commit). Reuse it.
            log.WriteLine($"[payload] {name} {info.Version} already extracted - reusing");
            return;
        }

        var zipPath = Path.Combine(downloadsDir, $"{info.Version}.zip");
        var partPath = zipPath + ".part";

        if (!File.Exists(zipPath))
        {
            var label = $"Downloading {(name == "env" ? "the assessment runtime" : "the HYPE apps")} ({info.SizeBytes / 1_000_000} MB)…";
            var progress = new SyncProgress<long>(done => Report(PayloadPhase.Downloading, name, done, info.SizeBytes, label));
            Report(PayloadPhase.Downloading, name, 0, info.SizeBytes, label);

            var downloader = new Downloader(source);
            try
            {
                await downloader.DownloadAsync(info.Url, partPath, info.Sha256, info.SizeBytes, progress, ct).ConfigureAwait(false);
            }
            catch (PayloadIntegrityException first)
            {
                log.WriteLine($"[payload] {name} integrity mismatch ({first.ActualSha256}) - retrying once from scratch");
                try
                {
                    await downloader.DownloadAsync(info.Url, partPath, info.Sha256, info.SizeBytes, progress, ct).ConfigureAwait(false);
                }
                catch (PayloadIntegrityException second)
                {
                    throw new ShellException(
                        $"The downloaded {name} package failed verification twice (expected sha256 {second.ExpectedSha256}, got {second.ActualSha256}). " +
                        "The release may be corrupted. Try again later.", second);
                }
            }

            Report(PayloadPhase.Verifying, name, info.SizeBytes, info.SizeBytes, "Verifying download…");
            File.Move(partPath, zipPath, overwrite: true);
        }
        else
        {
            log.WriteLine($"[payload] {name} zip already present - skipping download");
        }

        var stagingDir = Path.Combine(state.PayloadsDir, $".staging-{Guid.NewGuid():N}");
        try
        {
            var extractLabel = $"Unpacking {(name == "env" ? "the assessment runtime" : "the HYPE apps")}…";
            Report(PayloadPhase.Extracting, name, 0, 0, extractLabel);
            var progress = new SyncProgress<(int Done, int Total)>(p =>
                Report(PayloadPhase.Extracting, name, p.Done, p.Total, extractLabel));
            await Task.Run(() => Extractor.Extract(zipPath, stagingDir, progress, ct), ct).ConfigureAwait(false);
            Directory.Move(stagingDir, finalDir);
        }
        catch
        {
            TryDelete(stagingDir);
            TryDeleteFile(zipPath); // a zip that failed to extract must not be reused next run
            throw;
        }

        TryDeleteFile(zipPath);
        log.WriteLine($"[payload] {name} {info.Version} installed");
    }

    private void PreflightDiskSpace(UpdatePlan plan)
    {
        Directory.CreateDirectory(state.PayloadsDir);
        var required = plan.DownloadBytes + plan.InstalledBytes + DiskSlackBytes;
        var available = new DriveInfo(Path.GetPathRoot(Path.GetFullPath(state.PayloadsDir))!).AvailableFreeSpace;
        if (available < required)
        {
            throw new ShellException(
                $"Not enough disk space: this update needs about {required / 1_000_000_000.0:F1} GB free " +
                $"but only {available / 1_000_000_000.0:F1} GB is available on {Path.GetPathRoot(state.PayloadsDir)}.");
        }
    }

    private void Report(PayloadPhase phase, string? component, long done, long total, string message) =>
        Progress?.Invoke(new PayloadProgress(phase, component, done, total, message));

    /// <summary>
    /// Synchronous IProgress. Progress&lt;T&gt; posts callbacks through a sync context or the
    /// thread pool, so a late download/extract report could arrive after the final synchronous
    /// Done/Failed report and overwrite it (stale setup text in the UI; on CI the payload tests
    /// saw Extracting delivered after Done). Reporting inline keeps the event stream ordered;
    /// the launcher marshals to the UI thread itself.
    /// </summary>
    private sealed class SyncProgress<T>(Action<T> handler) : IProgress<T>
    {
        public void Report(T value) => handler(value);
    }

    private static void TryDelete(string dir)
    {
        try
        {
            if (Directory.Exists(dir))
            {
                Directory.Delete(dir, recursive: true);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
        }
    }

    private static void TryDeleteFile(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
        }
    }
}
