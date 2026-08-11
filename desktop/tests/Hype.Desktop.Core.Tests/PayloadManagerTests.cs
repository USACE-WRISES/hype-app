using System.IO.Compression;
using System.Security.Cryptography;
using System.Text.Json;
using Hype.Desktop.Core;
using Hype.Desktop.Core.Logging;
using Hype.Desktop.Core.Manifest;
using Hype.Desktop.Core.Payload;

namespace Hype.Desktop.Core.Tests;

/// <summary>
/// End-to-end payload-manager scenarios over real temp dirs with a file source: first run,
/// apps-only update, integrity failure, crash-recovery reuse, shell-version gate, offline bundle.
/// </summary>
public sealed class PayloadManagerTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), "hype-desktop-tests", Guid.NewGuid().ToString("N"));
    private readonly string _releaseDir;
    private readonly LocalState _state;
    private readonly LogFactory _logs;
    private readonly List<PayloadProgress> _progress = [];
    private IReadOnlyCollection<string> _inUse = [];

    public PayloadManagerTests()
    {
        _releaseDir = Path.Combine(_root, "release");
        Directory.CreateDirectory(_releaseDir);
        _state = new LocalState(Path.Combine(_root, "payloads"));
        _logs = new LogFactory(Path.Combine(_root, "logs"));
    }

    private PayloadManager CreateManager(Version? shellVersion = null)
    {
        var manager = new PayloadManager(
            _state,
            downloadsDir: Path.Combine(_root, "downloads"),
            shellVersion ?? new Version(1, 0, 0),
            allowedUrlPrefixes: [""],
            inUsePayloadDirs: () => _inUse,
            _logs.For("shell"));
        manager.Progress += _progress.Add;
        return manager;
    }

    /// <summary>Builds (idempotently) a zip in the fake release dir whose root contains one marker file tree.</summary>
    private (string FileName, string Sha256, long Size) MakeZip(string version, string innerPath, string content)
    {
        var stage = Path.Combine(_root, "stage", version);
        if (Directory.Exists(stage))
        {
            Directory.Delete(stage, recursive: true);
        }
        Directory.CreateDirectory(Path.Combine(stage, Path.GetDirectoryName(innerPath) ?? ""));
        File.WriteAllText(Path.Combine(stage, innerPath), content);
        var zipPath = Path.Combine(_releaseDir, $"{version}.zip");
        File.Delete(zipPath);
        ZipFile.CreateFromDirectory(stage, zipPath);
        var bytes = File.ReadAllBytes(zipPath);
        return ($"{version}.zip", Convert.ToHexString(SHA256.HashData(bytes)), bytes.Length);
    }

    private string WriteManifest(
        (string FileName, string Sha256, long Size) env, string envVersion,
        (string FileName, string Sha256, long Size) apps, string appsVersion,
        string minShell = "1.0.0")
    {
        var json = JsonSerializer.Serialize(new
        {
            schemaVersion = 1,
            minShellVersion = minShell,
            components = new
            {
                env = new { version = envVersion, url = env.FileName, sha256 = env.Sha256, sizeBytes = env.Size, installedSizeBytes = env.Size * 3, python = "3.12.11" },
                apps = new { version = appsVersion, url = apps.FileName, sha256 = apps.Sha256, sizeBytes = apps.Size, installedSizeBytes = apps.Size * 3, requiresEnv = envVersion },
            },
        }, DesktopJson.Options);
        var path = Path.Combine(_releaseDir, "latest-desktop.json");
        File.WriteAllText(path, json);
        return path;
    }

    [Fact]
    public async Task FirstRun_InstallsBothComponents_AndCommits()
    {
        var env = MakeZip("env-cp312-aaaa1111", Path.Combine("python", "python.exe"), "fake python");
        var apps = MakeZip("apps-2026.07.06-abc1234", Path.Combine("easi", "app.py"), "app = None");
        WriteManifest(env, "env-cp312-aaaa1111", apps, "apps-2026.07.06-abc1234");

        var manager = CreateManager();
        using var source = new FilePayloadSource(_releaseDir);
        var check = await manager.CheckAsync(source, "latest-desktop.json", CancellationToken.None);

        var update = Assert.IsType<CheckResult.UpdateAvailable>(check);
        Assert.NotNull(update.Plan.Env);
        Assert.NotNull(update.Plan.Apps);

        await manager.ApplyAsync(source, update.Manifest, update.Plan, CancellationToken.None);

        Assert.True(File.Exists(Path.Combine(_state.DirFor("env-cp312-aaaa1111"), "python", "python.exe")));
        Assert.True(File.Exists(Path.Combine(_state.DirFor("apps-2026.07.06-abc1234"), "easi", "app.py")));
        var pointer = _state.Load()!;
        Assert.Equal("env-cp312-aaaa1111", pointer.Env!.Version);
        Assert.Equal(PayloadPhase.Done, _progress[^1].Phase);
        Assert.Empty(Directory.GetFiles(Path.Combine(_root, "downloads"), "*.zip")); // cleaned up
    }

    [Fact]
    public async Task AppsOnlyUpdate_CarriesEnvForward_AndRecordsPrevious()
    {
        await FirstRun_InstallsBothComponents_AndCommits();
        _progress.Clear();

        var env = MakeZip("env-cp312-aaaa1111", Path.Combine("python", "python.exe"), "fake python");
        var apps2 = MakeZip("apps-2026.07.07-def5678", Path.Combine("easi", "app.py"), "app = 2");
        WriteManifest(env, "env-cp312-aaaa1111", apps2, "apps-2026.07.07-def5678");

        var manager = CreateManager();
        using var source = new FilePayloadSource(_releaseDir);
        var update = Assert.IsType<CheckResult.UpdateAvailable>(
            await manager.CheckAsync(source, "latest-desktop.json", CancellationToken.None));
        Assert.Null(update.Plan.Env); // env unchanged
        Assert.NotNull(update.Plan.Apps);

        await manager.ApplyAsync(source, update.Manifest, update.Plan, CancellationToken.None);

        var pointer = _state.Load()!;
        Assert.Equal("apps-2026.07.07-def5678", pointer.Apps!.Version);
        Assert.Equal("apps-2026.07.06-abc1234", pointer.PreviousApps!.Version);
        Assert.Equal("env-cp312-aaaa1111", pointer.Env!.Version);
    }

    [Fact]
    public async Task UpToDate_WhenPointerMatchesManifest()
    {
        await FirstRun_InstallsBothComponents_AndCommits();
        var manager = CreateManager();
        using var source = new FilePayloadSource(_releaseDir);
        Assert.IsType<CheckResult.UpToDate>(await manager.CheckAsync(source, "latest-desktop.json", CancellationToken.None));
    }

    [Fact]
    public async Task ShellTooOld_BlocksPlan()
    {
        var env = MakeZip("env-cp312-bbbb2222", Path.Combine("python", "python.exe"), "p");
        var apps = MakeZip("apps-2026.08.01-aaa0000", Path.Combine("x", "y.txt"), "z");
        WriteManifest(env, "env-cp312-bbbb2222", apps, "apps-2026.08.01-aaa0000", minShell: "2.5.0");

        var manager = CreateManager(new Version(1, 0, 0));
        using var source = new FilePayloadSource(_releaseDir);
        var result = await manager.CheckAsync(source, "latest-desktop.json", CancellationToken.None);
        var tooOld = Assert.IsType<CheckResult.ShellTooOld>(result);
        Assert.Equal("2.5.0", tooOld.RequiredShellVersion);
    }

    [Fact]
    public async Task IntegrityFailureTwice_SurfacesError_AndLeavesStateUntouched()
    {
        var env = MakeZip("env-cp312-cccc3333", Path.Combine("python", "python.exe"), "p");
        var apps = MakeZip("apps-2026.07.08-bad0000", Path.Combine("x", "y.txt"), "z");
        var manifest = WriteManifest(env, "env-cp312-cccc3333", apps, "apps-2026.07.08-bad0000");

        // Corrupt the env zip AFTER hashing it into the manifest.
        var envZip = Path.Combine(_releaseDir, env.FileName);
        var bytes = File.ReadAllBytes(envZip);
        bytes[^1] ^= 0xFF;
        File.WriteAllBytes(envZip, bytes);

        var manager = CreateManager();
        using var source = new FilePayloadSource(_releaseDir);
        var update = Assert.IsType<CheckResult.UpdateAvailable>(
            await manager.CheckAsync(source, "latest-desktop.json", CancellationToken.None));

        var ex = await Assert.ThrowsAsync<ShellException>(() =>
            manager.ApplyAsync(source, update.Manifest, update.Plan, CancellationToken.None));
        Assert.Contains("verification twice", ex.Message);
        Assert.Null(_state.Load()); // nothing committed
        Assert.False(Directory.Exists(_state.DirFor("env-cp312-cccc3333")));
        Assert.Equal(PayloadPhase.Failed, _progress[^1].Phase);
        _ = manifest;
    }

    [Fact]
    public async Task PreExtractedVersionDir_IsReused_WithoutDownload()
    {
        var env = MakeZip("env-cp312-dddd4444", Path.Combine("python", "python.exe"), "p");
        var apps = MakeZip("apps-2026.07.09-eee1111", Path.Combine("x", "y.txt"), "z");
        WriteManifest(env, "env-cp312-dddd4444", apps, "apps-2026.07.09-eee1111");

        // Simulate crash-after-extract-before-commit: env dir exists, then delete the release zip
        // so any download attempt would fail loudly.
        Directory.CreateDirectory(Path.Combine(_state.DirFor("env-cp312-dddd4444"), "python"));
        File.WriteAllText(Path.Combine(_state.DirFor("env-cp312-dddd4444"), "python", "python.exe"), "p");
        File.Delete(Path.Combine(_releaseDir, env.FileName));

        var manager = CreateManager();
        using var source = new FilePayloadSource(_releaseDir);
        var update = Assert.IsType<CheckResult.UpdateAvailable>(
            await manager.CheckAsync(source, "latest-desktop.json", CancellationToken.None));

        await manager.ApplyAsync(source, update.Manifest, update.Plan, CancellationToken.None);
        Assert.Equal("env-cp312-dddd4444", _state.Load()!.Env!.Version);
    }

    [Fact]
    public async Task InstallFromDirectory_OfflineBundle_Works()
    {
        var env = MakeZip("env-cp312-ffff5555", Path.Combine("python", "python.exe"), "p");
        var apps = MakeZip("apps-2026.07.10-fff2222", Path.Combine("easi", "app.py"), "a");
        WriteManifest(env, "env-cp312-ffff5555", apps, "apps-2026.07.10-fff2222");

        var manager = CreateManager();
        await manager.InstallFromDirectoryAsync(_releaseDir, CancellationToken.None);

        Assert.Equal("env-cp312-ffff5555", _state.Load()!.Env!.Version);
        Assert.True(File.Exists(Path.Combine(_state.DirFor("apps-2026.07.10-fff2222"), "easi", "app.py")));
    }

    [Fact]
    public async Task InstallFromDirectory_ResolvesGithubUrlsToLocalFilenames()
    {
        // The realistic offline bundle: the ONLINE manifest (GitHub URLs) + its zips in one folder.
        var env = MakeZip("env-cp312-off77777", Path.Combine("python", "python.exe"), "p");
        var apps = MakeZip("apps-2026.07.12-off8888", Path.Combine("easi", "app.py"), "a");
        var json = JsonSerializer.Serialize(new
        {
            schemaVersion = 1,
            minShellVersion = "1.0.0",
            components = new
            {
                env = new
                {
                    version = "env-cp312-off77777",
                    url = $"https://github.com/USACE-WRISES/hype-app/releases/download/x/{env.FileName}",
                    sha256 = env.Sha256, sizeBytes = env.Size, installedSizeBytes = env.Size * 3, python = "3.12",
                },
                apps = new
                {
                    version = "apps-2026.07.12-off8888",
                    url = $"https://github.com/USACE-WRISES/hype-app/releases/download/x/{apps.FileName}",
                    sha256 = apps.Sha256, sizeBytes = apps.Size, installedSizeBytes = apps.Size * 3,
                    requiresEnv = "env-cp312-off77777",
                },
            },
        }, DesktopJson.Options);
        File.WriteAllText(Path.Combine(_releaseDir, "latest-desktop.json"), json);

        var manager = CreateManager();
        await manager.InstallFromDirectoryAsync(_releaseDir, CancellationToken.None);

        Assert.Equal("env-cp312-off77777", _state.Load()!.Env!.Version);
        Assert.True(File.Exists(Path.Combine(_state.DirFor("apps-2026.07.12-off8888"), "easi", "app.py")));
    }

    [Fact]
    public async Task Prune_SkipsInUseDirs_DuringApply()
    {
        await FirstRun_InstallsBothComponents_AndCommits();

        // Stale dir that a "running app" still uses.
        var staleInUse = _state.DirFor("env-cp312-old99999");
        Directory.CreateDirectory(staleInUse);
        var staleIdle = _state.DirFor("apps-2020.01.01-dead000");
        Directory.CreateDirectory(staleIdle);
        _inUse = [staleInUse];

        var env = MakeZip("env-cp312-aaaa1111", Path.Combine("python", "python.exe"), "fake python");
        var apps2 = MakeZip("apps-2026.07.11-ggg3333", Path.Combine("easi", "app.py"), "app = 3");
        WriteManifest(env, "env-cp312-aaaa1111", apps2, "apps-2026.07.11-ggg3333");

        var manager = CreateManager();
        using var source = new FilePayloadSource(_releaseDir);
        var update = Assert.IsType<CheckResult.UpdateAvailable>(
            await manager.CheckAsync(source, "latest-desktop.json", CancellationToken.None));
        await manager.ApplyAsync(source, update.Manifest, update.Plan, CancellationToken.None);

        Assert.True(Directory.Exists(staleInUse), "in-use dir must survive prune");
        Assert.False(Directory.Exists(staleIdle), "idle stale dir should be pruned");
    }

    public void Dispose()
    {
        _logs.Dispose();
        try
        {
            Directory.Delete(_root, recursive: true);
        }
        catch (IOException)
        {
        }
    }
}
