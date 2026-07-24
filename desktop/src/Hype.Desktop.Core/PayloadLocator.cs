namespace Hype.Desktop.Core;

/// <summary>Where the Python runtime, the apps tree, and the app manifest currently live.</summary>
public sealed record PayloadPaths(string PythonExe, string AppsRoot, string ManifestFile);

/// <summary>
/// Resolves the active payload. Dev mode resolves against the repo checkout; installed mode
/// (M3) will resolve against %LOCALAPPDATA%\HYPE\payloads\current.json.
/// </summary>
public interface IPayloadLocator
{
    PayloadPaths Resolve();
}

/// <summary>Resolves against the installed payload pointed at by payloads\current.json.</summary>
public sealed class InstalledPayloadLocator(ShellConfig config) : IPayloadLocator
{
    public PayloadPaths Resolve()
    {
        var state = new Payload.LocalState(config.PayloadsDir);
        var pointer = state.Load();
        if (pointer is not { Env: { } env, Apps: { } apps })
        {
            throw new ShellException("The HYPE runtime is not installed yet. Complete first-run setup.");
        }

        var python = Path.Combine(state.DirFor(env.Dir), "python", "python.exe");
        if (!File.Exists(python))
        {
            throw new ShellException(
                $"The installed runtime is missing ({python}). Run first-run setup again.");
        }
        var appsRoot = state.DirFor(apps.Dir);
        var manifest = Path.Combine(appsRoot, "desktop-manifest.json");
        if (!File.Exists(manifest))
        {
            throw new ShellException($"The installed apps payload is missing its manifest ({manifest}).");
        }

        return new PayloadPaths(python, appsRoot, manifest);
    }
}

/// <summary>Runs the apps from the repo's shared .venv — the developer loop, no payload required.</summary>
public sealed class DevPayloadLocator(ShellConfig config) : IPayloadLocator
{
    public PayloadPaths Resolve()
    {
        var repo = config.DevRepoRoot
            ?? throw new ShellException("Dev mode is not active (no repo root found) and no payload is installed yet.");

        var python = Path.Combine(repo, ".venv", "Scripts", "python.exe");
        if (!File.Exists(python))
        {
            throw new ShellException(
                $"Dev python not found at {python}. Create the venv first: py -3.12 -m venv .venv && .venv\\Scripts\\pip install -r requirements.txt");
        }

        // The app lives at the repo root (app.py + hype_app/), not in an apps/ subtree.
        return new PayloadPaths(
            PythonExe: python,
            AppsRoot: repo,
            ManifestFile: Path.Combine(repo, "desktop", "dev", "dev-manifest.json"));
    }
}
