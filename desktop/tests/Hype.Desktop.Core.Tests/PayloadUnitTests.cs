using System.IO.Compression;
using System.Security.Cryptography;
using Hype.Desktop.Core;
using Hype.Desktop.Core.Manifest;
using Hype.Desktop.Core.Payload;

namespace Hype.Desktop.Core.Tests;

/// <summary>Scriptable payload source: honors or ignores resume offsets, can corrupt bytes.</summary>
internal sealed class FakeSource : IPayloadSource
{
    public required byte[] Bytes { get; set; }
    public bool SupportsResume { get; set; } = true;
    public bool CorruptLastByte { get; set; }
    public List<long> RequestedOffsets { get; } = [];

    public Task<PayloadStream> OpenAsync(string url, long offset, CancellationToken ct)
    {
        RequestedOffsets.Add(offset);
        var data = Bytes;
        if (CorruptLastByte)
        {
            data = (byte[])data.Clone();
            data[^1] ^= 0xFF;
        }
        var effectiveOffset = SupportsResume ? offset : 0;
        var stream = new MemoryStream(data, (int)effectiveOffset, data.Length - (int)effectiveOffset, writable: false);
        return Task.FromResult(new PayloadStream(stream, data.Length, SupportsResume && offset > 0));
    }

    public void Dispose()
    {
    }
}

public sealed class LatestManifestTests
{
    private static readonly string[] Official = ["https://github.com/USACE-WRISES/hype-app/releases/download/"];

    private static string Json(
        string envVersion = "env-cp312-4f9a2c1b",
        string appsRequires = "env-cp312-4f9a2c1b",
        string envUrl = "https://github.com/USACE-WRISES/hype-app/releases/download/x/env.zip",
        string sha = "AB12CD34AB12CD34AB12CD34AB12CD34AB12CD34AB12CD34AB12CD34AB12CD34",
        int schema = 1,
        string minShell = "1.0.0") => $$"""
        {
          "schemaVersion": {{schema}},
          "minShellVersion": "{{minShell}}",
          "components": {
            "env": { "version": "{{envVersion}}", "url": "{{envUrl}}",
                     "sha256": "{{sha}}", "sizeBytes": 1000, "installedSizeBytes": 3000, "python": "3.12.11" },
            "apps": { "version": "apps-2026.07.06-abc1234",
                      "url": "https://github.com/USACE-WRISES/hype-app/releases/download/x/apps.zip",
                      "sha256": "{{sha}}", "sizeBytes": 500, "installedSizeBytes": 800,
                      "requiresEnv": "{{appsRequires}}" }
          }
        }
        """;

    [Fact]
    public void ParsesValidManifest()
    {
        var m = LatestManifest.Parse(Json(), Official);
        Assert.Equal("env-cp312-4f9a2c1b", m.Components.Env.Version);
        Assert.Equal(500, m.Components.Apps.SizeBytes);
    }

    [Fact]
    public void RejectsUrlOutsideAllowlist()
    {
        var ex = Assert.Throws<ShellException>(() =>
            LatestManifest.Parse(Json(envUrl: "https://evil.example.com/env.zip"), Official));
        Assert.Contains("unexpected location", ex.Message);
    }

    [Fact]
    public void RejectsEnvAppsVersionSkew()
    {
        Assert.Throws<ShellException>(() => LatestManifest.Parse(Json(appsRequires: "env-cp312-other"), Official));
    }

    [Theory]
    [InlineData("..")]
    [InlineData("a/b")]
    [InlineData("a\\b")]
    [InlineData(".hidden")]
    [InlineData("")]
    public void RejectsUnsafeVersionStrings(string version)
    {
        Assert.Throws<ShellException>(() => LatestManifest.Parse(Json(envVersion: version, appsRequires: version), Official));
    }

    [Fact]
    public void RejectsBadSchemaShaAndShellVersion()
    {
        Assert.Throws<ShellException>(() => LatestManifest.Parse(Json(schema: 2), Official));
        Assert.Throws<ShellException>(() => LatestManifest.Parse(Json(sha: "1234"), Official));
        Assert.Throws<ShellException>(() => LatestManifest.Parse(Json(minShell: "not-a-version"), Official));
    }
}

