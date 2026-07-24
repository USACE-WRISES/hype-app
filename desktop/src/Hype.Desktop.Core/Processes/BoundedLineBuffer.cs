namespace Hype.Desktop.Core.Processes;

/// <summary>Thread-safe ring of the most recent output lines, for crash dialogs and bind-error detection.</summary>
public sealed class BoundedLineBuffer(int capacity)
{
    private readonly Lock _lock = new();
    private readonly Queue<string> _lines = new();

    public void Add(string line)
    {
        lock (_lock)
        {
            _lines.Enqueue(line);
            while (_lines.Count > capacity)
            {
                _lines.Dequeue();
            }
        }
    }

    public IReadOnlyList<string> Snapshot()
    {
        lock (_lock)
        {
            return [.. _lines];
        }
    }

    public void Clear()
    {
        lock (_lock)
        {
            _lines.Clear();
        }
    }

    public bool ContainsAny(IReadOnlyList<string> markers)
    {
        lock (_lock)
        {
            foreach (var line in _lines)
            {
                foreach (var marker in markers)
                {
                    if (line.Contains(marker, StringComparison.OrdinalIgnoreCase))
                    {
                        return true;
                    }
                }
            }
            return false;
        }
    }
}
