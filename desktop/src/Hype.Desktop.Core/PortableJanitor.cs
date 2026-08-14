namespace Hype.Desktop.Core;

/// <summary>
/// One-shot startup cleanup for portable installs that predate the launcher-name fix.
/// vpk 1.2.0 shipped the portable zip with a launcher named after the pack TITLE
/// ("HYPE Desktop.exe") while the updater writes the pack-ID name ("HypeDesktop.exe"),
/// so a portable install's first update left the title-named stub behind as a stale
/// duplicate (see desktop/RELEASING.md, "Prerequisites &amp; recovery"). The zip has since
/// been normalized to the ID name; this removes the leftover from installs extracted
/// before that.
/// </summary>
public static class PortableJanitor
{
    // Historical filenames — the whole point is that these two disagreed once.
    private const string IdLauncher = "HypeDesktop.exe";
    private const string StaleTitleLauncher = "HYPE Desktop.exe";

    /// <summary>
    /// Deletes a stale title-named launcher one level above <paramref name="appDir"/>
    /// (the running app's directory, i.e. the Velopack "current" dir). Acts only when
    /// this is a portable install (".portable" marker) AND the id-named launcher exists —
    /// the only launcher is never deleted. Returns true only when the stale file was
    /// actually removed; never throws.
    /// </summary>
    public static bool CleanStaleLauncher(string appDir, Action<string>? log = null)
    {
        try
        {
            var root = Directory.GetParent(Path.TrimEndingDirectorySeparator(appDir))?.FullName;
            if (root is null)
            {
                return false;
            }
            if (!File.Exists(Path.Combine(root, ".portable"))
                || !File.Exists(Path.Combine(root, IdLauncher)))
            {
                return false;
            }
            var stale = Path.Combine(root, StaleTitleLauncher);
            if (!File.Exists(stale))
            {
                return false;
            }
            File.Delete(stale);
            log?.Invoke($"[janitor] removed stale portable launcher: {stale}");
            return true;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Locked or protected (e.g. the user launched THROUGH the stale stub this very
            // session) — leave it for the next start; the app must never fail over this.
            log?.Invoke($"[janitor] could not remove stale portable launcher: {ex.Message}");
            return false;
        }
    }
}
