using System.Runtime.InteropServices;

namespace Hype.Desktop.Core.Processes;

/// <summary>
/// Implements `HypeDesktop.exe --stop-helper &lt;pid&gt;`: delivers a real Ctrl+C to a child process so
/// uvicorn/shiny shut down gracefully. A GUI parent cannot send console control events directly —
/// the trick is a throwaway helper process (this re-invoked exe) that attaches to the child's
/// private hidden console (it has one because it was spawned with CREATE_NO_WINDOW), immunizes
/// itself, and raises CTRL_C_EVENT for that console's process group.
/// </summary>
public static partial class StopHelper
{
    public const int ExitOk = 0;
    public const int ExitAttachFailed = 2;
    public const int ExitSignalFailed = 3;

    public static int Run(int pid)
    {
        _ = FreeConsole(); // GUI exe normally has none; ignore result

        if (!AttachConsole((uint)pid))
        {
            return ExitAttachFailed;
        }

        // Ignore the Ctrl+C we are about to raise so it only affects the target's tree.
        _ = SetConsoleCtrlHandler(IntPtr.Zero, add: true);

        var ok = GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0);
        _ = FreeConsole();
        return ok ? ExitOk : ExitSignalFailed;
    }

    private const uint CTRL_C_EVENT = 0;

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool FreeConsole();

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool AttachConsole(uint dwProcessId);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool SetConsoleCtrlHandler(IntPtr handlerRoutine, [MarshalAs(UnmanagedType.Bool)] bool add);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool GenerateConsoleCtrlEvent(uint dwCtrlEvent, uint dwProcessGroupId);
}
