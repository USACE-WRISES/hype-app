using Hype.Desktop.Core;
using Hype.Desktop.Core.Processes;

namespace Hype.Desktop.Core.Tests;

public sealed class AppSupervisorTests
{
    [Fact]
    public async Task Start_BecomesRunning_WhenProbeTurnsHealthy()
    {
        using var world = new TestWorld();
        world.Probe.Handler = _ => world.Runner.Spawns.Count > 0;

        await world.Supervisor.StartAsync("easi");
        var state = await world.WaitForStatusAsync(AppStatus.Running);

        Assert.NotNull(state.Port);
        Assert.NotNull(state.Pid);
        Assert.Single(world.Runner.Spawns);
        Assert.True(world.StateStore.HasStartedOk("easi"));

        var spec = world.Runner.Spawns[0].Spec;
        Assert.Equal(world.Locator.Paths!.PythonExe, spec.ExePath);
        Assert.Contains("shiny", spec.Arguments);
        Assert.Contains("app.py", spec.Arguments);
        Assert.Equal("1", spec.Environment["HYPE_DESKTOP"]);
    }

    [Fact]
    public async Task Start_ReportsCrash_WhenProcessDiesDuringStartup()
    {
        using var world = new TestWorld();
        await world.Supervisor.StartAsync("easi");

        var spawn = world.Runner.Spawns.Single();
        spawn.OnOutput?.Invoke("Traceback (most recent call last):");
        spawn.OnOutput?.Invoke("ModuleNotFoundError: No module named 'shiny'");
        spawn.Process.MarkExited(1);

        var state = await world.WaitForStatusAsync(AppStatus.Crashed);
        Assert.Contains("exited during startup", state.Detail);
        Assert.Contains("code 1", state.Detail);
    }

    [Fact]
    public async Task Start_RetriesWithNewPort_OnBindConflict()
    {
        using var world = new TestWorld();
        world.Probe.Handler = _ => world.Runner.Spawns.Count >= 2;

        await world.Supervisor.StartAsync("easi");
        var first = world.Runner.Spawns.Single();
        first.OnOutput?.Invoke("ERROR: [Errno 10048] error while attempting to bind on address ('127.0.0.1', 55555)");
        first.Process.MarkExited(1);

        var state = await world.WaitForStatusAsync(AppStatus.Running);

        Assert.Equal(2, world.Runner.Spawns.Count);
        var firstPort = PortOf(world.Runner.Spawns[0].Spec);
        var secondPort = PortOf(world.Runner.Spawns[1].Spec);
        Assert.NotEqual(firstPort, secondPort);
        Assert.Equal(secondPort, state.Port!.Value.ToString());
    }

    [Fact]
    public async Task Start_KillsAndReportsTimeout_AtHardCap()
    {
        using var world = new TestWorld(TestWorld.FastOptions with { StartHardCap = TimeSpan.FromMilliseconds(300) });

        await world.Supervisor.StartAsync("easi");
        var state = await world.WaitForStatusAsync(AppStatus.Crashed);

        Assert.Equal("startup timed out", state.Detail);
        Assert.True(world.Runner.Spawns.Single().Process.KillTreeCalled);
    }

    [Fact]
    public async Task Stop_IsGraceful_WhenHelperWorks()
    {
        using var world = new TestWorld();
        world.Probe.Handler = _ => true;
        world.Runner.OnRunToExit = _ =>
        {
            // Simulate the Ctrl+C landing: the server drains and exits cleanly.
            world.Runner.Spawns[^1].Process.MarkExited(0);
            return Task.FromResult<int?>(0);
        };

        await world.Supervisor.StartAsync("easi");
        await world.WaitForStatusAsync(AppStatus.Running);
        await world.Supervisor.StopAsync("easi");

        var state = world.Supervisor.GetState("easi");
        Assert.Equal(AppStatus.Stopped, state.Status);
        Assert.Contains("exit code 0", state.Detail);
        Assert.False(world.Runner.Spawns.Single().Process.KillTreeCalled);

        var helper = Assert.Single(world.Runner.HelperRuns);
        Assert.Equal(world.Config.SelfExePath, helper.ExePath);
        Assert.Equal(["--stop-helper", world.Runner.Spawns.Single().Process.Pid.ToString()], helper.Arguments);
    }

    [Fact]
    public async Task Stop_EscalatesToKillTree_WhenGracefulStallsOut()
    {
        using var world = new TestWorld();
        world.Probe.Handler = _ => true;
        // Helper "succeeds" but the server ignores Ctrl+C entirely.
        world.Runner.OnRunToExit = _ => Task.FromResult<int?>(0);

        await world.Supervisor.StartAsync("easi");
        await world.WaitForStatusAsync(AppStatus.Running);
        await world.Supervisor.StopAsync("easi");

        var state = world.Supervisor.GetState("easi");
        Assert.Equal(AppStatus.Stopped, state.Status);
        Assert.Contains("forced", state.Detail);
        Assert.True(world.Runner.Spawns.Single().Process.KillTreeCalled);
    }

    [Fact]
    public async Task RunningApp_FlipsToCrashed_OnUnexpectedExit()
    {
        using var world = new TestWorld();
        world.Probe.Handler = _ => true;

        await world.Supervisor.StartAsync("easi");
        await world.WaitForStatusAsync(AppStatus.Running);

        var spawn = world.Runner.Spawns.Single();
        spawn.OnOutput?.Invoke("Fatal Python error: Aborted");
        spawn.Process.MarkExited(3);

        var state = await world.WaitForStatusAsync(AppStatus.Crashed);
        Assert.Contains("exited unexpectedly", state.Detail);
        Assert.Contains("Fatal Python error", state.Detail);
    }

    [Fact]
    public async Task Start_SetsCrashedWithMessage_WhenPayloadUnavailable()
    {
        using var world = new TestWorld();
        world.Locator.Throws = new ShellException("Dev python not found at X.");

        await world.Supervisor.StartAsync("easi");

        var state = world.Supervisor.GetState("easi");
        Assert.Equal(AppStatus.Crashed, state.Status);
        Assert.Equal("Dev python not found at X.", state.Detail);
        Assert.Empty(world.Runner.Spawns);
    }

    [Fact]
    public async Task Start_CanRecover_AfterCrash()
    {
        using var world = new TestWorld();
        await world.Supervisor.StartAsync("easi");
        world.Runner.Spawns.Single().Process.MarkExited(1);
        await world.WaitForStatusAsync(AppStatus.Crashed);

        world.Probe.Handler = _ => world.Runner.Spawns.Count >= 2;
        await world.Supervisor.StartAsync("easi");
        var state = await world.WaitForStatusAsync(AppStatus.Running);

        Assert.Equal(2, world.Runner.Spawns.Count);
        Assert.NotNull(state.Port);
    }

    private static string PortOf(ProcessSpec spec)
    {
        var args = spec.Arguments;
        var index = args.ToList().IndexOf("--port");
        Assert.True(index >= 0 && index + 1 < args.Count, "spawn args missing --port");
        return args[index + 1];
    }
}
