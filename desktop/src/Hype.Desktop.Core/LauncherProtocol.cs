using System.Text.Json;
using Hype.Desktop.Core.Manifest;
using Hype.Desktop.Core.Processes;

namespace Hype.Desktop.Core;

/// <summary>
/// The JSON contract between the shell and the pages it hosts (WebView2 postMessage).
/// Shell → page: full snapshots only — no deltas, so the page can always render from scratch.
/// Page → shell: small command messages ({ type, appId? }) from the launcher page, plus the
/// app page's project-folder commands ({ type, purpose?, fileName?, title? }) relayed by
/// www/desktop_bridge.js. Unknown JSON properties are ignored on both sides, so the record
/// can grow fields without breaking older payloads.
/// </summary>
public static class LauncherProtocol
{
    public sealed record ShellInfo(string Version, string Mode, string DataRoot);

    public sealed record AppCard(
        string Id,
        string Name,
        string FullName,
        string Tier,
        int TierNum,
        string Description,
        string WebUrl,
        string Status,
        int? Port,
        string? Detail);

    public sealed record Snapshot(string Type, ShellInfo Shell, IReadOnlyList<AppCard> Apps);

    public sealed record Command(string Type, string? AppId = null, string? Purpose = null,
                                 string? FileName = null, string? Title = null);

    public static string BuildSnapshotJson(
        ShellInfo shell,
        IEnumerable<AppDescriptor> apps,
        Func<string, AppRuntimeState> stateOf)
    {
        var cards = apps.Select(app =>
        {
            var state = stateOf(app.Id);
            return new AppCard(
                app.Id,
                app.Name,
                app.FullName,
                app.Tier,
                app.TierNum,
                app.Description,
                app.WebUrl,
                state.Status.ToString().ToLowerInvariant(),
                state.Port,
                state.Detail);
        }).ToList();

        return JsonSerializer.Serialize(new Snapshot("snapshot", shell, cards), DesktopJson.Options);
    }

    public static Command? ParseCommand(string json)
    {
        try
        {
            var command = JsonSerializer.Deserialize<Command>(json, DesktopJson.Options);
            return command is { Type.Length: > 0 } ? command : null;
        }
        catch (JsonException)
        {
            return null;
        }
    }
}
