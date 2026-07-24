using System.Text.Json;

namespace Hype.Desktop.Core.Manifest;

/// <summary>Informational shell block of latest-desktop.json (Velopack owns actual shell updates).</summary>
public sealed record ShellRelease
{
    public string Version { get; init; } = "";
    public string InstallerUrl { get; init; } = "";
    public string PortableUrl { get; init; } = "";
    public string Sha256 { get; init; } = "";
}

public sealed record ComponentInfo
{
    public required string Version { get; init; }
    public required string Url { get; init; }
    public required string Sha256 { get; init; }
    public required long SizeBytes { get; init; }
    public long InstalledSizeBytes { get; init; }

    /// <summary>Apps component: the env version it was built against.</summary>
    public string? RequiresEnv { get; init; }

    /// <summary>Env component: the embedded Python version (informational).</summary>
    public string? Python { get; init; }
}

public sealed record ManifestComponents
{
    public required ComponentInfo Env { get; init; }
    public required ComponentInfo Apps { get; init; }
}

/// <summary>
/// The payload manifest (latest-desktop.json) published on the rolling `desktop-current`
/// prerelease. Validation is strict because this file drives downloads: URLs must sit under an
/// allowed prefix, versions must be filesystem-safe (they become directory names), and the apps
/// component must reference the env published beside it.
/// </summary>
public sealed record LatestManifest
{
    public required int SchemaVersion { get; init; }
    public string MinShellVersion { get; init; } = "0.0.0";
    public ShellRelease? Shell { get; init; }
    public required ManifestComponents Components { get; init; }

    public static LatestManifest Parse(string json, IReadOnlyList<string> allowedUrlPrefixes, string source = "latest-desktop.json")
    {
        LatestManifest? manifest;
        try
        {
            manifest = JsonSerializer.Deserialize<LatestManifest>(json, DesktopManifest.JsonOptions);
        }
        catch (JsonException ex)
        {
            throw new ShellException($"The update manifest ({source}) is not valid JSON.", ex);
        }
        if (manifest is null)
        {
            throw new ShellException($"The update manifest ({source}) is empty.");
        }
        if (manifest.SchemaVersion != 1)
        {
            throw new ShellException(
                $"The update manifest uses schema version {manifest.SchemaVersion}; this version of HYPE Desktop understands version 1. Update HYPE Desktop.");
        }
        if (!System.Version.TryParse(manifest.MinShellVersion, out _))
        {
            throw new ShellException($"The update manifest has an invalid minShellVersion '{manifest.MinShellVersion}'.");
        }

        ValidateComponent(manifest.Components.Env, "env", allowedUrlPrefixes, source);
        ValidateComponent(manifest.Components.Apps, "apps", allowedUrlPrefixes, source);

        if (!string.Equals(manifest.Components.Apps.RequiresEnv, manifest.Components.Env.Version, StringComparison.Ordinal))
        {
            throw new ShellException(
                $"The update manifest is inconsistent: apps requires env '{manifest.Components.Apps.RequiresEnv}' but publishes env '{manifest.Components.Env.Version}'.");
        }
        return manifest;
    }

    private static void ValidateComponent(ComponentInfo c, string name, IReadOnlyList<string> allowedUrlPrefixes, string source)
    {
        if (!IsSafeVersionDirName(c.Version))
        {
            throw new ShellException($"The update manifest has an unsafe {name} version string '{c.Version}'.");
        }
        if (c.Sha256.Length != 64 || !c.Sha256.All(Uri.IsHexDigit))
        {
            throw new ShellException($"The update manifest has an invalid {name} sha256.");
        }
        if (c.SizeBytes <= 0)
        {
            throw new ShellException($"The update manifest has an invalid {name} size.");
        }
        if (!allowedUrlPrefixes.Any(p => c.Url.StartsWith(p, StringComparison.OrdinalIgnoreCase)))
        {
            throw new ShellException(
                $"The update manifest points the {name} download at an unexpected location ({c.Url}); refusing. ({source})");
        }
    }

    /// <summary>Version strings become directory names under payloads\ — allow only tame characters.</summary>
    public static bool IsSafeVersionDirName(string version) =>
        version.Length is > 0 and <= 100
        && version.All(ch => char.IsAsciiLetterOrDigit(ch) || ch is '.' or '-' or '_')
        && version[0] != '.';
}
