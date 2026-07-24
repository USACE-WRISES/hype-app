namespace Hype.Desktop.Core;

/// <summary>An error with a message suitable for showing directly to the user.</summary>
public sealed class ShellException(string userMessage, Exception? inner = null)
    : Exception(userMessage, inner);
