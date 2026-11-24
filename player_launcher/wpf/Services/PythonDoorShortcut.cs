namespace DNFLogin.Services;

internal static class PythonDoorShortcut
{
    internal static bool IsDirection(uint key) => key is >= 0x25 and <= 0x28;

    internal static string? Action(uint key, bool enabled, bool firstPress, bool alt,
        bool shift, bool control, bool windows, bool injected, int foregroundSlot)
    {
        if (!enabled || !firstPress || !alt || shift || control || windows || injected || foregroundSlot < 0)
            return null;
        return key switch { 0x25 => "door-left", 0x26 => "door-up", 0x27 => "door-right", 0x28 => "door-down", _ => null };
    }
}