public sealed class DownloaderTests : IDisposable
{
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "hype-desktop-tests", Guid.NewGuid().ToString("N"));

    private static byte[] MakeBytes(int n) =>
        [.. Enumerable.Range(0, n).Select(i => (byte)(i % 251))];

    private static string Sha(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes));

    [Fact]
    public async Task FullDownload_VerifiesAndKeepsPart()
    {
        var bytes = MakeBytes(200_000);
        var source = new FakeSource { Bytes = bytes };
        var part = Path.Combine(_dir, "a.zip.part");

        await new Downloader(source).DownloadAsync("u", part, Sha(bytes), bytes.Length, null, CancellationToken.None);

        Assert.Equal(bytes, await File.ReadAllBytesAsync(part));
        Assert.Equal([0L], source.RequestedOffsets);
    }

    [Fact]
    public async Task Resume_ContinuesFromPartial_AndHashMatches()
    {
        var bytes = MakeBytes(300_000);
        var part = Path.Combine(_dir, "b.zip.part");
        Directory.CreateDirectory(_dir);
        await File.WriteAllBytesAsync(part, bytes[..120_000]);

        var source = new FakeSource { Bytes = bytes };
        await new Downloader(source).DownloadAsync("u", part, Sha(bytes), bytes.Length, null, CancellationToken.None);

        Assert.Equal(bytes, await File.ReadAllBytesAsync(part));
        Assert.Equal([120_000L], source.RequestedOffsets);
    }

    [Fact]
    public async Task Resume_RestartsCleanly_WhenSourceIgnoresOffset()
    {
        var bytes = MakeBytes(100_000);
        var part = Path.Combine(_dir, "c.zip.part");
        Directory.CreateDirectory(_dir);
        await File.WriteAllBytesAsync(part, bytes[..40_000]);

        var source = new FakeSource { Bytes = bytes, SupportsResume = false };
        await new Downloader(source).DownloadAsync("u", part, Sha(bytes), bytes.Length, null, CancellationToken.None);

        Assert.Equal(bytes, await File.ReadAllBytesAsync(part));
    }

    [Fact]
    public async Task CorruptStream_DeletesPartAndThrows()
    {
        var bytes = MakeBytes(50_000);
        var source = new FakeSource { Bytes = bytes, CorruptLastByte = true };
        var part = Path.Combine(_dir, "d.zip.part");

        await Assert.ThrowsAsync<PayloadIntegrityException>(() =>
            new Downloader(source).DownloadAsync("u", part, Sha(bytes), bytes.Length, null, CancellationToken.None));
        Assert.False(File.Exists(part));
    }

    [Fact]
    public async Task CompletePartFile_IsVerifiedWithoutRedownload()
    {
        var bytes = MakeBytes(80_000);
        var part = Path.Combine(_dir, "e.zip.part");
        Directory.CreateDirectory(_dir);
        await File.WriteAllBytesAsync(part, bytes);

        var source = new FakeSource { Bytes = bytes };
        await new Downloader(source).DownloadAsync("u", part, Sha(bytes), bytes.Length, null, CancellationToken.None);

        Assert.Empty(source.RequestedOffsets); // never hit the network
    }

    public void Dispose()
    {
        try
        {
            Directory.Delete(_dir, recursive: true);
        }
        catch (IOException)
        {
        }
    }
}

public sealed class ExtractorTests
{
    [Fact]
    public void ExtractsNestedTree_AndRejectsZipSlip()
    {
        var root = Path.Combine(Path.GetTempPath(), "hype-desktop-tests", Guid.NewGuid().ToString("N"));
        var zip = Path.Combine(root, "p.zip");
        Directory.CreateDirectory(root);

        using (var archive = ZipFile.Open(zip, ZipArchiveMode.Create))
        {
            var a = archive.CreateEntry("python/python.exe");
            using (var s = new StreamWriter(a.Open()))
            {
                s.Write("fake");
            }
            archive.CreateEntry("python/Lib/");
            var b = archive.CreateEntry("python/Lib/site.py");
            using (var s = new StreamWriter(b.Open()))
            {
                s.Write("x = 1");
            }
        }

        var dest = Path.Combine(root, "out");
        Extractor.Extract(zip, dest, null, CancellationToken.None);
        Assert.True(File.Exists(Path.Combine(dest, "python", "python.exe")));
        Assert.True(File.Exists(Path.Combine(dest, "python", "Lib", "site.py")));

        var evil = Path.Combine(root, "evil.zip");
        using (var archive = ZipFile.Open(evil, ZipArchiveMode.Create))
        {
            var e = archive.CreateEntry("../escape.txt");
            using var s = new StreamWriter(e.Open());
            s.Write("nope");
        }
        Assert.Throws<ShellException>(() =>
            Extractor.Extract(evil, Path.Combine(root, "out2"), null, CancellationToken.None));

        Directory.Delete(root, recursive: true);
    }
}

