namespace Hype.Desktop.Core;

public enum UpdateKind { Payload, Shell, Combined }

/// <summary>One banner spec: what the strip says, what the button says, which action runs.</summary>
public sealed record UpdateBanner(string Text, string Button, UpdateKind Kind);

/// <summary>
/// The single source of truth for the update banner. Two update streams exist (the app
/// payload via the manifest, the desktop shell via Velopack) and a version release makes
/// both pend at once; historically each stream wrote the one banner slot directly and the
/// last writer won, which produced back-to-back banners with near-identical labels. This
/// composes ONE banner from the combined pending state instead. Pure and UI-free so the
/// priority rules and every user-facing string are unit-testable.
/// </summary>
public static class UpdatePlanner
{
    /// <param name="payloadMb">Pending app-payload download size in MB, or null when current.</param>
    /// <param name="shellVersion">Pending shell version, or null when current/unknown.</param>
    /// <param name="shellTooOld">The manifest requires a newer shell before the payload can
    /// install (the payload plan is withheld in that state, so <paramref name="payloadMb"/>
    /// is normally null with it).</param>
    public static UpdateBanner? Compose(long? payloadMb, string? shellVersion, bool shellTooOld = false)
    {
        if (shellVersion is null)
        {
            // shellTooOld without a visible shell update is a transient release-ordering
            // skew; stay quiet and let the next check resolve it.
            if (payloadMb is null || shellTooOld)
            {
                return null;
            }
            return new UpdateBanner(
                $"A HYPE app update is ready ({payloadMb} MB). It installs in this window.",
                "Install update", UpdateKind.Payload);
        }

        if (payloadMb is not null && !shellTooOld)
        {
            return new UpdateBanner(
                $"A HYPE update is ready: the app ({payloadMb} MB) and the desktop shell ({shellVersion}).",
                "Update and restart", UpdateKind.Combined);
        }

        var tail = shellTooOld ? " The app update installs after the restart." : "";
        return new UpdateBanner(
            $"A new version of HYPE Desktop ({shellVersion}) is ready to download.{tail}",
            "Download and restart", UpdateKind.Shell);
    }
}
