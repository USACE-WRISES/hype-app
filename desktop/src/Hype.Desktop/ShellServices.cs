using Hype.Desktop.Core;
using Hype.Desktop.Core.Logging;
using Hype.Desktop.Core.Manifest;
using Hype.Desktop.Core.Payload;
using Hype.Desktop.Core.Processes;

namespace Hype.Desktop;

/// <summary>Everything Program wires up for the launcher window.</summary>
internal sealed record ShellServices
{
    public required ShellConfig Config { get; init; }
    public required ILineLog ShellLog { get; init; }
    public required IPayloadLocator Locator { get; init; }
    public required Func<IReadOnlyList<AppDescriptor>, AppSupervisor> SupervisorFactory { get; init; }

    /// <summary>Null in dev mode — apps run straight from the repo .venv, no payload involved.</summary>
    public PayloadManager? PayloadManager { get; init; }

    /// <summary>Where CheckAsync fetches latest-desktop.json (null in dev mode).</summary>
    public string? ManifestUrl { get; init; }

    public required Func<IPayloadSource> SourceFactory { get; init; }

    public Version ShellVersion { get; init; } = typeof(ShellServices).Assembly.GetName().Version ?? new Version(0, 0, 0);
}
