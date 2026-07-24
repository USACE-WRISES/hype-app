using System.ComponentModel;
using System.Diagnostics;

namespace Hype.Desktop.Core.Processes;

/// <summary>
/// Startup belt-and-suspenders behind the job object: kills any process still executing out of
/// the payloads directory (a python server orphaned by a shell crash on a machine where the job
/// object could not be created/assigned). Never touches anything outside that directory — in
/// particular, dev servers running from the repo .venv are not ours to kill.
/// </summary>
public static class OrphanReaper
{
    public static int ReapPayloadOrphans(string payloadsDir, Action<string>? log = null)
    {
        var reaped = 0;
        var prefix = Path.TrimEndingDirectorySeparator(Path.GetFullPath(payloadsDir)) + Path.DirectorySeparatorChar;

        foreach (var process in Process.GetProcesses())
        {
            try
            {
                string? exePath;
                try
                {
                    exePath = process.MainModule?.FileName;
                }
                catch (Exception ex) when (ex is Win32Exception or InvalidOperationException or NotSupportedException)
                {
                    continue; // other users' / protected processes
                }

                if (exePath is not null && exePath.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                {
                    log?.Invoke($"[reaper] killing orphaned process {process.Id} ({exePath})");
                    process.Kill(entireProcessTree: true);
                    reaped++;
                }
            }
            catch (Exception ex) when (ex is Win32Exception or InvalidOperationException)
            {
                // Exited between enumeration and kill, or access denied — skip.
            }
            finally
            {
                process.Dispose();
            }
        }
        return reaped;
    }
}
