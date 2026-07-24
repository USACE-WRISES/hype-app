using System.Diagnostics;
using System.Text;

namespace Hype.Desktop.Core.Processes;

public sealed record ProcessSpec
{
    public required string ExePath { get; init; }
    public required IReadOnlyList<string> Arguments { get; init; }
    public string? WorkingDirectory { get; init; }

    /// <summary>Overrides applied on top of the inherited environment; a null value removes the variable.</summary>
    public IReadOnlyDictionary<string, string?> Environment { get; init; } =
        new Dictionary<string, string?>();
}

/// <summary>A live child process as the supervisor sees it.</summary>
public interface IAppProcess : IDisposable
{
    int Pid { get; }
    bool HasExited { get; }
    int? ExitCode { get; }

    /// <summary>May fire on any thread; may never fire if the process outlives interest. Poll HasExited for certainty.</summary>
    event Action? Exited;

    void KillTree();
}

public interface IProcessRunner
{
    /// <summary>Starts a long-lived child. onOutputLine (merged stdout+stderr) is wired before the first byte can arrive.</summary>
    IAppProcess Start(ProcessSpec spec, Action<string>? onOutputLine = null);

    /// <summary>Runs a short-lived helper to completion. Returns its exit code, or null on failure/timeout (the helper is killed).</summary>
    Task<int?> RunToExitAsync(ProcessSpec spec, TimeSpan timeout, CancellationToken ct = default);
}

/// <summary>
/// Real implementation: CREATE_NO_WINDOW children (which gives each a private hidden console —
/// required by the graceful-stop helper), merged output events, and assignment into the
/// kill-on-close job object so shell death can never orphan a python server.
/// </summary>
public sealed class WindowsProcessRunner(KillOnCloseJob? job, Action<string>? log = null) : IProcessRunner
{
    public IAppProcess Start(ProcessSpec spec, Action<string>? onOutputLine = null)
    {
        var process = new Process { StartInfo = BuildStartInfo(spec), EnableRaisingEvents = true };
        var wrapper = new RealAppProcess(process);

        if (onOutputLine is not null)
        {
            process.OutputDataReceived += (_, e) => { if (e.Data is not null) { onOutputLine(e.Data); } };
            process.ErrorDataReceived += (_, e) => { if (e.Data is not null) { onOutputLine(e.Data); } };
        }
        process.Exited += (_, _) => wrapper.RaiseExited();

        if (!process.Start())
        {
            process.Dispose();
            throw new ShellException($"Windows refused to start {spec.ExePath}.");
        }

        job?.TryAssign(process, log);
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        return wrapper;
    }

    public async Task<int?> RunToExitAsync(ProcessSpec spec, TimeSpan timeout, CancellationToken ct = default)
    {
        using var process = new Process { StartInfo = BuildStartInfo(spec, redirectOutput: false) };
        try
        {
            if (!process.Start())
            {
                return null;
            }
        }
        catch (System.ComponentModel.Win32Exception)
        {
            return null;
        }
        // Deliberately NOT assigned to the job: helpers are short-lived and killed on timeout anyway.
        using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutCts.CancelAfter(timeout);
        try
        {
            await process.WaitForExitAsync(timeoutCts.Token).ConfigureAwait(false);
            return process.ExitCode;
        }
        catch (OperationCanceledException)
        {
            try { process.Kill(entireProcessTree: true); }
            catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception or AggregateException) { }
            return null;
        }
    }

    private static ProcessStartInfo BuildStartInfo(ProcessSpec spec, bool redirectOutput = true)
    {
        var psi = new ProcessStartInfo
        {
            FileName = spec.ExePath,
            WorkingDirectory = spec.WorkingDirectory ?? "",
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = redirectOutput,
            RedirectStandardError = redirectOutput,
        };
        if (redirectOutput)
        {
            psi.StandardOutputEncoding = Encoding.UTF8;
            psi.StandardErrorEncoding = Encoding.UTF8;
        }
        foreach (var arg in spec.Arguments)
        {
            psi.ArgumentList.Add(arg);
        }
        foreach (var (key, value) in spec.Environment)
        {
            if (value is null)
            {
                psi.Environment.Remove(key);
            }
            else
            {
                psi.Environment[key] = value;
            }
        }
        return psi;
    }

    private sealed class RealAppProcess(Process process) : IAppProcess
    {
        public event Action? Exited;

        public int Pid => process.Id;

        public bool HasExited
        {
            get
            {
                try { return process.HasExited; }
                catch (InvalidOperationException) { return true; }
            }
        }

        public int? ExitCode
        {
            get
            {
                try { return process.HasExited ? process.ExitCode : null; }
                catch (InvalidOperationException) { return null; }
            }
        }

        public void RaiseExited() => Exited?.Invoke();

        public void KillTree()
        {
            try
            {
                process.Kill(entireProcessTree: true);
            }
            catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception or AggregateException)
            {
                // Already exited or access lost — nothing further to do.
            }
        }

        public void Dispose() => process.Dispose();
    }
}
