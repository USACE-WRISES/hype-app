using System.Security.Cryptography;

namespace Hype.Desktop.Core.Payload;

public sealed class PayloadIntegrityException(string expectedSha256, string actualSha256)
    : Exception($"Downloaded file failed integrity verification (expected sha256 {expectedSha256}, got {actualSha256}).")
{
    public string ExpectedSha256 { get; } = expectedSha256;
    public string ActualSha256 { get; } = actualSha256;
}

/// <summary>
/// Streams a payload asset to disk with resume and hash-as-you-go verification. The destination
/// is a ".part" file the caller renames after this method returns — it never returns on a hash
/// or size mismatch (the partial is deleted and PayloadIntegrityException thrown so the manager
/// can retry once from scratch).
/// </summary>
public sealed class Downloader(IPayloadSource source)
{
    private const int BufferSize = 1 << 16;

    public async Task DownloadAsync(
        string url,
        string partPath,
        string expectedSha256,
        long expectedSize,
        IProgress<long>? bytesDone,
        CancellationToken ct)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(partPath)!);

        var offset = File.Exists(partPath) ? new FileInfo(partPath).Length : 0;
        if (offset > expectedSize)
        {
            File.Delete(partPath); // stale partial from a different release
            offset = 0;
        }

        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);

        if (offset == expectedSize)
        {
            // Fully downloaded previously but not yet verified/renamed — verify what's on disk.
            await HashExistingAsync(partPath, hash, ct).ConfigureAwait(false);
            FinishOrThrow(partPath, hash, expectedSha256, expectedSize);
            return;
        }

        using var payload = await source.OpenAsync(url, offset, ct).ConfigureAwait(false);
        if (offset > 0 && !payload.ResumedFromOffset)
        {
            offset = 0; // source restarted from zero; drop the partial
        }

        if (offset > 0)
        {
            await HashExistingAsync(partPath, hash, ct).ConfigureAwait(false);
        }

        await using (var file = new FileStream(
            partPath,
            offset > 0 ? FileMode.Append : FileMode.Create,
            FileAccess.Write,
            FileShare.None,
            BufferSize,
            useAsync: true))
        {
            var buffer = new byte[BufferSize];
            var total = offset;
            int read;
            while ((read = await payload.Stream.ReadAsync(buffer.AsMemory(), ct).ConfigureAwait(false)) > 0)
            {
                await file.WriteAsync(buffer.AsMemory(0, read), ct).ConfigureAwait(false);
                hash.AppendData(buffer, 0, read);
                total += read;
                if (total > expectedSize)
                {
                    break; // server sent more than the manifest promised — fail via size check below
                }
                bytesDone?.Report(total);
            }
        }

        FinishOrThrow(partPath, hash, expectedSha256, expectedSize);
    }

    private static async Task HashExistingAsync(string partPath, IncrementalHash hash, CancellationToken ct)
    {
        await using var existing = new FileStream(
            partPath, FileMode.Open, FileAccess.Read, FileShare.None, BufferSize, useAsync: true);
        var buffer = new byte[BufferSize];
        int read;
        while ((read = await existing.ReadAsync(buffer.AsMemory(), ct).ConfigureAwait(false)) > 0)
        {
            hash.AppendData(buffer, 0, read);
        }
    }

    private static void FinishOrThrow(string partPath, IncrementalHash hash, string expectedSha256, long expectedSize)
    {
        var actual = Convert.ToHexString(hash.GetHashAndReset());
        var actualSize = new FileInfo(partPath).Length;
        if (actualSize != expectedSize || !actual.Equals(expectedSha256, StringComparison.OrdinalIgnoreCase))
        {
            File.Delete(partPath);
            throw new PayloadIntegrityException(expectedSha256.ToUpperInvariant(), actual);
        }
    }
}
