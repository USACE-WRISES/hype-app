using Hype.Desktop.Core;
using Hype.Desktop.Core.Processes;

namespace Hype.Desktop.Core.Tests;

internal sealed class FakeAppProcess : IAppProcess
{
    private static int _nextPid = 5000;

    public int Pid { get; } = Interlocked.Increment(ref _nextPid);
    public bool HasExited { get; private set; }
    public int? ExitCode { get; private set; }
    public bool KillTreeCalled { get; private set; }

    public event Action? Exited;

    public void MarkExited(int code)
    {
        if (HasExited)
        {
            return;
        }
        ExitCode = code;
        HasExited = true;
        Exited?.Invoke();
    }

    public void KillTree()
    {
        KillTreeCalled = true;
        MarkExited(-1);
    }

    public void Dispose()
    {
    }
}

internal sealed class FakeRunner : IProcessRunner
{
    public sealed record Spawn(ProcessSpec Spec, FakeAppProcess Process, Action<string>? OnOutput);

    public List<Spawn> Spawns { get; } = [];
    public List<ProcessSpec> HelperRuns { get; } = [];

    /// <summary>Customize the process handed back per spawn (defaults to a fresh healthy-looking fake).</summary>
    public Func<ProcessSpec, FakeAppProcess>? OnStart { get; set; }

    /// <summary>Stop-helper behavior; default reports success without side effects.</summary>
    public Func<ProcessSpec, Task<int?>>? OnRunToExit { get; set; }

    public IAppProcess Start(ProcessSpec spec, Action<string>? onOutputLine = null)
    {
        var process = OnStart?.Invoke(spec) ?? new FakeAppProcess();
        Spawns.Add(new Spawn(spec, process, onOutputLine));
        return process;
    }

    public Task<int?> RunToExitAsync(ProcessSpec spec, TimeSpan timeout, CancellationToken ct = default)
    {
        HelperRuns.Add(spec);
        return OnRunToExit?.Invoke(spec) ?? Task.FromResult<int?>(0);
    }
}

internal sealed class FakeProbe : IHealthProbe
{
    public Func<int, bool> Handler { get; set; } = _ => false;

    public Task<bool> IsHealthyAsync(int port, CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();
        return Task.FromResult(Handler(port));
    }
}

internal sealed class FakeLocator : IPayloadLocator
{
    public PayloadPaths? Paths { get; set; }
    public ShellException? Throws { get; set; }

    public PayloadPaths Resolve() =>
        Throws is not null ? throw Throws : Paths ?? throw new InvalidOperationException("FakeLocator unconfigured");
}
