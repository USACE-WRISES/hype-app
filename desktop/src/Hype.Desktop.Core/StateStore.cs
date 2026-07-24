using System.Text.Json;

namespace Hype.Desktop.Core;

/// <summary>
/// Small persisted shell state (currently: which apps have completed a first successful start,
/// which selects the shorter startup timeout on later runs). Atomic tmp+rename writes; a corrupt
/// or missing file simply resets to empty.
/// </summary>
public sealed class StateStore
{
    private sealed record Model(List<string> AppsStartedOk);

    private readonly string _path;
    private readonly Lock _lock = new();
    private readonly HashSet<string> _startedOk = new(StringComparer.OrdinalIgnoreCase);

    public StateStore(string path)
    {
        _path = path;
        try
        {
            if (File.Exists(path))
            {
                var model = JsonSerializer.Deserialize<Model>(File.ReadAllText(path), DesktopJson.Options);
                if (model?.AppsStartedOk is { } list)
                {
                    foreach (var id in list)
                    {
                        _startedOk.Add(id);
                    }
                }
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            // Corrupt/unreadable state is disposable — start fresh.
        }
    }

    public bool HasStartedOk(string appId)
    {
        lock (_lock)
        {
            return _startedOk.Contains(appId);
        }
    }

    public void MarkStartedOk(string appId)
    {
        lock (_lock)
        {
            if (!_startedOk.Add(appId))
            {
                return;
            }
            Save();
        }
    }

    private void Save()
    {
        try
        {
            var json = JsonSerializer.Serialize(new Model([.. _startedOk]), DesktopJson.Options);
            var tmp = _path + ".tmp";
            Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
            File.WriteAllText(tmp, json);
            File.Move(tmp, _path, overwrite: true);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Losing this state only costs a longer startup timeout next run.
        }
    }
}

/// <summary>Shared JSON conventions for shell-owned files (camelCase, tolerant reads).</summary>
public static class DesktopJson
{
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
        WriteIndented = true,
    };
}
