using System.Text;

namespace Hype.Desktop.Core.Logging;

public interface ILineLog : IDisposable
{
    string Path { get; }
    void WriteLine(string line);
}

/// <summary>
/// Timestamped, size-rotated line log (base + .1 + .2). Every write is flushed so the log is
/// useful after a crash — supportability on locked-down machines beats write throughput here.
/// </summary>
public sealed class RollingLogWriter : ILineLog
{
    private readonly Lock _lock = new();
    private readonly long _maxBytes;
    private readonly int _keep;
    private StreamWriter? _writer;
    private bool _disposed;

    public string Path { get; }

    public RollingLogWriter(string path, long maxBytes = 5_000_000, int keep = 3)
    {
        Path = path;
        _maxBytes = maxBytes;
        _keep = Math.Max(1, keep);
    }

    public void WriteLine(string line)
    {
        lock (_lock)
        {
            if (_disposed)
            {
                return;
            }
            try
            {
                _writer ??= Open();
                _writer.WriteLine($"{DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss.fff} {line}");
                if (_writer.BaseStream.Length > _maxBytes)
                {
                    Rotate();
                }
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                // Logging must never take the shell down.
            }
        }
    }

    private StreamWriter Open()
    {
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(Path)!);
        // ReadWrite sharing: the single-instance mutex already guarantees one long-lived writer,
        // so wider sharing costs nothing — and it stops short-lived siblings (--stop-helper, log
        // viewers, probes) from being locked out while the shell holds the file. A pre-existing
        // restrictive foreign handle still wins at open time; the per-line Open() retry above
        // means logging self-heals the moment such a holder lets go.
        return new StreamWriter(
            new FileStream(Path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false))
        { AutoFlush = true };
    }

    private void Rotate()
    {
        _writer?.Dispose();
        _writer = null;
        var oldest = $"{Path}.{_keep - 1}";
        if (File.Exists(oldest))
        {
            File.Delete(oldest);
        }
        for (var i = _keep - 2; i >= 1; i--)
        {
            var from = $"{Path}.{i}";
            if (File.Exists(from))
            {
                File.Move(from, $"{Path}.{i + 1}", overwrite: true);
            }
        }
        File.Move(Path, $"{Path}.1", overwrite: true);
        _writer = Open();
    }

    public void Dispose()
    {
        lock (_lock)
        {
            _disposed = true;
            _writer?.Dispose();
            _writer = null;
        }
    }
}

/// <summary>Creates and caches one rolling log per name under the logs directory.</summary>
public sealed class LogFactory(string logsDir) : IDisposable
{
    private readonly Lock _lock = new();
    private readonly Dictionary<string, RollingLogWriter> _logs = new(StringComparer.OrdinalIgnoreCase);

    public ILineLog For(string name)
    {
        lock (_lock)
        {
            if (!_logs.TryGetValue(name, out var log))
            {
                log = new RollingLogWriter(Path.Combine(logsDir, $"{name}.log"));
                _logs[name] = log;
            }
            return log;
        }
    }

    public void Dispose()
    {
        lock (_lock)
        {
            foreach (var log in _logs.Values)
            {
                log.Dispose();
            }
            _logs.Clear();
        }
    }
}
