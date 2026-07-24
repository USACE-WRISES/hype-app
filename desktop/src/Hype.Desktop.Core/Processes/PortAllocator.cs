using System.Net;
using System.Net.Sockets;

namespace Hype.Desktop.Core.Processes;

public static class PortAllocator
{
    /// <summary>
    /// Asks the OS for a free loopback port. The listener is closed before the app binds it, so a
    /// race is possible in principle; the supervisor closes that race by retrying on bind failure.
    /// </summary>
    public static int GetFreeLoopbackPort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        try
        {
            return ((IPEndPoint)listener.LocalEndpoint).Port;
        }
        finally
        {
            listener.Stop();
        }
    }
}
