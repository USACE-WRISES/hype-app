using System.IO.Compression;

namespace Hype.Desktop.Core.Payload;

/// <summary>
/// Extracts a verified payload zip into a staging directory. The caller renames staging to the
/// final versioned directory afterwards — rename is atomic on the same volume, so a version dir
/// either exists completely or not at all. Guards against zip-slip: every entry must land inside
/// the staging root.
/// </summary>
public static class Extractor
{
    public static void Extract(string zipPath, string stagingDir, IProgress<(int Done, int Total)>? progress, CancellationToken ct)
    {
        Directory.CreateDirectory(stagingDir);
        var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(stagingDir)) + Path.DirectorySeparatorChar;

        using var archive = ZipFile.OpenRead(zipPath);
        var total = archive.Entries.Count;
        var done = 0;

        foreach (var entry in archive.Entries)
        {
            ct.ThrowIfCancellationRequested();

            var target = Path.GetFullPath(Path.Combine(stagingDir, entry.FullName));
            if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase))
            {
                throw new ShellException($"The payload archive contains an unsafe path ('{entry.FullName}'); refusing to extract.");
            }

            if (entry.FullName.EndsWith('/') || entry.FullName.EndsWith('\\'))
            {
                Directory.CreateDirectory(target);
            }
            else
            {
                Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                entry.ExtractToFile(target, overwrite: true);
            }

            done++;
            if (done % 200 == 0 || done == total)
            {
                progress?.Report((done, total));
            }
        }
    }
}
