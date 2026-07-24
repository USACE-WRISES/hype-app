using Hype.Desktop.Core.Logging;
using Velopack;
using Velopack.Sources;

namespace Hype.Desktop;

/// <summary>
/// Shell self-updates via Velopack against this repo's GitHub releases. Only shell releases are
/// normal (non-prerelease) releases — payload releases are always prereleases — so
/// prerelease:false resolves to the newest installer release. No-ops for unpackaged dev builds.
/// </summary>
internal sealed class ShellUpdater(ILineLog log)
{
    private const string RepoUrl = "https://github.com/gtmenichino/hype-app";

    private readonly UpdateManager _manager = new(new GithubSource(RepoUrl, accessToken: null, prerelease: false));
    private UpdateInfo? _pending;

    public bool IsSupported
    {
        get
        {
            try
            {
                return _manager.IsInstalled;
            }
            catch (NotSupportedException)
            {
                return false;
            }
        }
    }

    /// <summary>Returns the available new version string, or null when current/unsupported/offline.</summary>
    public async Task<string?> CheckAsync()
    {
        if (!IsSupported)
        {
            log.WriteLine("[shell-update] not a packaged install - skipping check");
            return null;
        }
        try
        {
            _pending = await _manager.CheckForUpdatesAsync().ConfigureAwait(false);
            if (_pending is null)
            {
                log.WriteLine("[shell-update] up to date");
                return null;
            }
            var version = _pending.TargetFullRelease.Version.ToString();
            log.WriteLine($"[shell-update] update available: {version}");
            return version;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or IOException or InvalidOperationException)
        {
            // Offline / rate-limited / blocked — routine checks fail silently.
            log.WriteLine($"[shell-update] check failed quietly: {ex.Message}");
            return null;
        }
    }

    /// <summary>Downloads the pending update, then restarts the shell into it. Does not return on success.</summary>
    public async Task DownloadAndRestartAsync(Action<int> progress)
    {
        if (_pending is null)
        {
            return;
        }
        await _manager.DownloadUpdatesAsync(_pending, progress).ConfigureAwait(false);
        log.WriteLine("[shell-update] downloaded - restarting to apply");
        _manager.ApplyUpdatesAndRestart(_pending);
    }
}
