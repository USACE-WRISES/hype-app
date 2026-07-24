namespace Hype.Desktop.Core.Payload;

public sealed class PayloadStream(Stream stream, long? totalLength, bool resumedFromOffset) : IDisposable
{
    public Stream Stream { get; } = stream;

    /// <summary>Total length of the FULL resource (not the remaining range), when the source knows it.</summary>
    public long? TotalLength { get; } = totalLength;

    /// <summary>False when a nonzero offset was requested but the source restarted from zero.</summary>
    public bool ResumedFromOffset { get; } = resumedFromOffset;

    public void Dispose() => Stream.Dispose();
}

/// <summary>Where payload bytes come from: GitHub release assets in production, local files for the offline bundle and tests.</summary>
public interface IPayloadSource : IDisposable
{
    Task<PayloadStream> OpenAsync(string url, long offset, CancellationToken ct);
}

/// <summary>
/// HTTP source with Range-resume. Follows redirects (GitHub release downloads redirect to
/// objects.githubusercontent.com — the manifest URL allowlist governs the *starting* URL).
/// </summary>
public sealed class HttpPayloadSource : IPayloadSource
{
    private readonly HttpClient _client;

    public HttpPayloadSource(TimeSpan? connectTimeout = null)
    {
        _client = new HttpClient(new SocketsHttpHandler
        {
            ConnectTimeout = connectTimeout ?? TimeSpan.FromSeconds(30),
            AutomaticDecompression = System.Net.DecompressionMethods.None,
        })
        {
            Timeout = Timeout.InfiniteTimeSpan, // long downloads; cancellation governs
        };
    }

    public async Task<PayloadStream> OpenAsync(string url, long offset, CancellationToken ct)
    {
        var request = new HttpRequestMessage(HttpMethod.Get, url);
        if (offset > 0)
        {
            request.Headers.Range = new System.Net.Http.Headers.RangeHeaderValue(offset, null);
        }

        var response = await _client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct).ConfigureAwait(false);
        try
        {
            response.EnsureSuccessStatusCode();
        }
        catch (HttpRequestException)
        {
            response.Dispose();
            throw;
        }

        var resumed = offset > 0 && response.StatusCode == System.Net.HttpStatusCode.PartialContent;
        long? total = response.StatusCode == System.Net.HttpStatusCode.PartialContent
            ? response.Content.Headers.ContentRange?.Length
            : response.Content.Headers.ContentLength;

        var stream = await response.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
        return new PayloadStream(new ResponseOwningStream(stream, response), total, resumed);
    }

    public void Dispose() => _client.Dispose();

    /// <summary>Keeps the HttpResponseMessage alive for the life of its content stream.</summary>
    private sealed class ResponseOwningStream(Stream inner, HttpResponseMessage response) : Stream
    {
        public override bool CanRead => inner.CanRead;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => inner.Length;
        public override long Position { get => inner.Position; set => throw new NotSupportedException(); }

        public override int Read(byte[] buffer, int offset, int count) => inner.Read(buffer, offset, count);
        public override int Read(Span<byte> buffer) => inner.Read(buffer);
        public override Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken ct) =>
            inner.ReadAsync(buffer, offset, count, ct);
        public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken ct = default) =>
            inner.ReadAsync(buffer, ct);

        public override void Flush() { }
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                inner.Dispose();
                response.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}

/// <summary>Local-file source: "urls" are absolute paths or paths relative to a base directory.</summary>
public sealed class FilePayloadSource(string? baseDirectory = null) : IPayloadSource
{
    public Task<PayloadStream> OpenAsync(string url, long offset, CancellationToken ct)
    {
        var path = url.StartsWith("file://", StringComparison.OrdinalIgnoreCase)
            ? new Uri(url).LocalPath
            : url;
        if (!Path.IsPathRooted(path) && baseDirectory is not null)
        {
            path = Path.Combine(baseDirectory, path);
        }

        var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        var total = stream.Length;
        if (offset > 0)
        {
            stream.Seek(offset, SeekOrigin.Begin);
        }
        return Task.FromResult(new PayloadStream(stream, total, offset > 0));
    }

    public void Dispose()
    {
    }
}
