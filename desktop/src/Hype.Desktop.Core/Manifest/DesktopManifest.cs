using System.Text.Json;
using System.Text.Json.Serialization;

namespace Hype.Desktop.Core.Manifest;

/// <summary>One launchable HYPE app, as described by desktop-manifest.json (generated in CI from docs/_data/apps.yml).</summary>
public sealed record AppDescriptor
{
    public required string Id { get; init; }
    public required string Dir { get; init; }
    public required string Entry { get; init; }
    public required string Name { get; init; }
    public string FullName { get; init; } = "";
    public string Tier { get; init; } = "";
    public int TierNum { get; init; }
    public string Role { get; init; } = "";
    public string Description { get; init; } = "";
    public string Status { get; init; } = "";
    public string WebUrl { get; init; } = "";
}

/// <summary>The apps-payload manifest shipped as desktop-manifest.json.</summary>
public sealed record DesktopManifest
{
    public required int SchemaVersion { get; init; }
    public required string Version { get; init; }
    public string BuiltFromCommit { get; init; } = "";
    public string RequiresEnv { get; init; } = "";
    public required IReadOnlyList<AppDescriptor> Apps { get; init; }

    public static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
        Converters = { new JsonStringEnumConverter() },
    };

    public static DesktopManifest Load(string path)
    {
        string json;
        try
        {
            json = File.ReadAllText(path);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or FileNotFoundException or DirectoryNotFoundException)
        {
            throw new ShellException($"Could not read the app manifest at {path}.", ex);
        }
        return Parse(json, path);
    }

    public static DesktopManifest Parse(string json, string source = "desktop-manifest.json")
    {
        DesktopManifest? manifest;
        try
        {
            manifest = JsonSerializer.Deserialize<DesktopManifest>(json, JsonOptions);
        }
        catch (JsonException ex)
        {
            throw new ShellException($"The app manifest ({source}) is not valid JSON.", ex);
        }

        if (manifest is null)
        {
            throw new ShellException($"The app manifest ({source}) is empty.");
        }
        if (manifest.SchemaVersion != 1)
        {
            throw new ShellException(
                $"The app manifest ({source}) uses schema version {manifest.SchemaVersion}; this version of HYPE Desktop understands version 1. Update HYPE Desktop.");
        }
        if (manifest.Apps.Count == 0)
        {
            throw new ShellException($"The app manifest ({source}) lists no apps.");
        }
        if (manifest.Apps.Select(a => a.Id).Distinct(StringComparer.OrdinalIgnoreCase).Count() != manifest.Apps.Count)
        {
            throw new ShellException($"The app manifest ({source}) contains duplicate app ids.");
        }
        foreach (var app in manifest.Apps)
        {
            // Dir may be the literal "." — HYPE's app lives at the payload root, not a subdir.
            if (!(app.Dir == "." || IsSafeRelativeSegment(app.Dir)) || !IsSafeRelativeSegment(app.Entry))
            {
                throw new ShellException($"The app manifest ({source}) has an unsafe path for app '{app.Id}'.");
            }
        }
        return manifest;
    }

    /// <summary>Rejects rooted paths and any traversal outside the payload directory.</summary>
    private static bool IsSafeRelativeSegment(string value) =>
        !string.IsNullOrWhiteSpace(value)
        && !Path.IsPathRooted(value)
        && value.Split('/', '\\').All(part => part is not ("" or "." or ".."));
}
