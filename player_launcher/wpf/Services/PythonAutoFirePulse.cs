namespace DNFLogin.Services;

// A press and its release must span separate timer samples. Keeping this
// state outside the hook also guarantees a release when focus/settings change.
internal sealed class PythonAutoFirePulse(Func<ushort, bool, bool> send)
{
    private ushort scan;
    private bool down;
    private long next;

    private static int Interval(int value) => Math.Clamp(value, 50, 200);
    private static int Hold(int interval) => Math.Clamp(interval / 2, 25, 80);

    internal bool Start(ushort keyScan, long now, int interval)
    {
        Stop();
        if (keyScan == 0 || !send(keyScan, false)) return false;
        scan = keyScan;
        down = true;
        next = now + Hold(Interval(interval));
        return true;
    }

    internal bool Tick(long now, int interval)
    {
        if (scan == 0) return false;
        if (now < next) return true;
        int period = Interval(interval);
        bool release = down;
        if (!send(scan, release))
        {
            Stop();
            return false;
        }
        down = !release;
        next = now + (release ? period - Hold(period) : Hold(period));
        return true;
    }

    internal void Stop()
    {
        if (down) send(scan, true);
        down = false;
        scan = 0;
        next = 0;
    }
}
