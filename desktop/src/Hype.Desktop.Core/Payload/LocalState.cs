using System.Text.Json;

namespace Hype.Desktop.Core.Payload;

public sealed record InstalledComponent(string Version, string Dir);

/// <summary>What payloads\current.json points at. Env+apps are committed together, atomically.</summary>
public sealed record CurrentPointer
{
    public int SchemaVersion { get; init; } = 1;
    public InstalledComponent? Env { get; init; }
    public InstalledComponent? Apps { get; init; }
    public DateTimeOffset InstalledAt { get; init; }
    public InstalledComponent? PreviousEnv { get; init; }
    public InstalledComponent? PreviousApps { get; init; }
}

/// <summary>
/// Owns the versioned payload directories and the current.json pointer. All pointer writes are
/// tmp+rename; version directories are immutable once committed; prune never touches current,
/// previous, or anything a running app is executing from.
/// </summary>
public sealed class LocalState(string payloadsDir)
{
    public string PayloadsDir { get; } = payloadsDir;
    public string CurrentFile => Path.Combine(PayloadsDir, "current.json");

    public string DirFor(string version) => Path.Combine(PayloadsDir, version);

    public CurrentPointer? Load()
    {
        try
        {
            if (!File.Exists(CurrentFile))
            {
                return null;
            }
            return JsonSerializer.Deserialize<CurrentPointer>(File.ReadAllText(CurrentFile), DesktopJson.Options);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            return null; // corrupt pointer = treat as not installed; dirs on disk are still reusable
        }
    }

    public void Save(CurrentPointer pointer)
    {
        Directory.CreateDirectory(PayloadsDir);
        var tmp = CurrentFile + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(pointer, DesktopJson.Options));
        File.Move(tmp, CurrentFile, overwrite: true);
    }

    /// <summary>Single atomic commit covering both components — no window where apps point at a missing env.</summary>
    public CurrentPointer Commit(InstalledComponent env, InstalledComponent apps, DateTimeOffset now)
    {
        var old = Load();
        var pointer = new CurrentPointer
        {
            Env = env,
            Apps = apps,
            InstalledAt = now,
            PreviousEnv = old?.Env is { } oldEnv && oldEnv.Version != env.Version ? oldEnv : old?.PreviousEnv,
            PreviousApps = old?.Apps is { } oldApps && oldApps.Version != apps.Version ? oldApps : old?.PreviousApps,
        };
        Save(pointer);
        return pointer;
    }

    /// <summary>Troubleshooting escape hatch: swap back to the previous payload if it still exists.</summary>
    public bool RevertToPrevious(DateTimeOffset now)
    {
        var current = Load();
        if (current?.PreviousEnv is not { } prevEnv || current.PreviousApps is not { } prevApps)
        {
            return false;
        }
        if (!Directory.Exists(DirFor(prevEnv.Dir)) || !Directory.Exists(DirFor(prevApps.Dir)))
        {
            return false;
        }
        Save(new CurrentPointer
        {
            Env = prevEnv,
            Apps = prevApps,
            InstalledAt = now,
            PreviousEnv = current.Env,
            PreviousApps = current.Apps,
        });
        return true;
    }

    /// <summary>Delete leftover extraction staging dirs (shell crashed mid-extract).</summary>
    public int SweepStaging()
    {
        if (!Directory.Exists(PayloadsDir))
        {
            return 0;
        }
        var swept = 0;
        foreach (var dir in Directory.GetDirectories(PayloadsDir, ".staging-*"))
        {
            try
            {
                Directory.Delete(dir, recursive: true);
                swept++;
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
            }
        }
        return swept;
    }

    /// <summary>
    /// Delete payload dirs that are neither current, previous, staging (handled separately), nor
    /// in use by a running app. Best-effort: locked dirs are skipped and retried next startup.
    /// </summary>
    public IReadOnlyList<string> Prune(IReadOnlyCollection<string> inUseDirs)
    {
        if (!Directory.Exists(PayloadsDir))
        {
            return [];
        }

        var pointer = Load();
        var keep = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var component in new[] { pointer?.Env, pointer?.Apps, pointer?.PreviousEnv, pointer?.PreviousApps })
        {
            if (component is not null)
            {
                keep.Add(Path.GetFullPath(DirFor(component.Dir)));
            }
        }
        foreach (var used in inUseDirs)
        {
            keep.Add(Path.GetFullPath(used));
        }

        var deleted = new List<string>();
        foreach (var dir in Directory.GetDirectories(PayloadsDir))
        {
            var name = Path.GetFileName(dir);
            if (name.StartsWith(".staging-", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            var full = Path.GetFullPath(dir);
            if (keep.Contains(full))
            {
                continue;
            }
            try
            {
                Directory.Delete(full, recursive: true);
                deleted.Add(name);
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                // Likely still executing — deferred to the next startup prune.
            }
        }
        return deleted;
    }
}
