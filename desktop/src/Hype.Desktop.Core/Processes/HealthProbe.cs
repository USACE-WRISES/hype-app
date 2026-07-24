namespace Hype.Desktop.Core.Processes;

public interface IHealthProbe
{
    Task<bool> IsHealthyAsync(int port, CancellationToken ct);
}

/// <summary>
/// GET http://127.0.0.1:port/ — any HTTP status below 500 means the Shiny server is up.
/// Connection refused is the normal "still importing geopandas" signal, not an error.
/// The client explicitly bypasses proxies: on PAC-configured government networks a proxied
/// loopback request would fail even though the server is fine.
/// </summary>
public sealed class HttpHealthProbe : IHealthProbe, IDisposable
{
    private readonly HttpClient _client = new(new SocketsHttpHandler { UseProxy = false })
    {
        Timeout = TimeSpan.FromSeconds(2),
    };

    public async Task<bool> IsHealthyAsync(int port, CancellationToken ct)
    {
        try
        {
            using var response = await _client
                .GetAsync($"http://127.0.0.1:{port}/", HttpCompletionOption.ResponseHeadersRead, ct)
                .ConfigureAwait(false);
            return (int)response.StatusCode < 500;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or OperationCanceledException)
        {
            ct.ThrowIfCancellationRequested();
            return false;
        }
    }

    public void Dispose() => _client.Dispose();
}
