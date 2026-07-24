namespace Hype.Desktop.Core.Navigation;

public enum NavAction
{
    /// <summary>Let the WebView handle it (same app, same origin).</summary>
    Allow,

    /// <summary>Cancel and launch/focus the named HYPE app's window.</summary>
    OpenApp,

    /// <summary>Cancel and focus the launcher window.</summary>
    FocusLauncher,

    /// <summary>Cancel and hand the URL to the system browser.</summary>
    OpenExternal,

    /// <summary>Cancel and do nothing.</summary>
    Suppress,
}

public sealed record NavDecision(NavAction Action, string? AppId = null, string? Url = null, string? Query = null);

/// <summary>
/// The one place that decides what any URL does inside shell windows. Pure logic:
/// WebView2 event handlers translate their args into a call here and act on the answer.
/// The apps' cross-links arrive as hype-desktop:// URIs because the shell injects
/// HYPE_LINKS_OVERRIDES (see AppEnvironment.LinksOverridesJson).
/// </summary>
public static class NavigationPolicy
{
    public const string Scheme = "hype-desktop";

    /// <param name="url">Target URL of the navigation or new-window request.</param>
    /// <param name="ownPort">Loopback port of the window's own app, if this is an app window.</param>
    /// <param name="portToAppId">Live map of loopback port → app id for all running apps.</param>
    public static NavDecision Decide(string url, int? ownPort, IReadOnlyDictionary<int, string> portToAppId)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            return new NavDecision(NavAction.Suppress);
        }

        if (uri.Scheme.Equals(Scheme, StringComparison.OrdinalIgnoreCase))
        {
            // hype-desktop://home → host "home"; hype-desktop://app/easi → host "app", path "/easi"
            if (uri.Host.Equals("home", StringComparison.OrdinalIgnoreCase))
            {
                return new NavDecision(NavAction.FocusLauncher);
            }
            if (uri.Host.Equals("app", StringComparison.OrdinalIgnoreCase))
            {
                var appId = uri.AbsolutePath.Trim('/');
                if (appId.Length > 0)
                {
                    return new NavDecision(NavAction.OpenApp, AppId: appId, Query: NormalizeQuery(uri.Query));
                }
            }
            return new NavDecision(NavAction.Suppress);
        }

        if (uri.Scheme is "http" or "https")
        {
            if (IsLoopback(uri))
            {
                if (ownPort is { } own && uri.Port == own)
                {
                    return new NavDecision(NavAction.Allow);
                }
                if (portToAppId.TryGetValue(uri.Port, out var appId))
                {
                    return new NavDecision(NavAction.OpenApp, AppId: appId, Query: NormalizeQuery(uri.Query));
                }
                // Unknown local server — likely a stale port from a previous session. Don't
                // navigate the app window away; don't open a browser to a dead port either.
                return new NavDecision(NavAction.Suppress);
            }
            return new NavDecision(NavAction.OpenExternal, Url: uri.ToString());
        }

        if (uri.Scheme is "about" or "blob" or "data")
        {
            return new NavDecision(NavAction.Allow);
        }

        return new NavDecision(NavAction.Suppress);
    }

    private static bool IsLoopback(Uri uri) =>
        uri.IsLoopback;

    /// <summary>
    /// The deep-link query (including the leading '?') to carry to the opened app window, or
    /// null when there is none. Cross-app links like <c>hype-desktop://app/deep/?assessment=x</c>
    /// use this so the target app's loopback URL becomes <c>.../?assessment=x</c> and its startup
    /// URL-param handler runs (e.g. DEEP preloading a library assessment).
    /// </summary>
    private static string? NormalizeQuery(string query) =>
        string.IsNullOrEmpty(query) || query == "?" ? null : query;
}
