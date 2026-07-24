using Hype.Desktop.Core.Processes;

namespace Hype.Desktop.Core.Tests;

public sealed class JobObjectTests
{
    /// <summary>
    /// The load-bearing safety guarantee: closing the job handle (what the OS does when the shell
    /// process dies, however it dies) terminates every child assigned to it.
    /// </summary>
    [Fact]
    public async Task ClosingJobHandle_KillsAssignedChild()
    {
        var job = KillOnCloseJob.TryCreate();
        Assert.NotNull(job);

        var runner = new WindowsProcessRunner(job);
        // cmd's `pause` blocks forever reading its (hidden) console — a stand-in for python.exe.
        var child = runner.Start(new ProcessSpec
        {
            ExePath = Environment.GetEnvironmentVariable("COMSPEC") ?? @"C:\Windows\System32\cmd.exe",
            Arguments = ["/c", "pause"],
        });

        try
        {
            Assert.False(child.HasExited);

            job.Dispose(); // simulate shell death: last handle closes → KILL_ON_JOB_CLOSE fires

            var deadline = DateTimeOffset.UtcNow + TimeSpan.FromSeconds(5);
            while (!child.HasExited && DateTimeOffset.UtcNow < deadline)
            {
                await Task.Delay(50);
            }
            Assert.True(child.HasExited, "child survived job-object close");
        }
        finally
        {
            child.KillTree();
            child.Dispose();
        }
    }
}
