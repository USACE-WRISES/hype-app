using Hype.Desktop.Core.Manifest;

namespace Hype.Desktop.Core.Processes;

/// <summary>
/// Builds the environment for the app server process. The app reads HYPE_DESKTOP (run-mode
/// switch: the pre-run size gates become advisory), HYPE_RAS_BIN / HYPE_MODFLOW_BIN (solver
/// locations, pointed into the payload's tools\ tree when present) and HYRIVER_CACHE_NAME
/// (via setdefault — parent wins); everything else here hardens the bundled interpreter
/// against the host machine (user site-packages, profile-root caches, proxies).
/// </summary>
public static class AppEnvironment
{
    public static Dictionary<string, string?> Build(
        ShellConfig config,
        PayloadPaths payload,
        AppDescriptor app,
        Func<string, string?>? getEnv = null,
        Func<Uri, Uri?>? proxyResolver = null)
    {
        getEnv ??= Environment.GetEnvironmentVariable;
        proxyResolver ??= ResolveSystemProxy;

        var pythonDir = Path.GetDirectoryName(payload.PythonExe)!;
        var env = new Dictionary<string, string?>(StringComparer.OrdinalIgnoreCase)
        {
            ["HYPE_DESKTOP"] = "1",
            // Explicit so hype_app/recents.py can never diverge from ShellConfig's resolution
            // (both default to %LOCALAPPDATA%\HYPE, but only one source of truth should decide).
            ["HYPE_DATA_ROOT"] = config.DataRoot,
            ["HYRIVER_CACHE_NAME"] = Path.Combine(config.CacheDir, $"{app.Id}_hyriver.sqlite"),
            ["MPLCONFIGDIR"] = Path.Combine(config.CacheDir, "matplotlib"),
            ["PYTHONDONTWRITEBYTECODE"] = "1",
            ["PYTHONNOUSERSITE"] = "1",
            ["PYTHONUTF8"] = "1",
            ["PATH"] = $"{pythonDir};{Path.Combine(pythonDir, "DLLs")};{getEnv("PATH")}",
        };

        // Installed payload layout: <envDir>\python\python.exe with sibling <envDir>\tools\
        // holding the Windows solver runtimes. Dev mode (repo .venv) has no tools dir — leave
        // the vars unset so the app falls back to the checkout's reference/ RAS install and an
        // inherited HYPE_MODFLOW_BIN (hype_app/ras.py and hype_app/run.py).
        if (Path.GetDirectoryName(pythonDir) is { } envDir)
        {
            var ras = Path.Combine(envDir, "tools", "ras2025");
            var modflow = Path.Combine(envDir, "tools", "modflow");
            if (File.Exists(Path.Combine(ras, "ras.exe")))
            {
                env["HYPE_RAS_BIN"] = ras;
            }
            if (File.Exists(Path.Combine(modflow, "mf6.exe")))
            {
                env["HYPE_MODFLOW_BIN"] = modflow;
            }
        }

        // The app's server-side USGS/3DEP/NHD calls (requests/aiohttp) only honor proxies via
        // env vars. WebView2 traffic follows system settings automatically; without this, map
        // tiles would work while terrain downloads silently failed on proxied networks.
        if (string.IsNullOrEmpty(getEnv("HTTPS_PROXY")) && string.IsNullOrEmpty(getEnv("https_proxy")))
        {
            var probe = new Uri("https://api.water.usgs.gov/");
            var proxy = proxyResolver(probe);
            if (proxy is not null && proxy != probe)
            {
                var value = proxy.GetLeftPart(UriPartial.Authority);
                env["HTTPS_PROXY"] = value;
                env["HTTP_PROXY"] = value;
            }
        }

        return env;
    }

    private static Uri? ResolveSystemProxy(Uri target)
    {
        try
        {
            var proxy = System.Net.Http.HttpClient.DefaultProxy;
            if (proxy.IsBypassed(target))
            {
                return null;
            }
            return proxy.GetProxy(target);
        }
        catch (PlatformNotSupportedException)
        {
            return null;
        }
    }
}
