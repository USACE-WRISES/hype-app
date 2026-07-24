namespace Hype.Desktop;

/// <summary>Applies the exe's embedded icon to windows (WinForms defaults to the generic .NET icon).</summary>
internal static class ShellIcon
{
    private static readonly Icon? Cached = Load();

    public static void Apply(Form form)
    {
        if (Cached is not null)
        {
            form.Icon = Cached;
        }
    }

    private static Icon? Load()
    {
        try
        {
            return Environment.ProcessPath is { } exe ? Icon.ExtractAssociatedIcon(exe) : null;
        }
        catch (Exception ex) when (ex is ArgumentException or System.ComponentModel.Win32Exception or IOException)
        {
            return null; // cosmetic only
        }
    }
}