public sealed class LocalStateTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), "hype-desktop-tests", Guid.NewGuid().ToString("N"));
    private readonly LocalState _state;

    public LocalStateTests()
    {
        _state = new LocalState(Path.Combine(_root, "payloads"));
        Directory.CreateDirectory(_state.PayloadsDir);
    }

    private string MakeDir(string name)
    {
        var dir = _state.DirFor(name);
        Directory.CreateDirectory(dir);
        File.WriteAllText(Path.Combine(dir, "marker.txt"), name);
        return dir;
    }

    [Fact]
    public void Commit_TracksPreviousOnlyWhenVersionChanges()
    {
        var now = DateTimeOffset.UtcNow;
        _state.Commit(new InstalledComponent("env-1", "env-1"), new InstalledComponent("apps-1", "apps-1"), now);
        _state.Commit(new InstalledComponent("env-1", "env-1"), new InstalledComponent("apps-2", "apps-2"), now);

        var pointer = _state.Load()!;
        Assert.Equal("apps-2", pointer.Apps!.Version);
        Assert.Equal("apps-1", pointer.PreviousApps!.Version);
        Assert.Null(pointer.PreviousEnv); // env never changed
    }

    [Fact]
    public void Revert_SwapsBack_WhenPreviousDirsExist()
    {
        MakeDir("env-1");
        MakeDir("apps-1");
        MakeDir("apps-2");
        var now = DateTimeOffset.UtcNow;
        _state.Commit(new InstalledComponent("env-1", "env-1"), new InstalledComponent("apps-1", "apps-1"), now);
        _state.Commit(new InstalledComponent("env-1", "env-1"), new InstalledComponent("apps-2", "apps-2"), now);

        Assert.False(_state.RevertToPrevious(now)); // previousEnv is null → no full previous pair
    }

    [Fact]
    public void Revert_Works_WithFullPreviousPair()
    {
        MakeDir("env-1");
        MakeDir("env-2");
        MakeDir("apps-1");
        MakeDir("apps-2");
        var now = DateTimeOffset.UtcNow;
        _state.Commit(new InstalledComponent("env-1", "env-1"), new InstalledComponent("apps-1", "apps-1"), now);
        _state.Commit(new InstalledComponent("env-2", "env-2"), new InstalledComponent("apps-2", "apps-2"), now);

        Assert.True(_state.RevertToPrevious(now));
        var pointer = _state.Load()!;
        Assert.Equal("env-1", pointer.Env!.Version);
        Assert.Equal("env-2", pointer.PreviousEnv!.Version);
    }

    [Fact]
    public void Prune_KeepsCurrentPreviousAndInUse()
    {
        MakeDir("env-1");
        MakeDir("env-2");
        MakeDir("apps-1");
        MakeDir("apps-2");
        var orphanKeep = MakeDir("env-0");   // in use by a running app
        MakeDir("apps-0");                    // genuinely stale

        var now = DateTimeOffset.UtcNow;
        _state.Commit(new InstalledComponent("env-1", "env-1"), new InstalledComponent("apps-1", "apps-1"), now);
        _state.Commit(new InstalledComponent("env-2", "env-2"), new InstalledComponent("apps-2", "apps-2"), now);

        var deleted = _state.Prune([orphanKeep]);

        Assert.Equal(["apps-0"], deleted);
        Assert.True(Directory.Exists(_state.DirFor("env-0")));
        Assert.True(Directory.Exists(_state.DirFor("env-1"))); // previous
        Assert.True(Directory.Exists(_state.DirFor("env-2"))); // current
    }

    [Fact]
    public void SweepStaging_RemovesOrphans()
    {
        Directory.CreateDirectory(Path.Combine(_state.PayloadsDir, ".staging-abc"));
        Assert.Equal(1, _state.SweepStaging());
        Assert.Empty(Directory.GetDirectories(_state.PayloadsDir, ".staging-*"));
    }

    public void Dispose()
    {
        try
        {
            Directory.Delete(_root, recursive: true);
        }
        catch (IOException)
        {
        }
    }
}
