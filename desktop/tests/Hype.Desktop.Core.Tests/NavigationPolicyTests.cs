using Hype.Desktop.Core.Navigation;

namespace Hype.Desktop.Core.Tests;

public sealed class NavigationPolicyTests
{
    private static readonly Dictionary<int, string> Ports = new()
    {
        [8100] = "easi",
        [8101] = "sfari",
    };

    [Theory]
    [InlineData("hype-desktop://app/sfari", "sfari")]
    [InlineData("HYPE-DESKTOP://APP/easi", "easi")]
    [InlineData("hype-desktop://app/curves/", "curves")]
    public void HypeDesktopAppLinks_OpenTheApp(string url, string expectedApp)
    {
        var decision = NavigationPolicy.Decide(url, ownPort: 8100, Ports);
        Assert.Equal(NavAction.OpenApp, decision.Action);
        Assert.Equal(expectedApp, decision.AppId);
    }

    [Theory]
    [InlineData("hype-desktop://app/deep/?assessment=eastern-corn-belt-plains", "deep", "?assessment=eastern-corn-belt-plains")]
    [InlineData("hype-desktop://app/deep?assessment=x", "deep", "?assessment=x")]
    [InlineData("hype-desktop://app/deep/?handoff=draft123", "deep", "?handoff=draft123")]
    public void HypeDesktopAppLinks_PreserveTheDeepLinkQuery(string url, string expectedApp, string expectedQuery)
    {
        var decision = NavigationPolicy.Decide(url, ownPort: 8100, Ports);
        Assert.Equal(NavAction.OpenApp, decision.Action);
        Assert.Equal(expectedApp, decision.AppId);
        Assert.Equal(expectedQuery, decision.Query);
    }

    [Theory]
    [InlineData("hype-desktop://app/deep")]
    [InlineData("hype-desktop://app/deep/")]
    public void HypeDesktopAppLinks_WithoutQuery_HaveNullQuery(string url)
    {
        var decision = NavigationPolicy.Decide(url, ownPort: 8100, Ports);
        Assert.Equal(NavAction.OpenApp, decision.Action);
        Assert.Null(decision.Query);
    }

    [Fact]
    public void LoopbackPortLink_PreservesTheDeepLinkQuery()
    {
        var decision = NavigationPolicy.Decide("http://127.0.0.1:8101/?assessment=x", ownPort: 8100, Ports);
        Assert.Equal(NavAction.OpenApp, decision.Action);
        Assert.Equal("sfari", decision.AppId);
        Assert.Equal("?assessment=x", decision.Query);
    }

    [Fact]
    public void HomeLink_FocusesLauncher()
    {
        var decision = NavigationPolicy.Decide("hype-desktop://home", 8100, Ports);
        Assert.Equal(NavAction.FocusLauncher, decision.Action);
    }

    [Fact]
    public void SameOriginLoopback_IsAllowed()
    {
        var decision = NavigationPolicy.Decide("http://127.0.0.1:8100/session/abc/download/report.pdf", 8100, Ports);
        Assert.Equal(NavAction.Allow, decision.Action);
    }

    [Fact]
    public void OtherKnownLoopbackPort_RoutesToThatApp()
    {
        var decision = NavigationPolicy.Decide("http://127.0.0.1:8101/", 8100, Ports);
        Assert.Equal(NavAction.OpenApp, decision.Action);
        Assert.Equal("sfari", decision.AppId);
    }

    [Fact]
    public void UnknownLoopbackPort_IsSuppressed()
    {
        var decision = NavigationPolicy.Decide("http://127.0.0.1:9999/", 8100, Ports);
        Assert.Equal(NavAction.Suppress, decision.Action);
    }

    [Theory]
    [InlineData("https://github.com/USACE-WRISES/hype-app")]
    [InlineData("https://basemap.nationalmap.gov/arcgis/rest/services")]
    [InlineData("http://example.com")]
    public void ExternalHttp_GoesToSystemBrowser(string url)
    {
        var decision = NavigationPolicy.Decide(url, 8100, Ports);
        Assert.Equal(NavAction.OpenExternal, decision.Action);
        Assert.Equal(new Uri(url).ToString(), decision.Url);
    }

    [Theory]
    [InlineData("file:///C:/Windows/system32/calc.exe")]
    [InlineData("ftp://example.com/x")]
    [InlineData("not a url")]
    [InlineData("hype-desktop://app/")]
    [InlineData("hype-desktop://unknown")]
    public void EverythingElse_IsSuppressed(string url)
    {
        var decision = NavigationPolicy.Decide(url, 8100, Ports);
        Assert.Equal(NavAction.Suppress, decision.Action);
    }

    [Fact]
    public void AboutBlank_IsAllowed()
    {
        Assert.Equal(NavAction.Allow, NavigationPolicy.Decide("about:blank", 8100, Ports).Action);
    }

    [Fact]
    public void LauncherWindow_HasNoOwnPort_SameLogicApplies()
    {
        var toApp = NavigationPolicy.Decide("http://127.0.0.1:8100/", ownPort: null, Ports);
        Assert.Equal(NavAction.OpenApp, toApp.Action);
        Assert.Equal("easi", toApp.AppId);
    }
}
