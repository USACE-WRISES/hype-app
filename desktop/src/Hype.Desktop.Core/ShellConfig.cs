namespace Hype.Desktop.Core;

/// <summary>
/// Resolved filesystem layout and runtime mode for the shell. All shell data lives under
/// %LOCALAPPDATA%\HYPE (never Roaming — government roaming-profile quotas), separate from the
/// Velopack-managed application directory so shell updates never disturb payloads or caches.
/// </summary>
public sealed record ShellConfig
{
    public required string DataRoot { get; init; }
    public required string SelfExePath { get; init; }

    /// <summary>Repo root when running against a checkout's .venv instead of an installed payload.</summary>
    public string? DevRepoRoot { get; init; }

    public string PayloadsDir => Path.Combine(DataRoot, "payloads");
    public string DownloadsDir => Path.Combine(DataRoot, "downloads");
    public string CacheDir => Path.Combine(DataRoot, "cache");
    public string LogsDir => Path.Combine(DataRoot, "logs");
    public string WebViewDataDir => Path.Combine(DataRoot, "webview-data");
    public string TmpDir => Path.Combine(DataRoot, "tmp");
    public string StateFile => Path.Combine(DataRoot, "state.json");

    public bool IsDevMode => DevRepoRoot is not null;

    public static ShellConfig Create()
    {
        var dataRoot = Environment.GetEnvironmentVariable("HYPE_DATA_ROOT");
        if (string.IsNullOrWhiteSpace(dataRoot))
        {
            dataRoot = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "HYPE");
        }

        return new ShellConfig
        {
            DataRoot = dataRoot,
            SelfExePath = Environment.ProcessPath
                ?? Path.Combine(AppContext.BaseDirectory, "HypeDesktop.exe"),
            DevRepoRoot = ResolveDevRepoRoot(),
        };
    }

    public void EnsureDirectories()
    {
        foreach (var dir in new[] { DataRoot, PayloadsDir, DownloadsDir, CacheDir, LogsDir, WebViewDataDir, TmpDir })
        {
            Directory.CreateDirectory(dir);
        }
    }

    /// <summary>
    /// Dev mode activates when the shell runs from inside the repo (bin dir under desktop/) or when
    /// HYPE_REPO_ROOT points at a checkout. HYPE_DESKTOP_DEV=0 forces it off.
    /// </summary>
    private static string? ResolveDevRepoRoot()
    {
        if (Environment.GetEnvironmentVariable("HYPE_DESKTOP_DEV") == "0")
        {
            return null;
        }

        var explicitRoot = Environment.GetEnvironmentVariable("HYPE_REPO_ROOT");
        if (!string.IsNullOrWhiteSpace(explicitRoot) && LooksLikeRepoRoot(explicitRoot))
        {
            return Path.GetFullPath(explicitRoot);
        }

        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            if (LooksLikeRepoRoot(dir.FullName))
            {
                return dir.FullName;
            }
            dir = dir.Parent;
        }
        return null;
    }

    private static bool LooksLikeRepoRoot(string path) =>
        File.Exists(Path.Combine(path, "app.py"))
        && Directory.Exists(Path.Combine(path, "hype_app"))
        && Directory.Exists(Path.Combine(path, "desktop"));
}
