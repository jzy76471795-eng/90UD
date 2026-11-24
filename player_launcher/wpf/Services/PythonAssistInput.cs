using System.Runtime.InteropServices;
using System.Windows.Threading;

namespace DNFLogin.Services;

internal sealed class PythonAssistInput : IDisposable
{
    private const uint KeyDown = 0x100, KeyUp = 0x101, SysKeyDown = 0x104, SysKeyUp = 0x105;
    private const uint Injected = 0x10, ScanCode = 0x08, Release = 0x02;
    private const uint SellKey = 0x78, DisjointKey = 0x79;
    private readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromMilliseconds(20) };
    private readonly HookCallback callback;
    private readonly Action<int, IntPtr, string> runBatch;
    private readonly HashSet<uint> held = new(), captured = new();
    private IntPtr hook, fireWindow;
    private uint fireKey;
    private readonly PythonAutoFirePulse firePulse = new(SendScan);

    public PythonAssistInput(Action<int, IntPtr, string> runBatch)
    {
        this.runBatch = runBatch;
        callback = OnKey;
        timer.Tick += (_, _) => Tick();
    }

    internal void Refresh()
    {
        var settings = AppConfig.Settings.GameAssist;
        bool active = (settings.AutoFire || settings.Sell || settings.Disjoint || settings.InstantDoor)
            && Enumerable.Range(0, GameProcessManager.SlotCount).Any(GameProcessManager.IsRunning);
        if (hook == IntPtr.Zero && active)
        {
            // Keys already held when the hook starts must be released before use.
            for (uint key = 'A'; key <= 'Z'; key++)
                if (IsDown(key)) held.Add(key);
            if (IsDown(SellKey)) held.Add(SellKey);
            if (IsDown(DisjointKey)) held.Add(DisjointKey);
            for (uint key = 0x25; key <= 0x28; key++) if (IsDown(key)) held.Add(key);
            hook = SetWindowsHookEx(13, callback, GetModuleHandle(null), 0);
        }
        if (hook != IntPtr.Zero && !active && captured.Count == 0)
            StopHook();
    }

    private void Tick()
    {
        var settings = AppConfig.Settings.GameAssist;
        if (fireWindow == IntPtr.Zero) return;
        if (!settings.AutoFire || ConfiguredFireKey(settings) != fireKey
            || GetForegroundWindow() != fireWindow || ForegroundSlot(fireWindow) < 0 || HasModifier())
        {
            StopFire();
            return;
        }
        long now = Environment.TickCount64;
        if (!firePulse.Tick(now, settings.IntervalMs))
        {
            StopFire();
            return;
        }
    }

    private IntPtr OnKey(int code, IntPtr message, IntPtr data)
    {
        if (code < 0) return CallNextHookEx(hook, code, message, data);
        var key = Marshal.PtrToStructure<KeyboardEvent>(data);
        if ((key.Flags & Injected) != 0 || (key.VirtualKey is < 'A' or > 'Z' && key.VirtualKey != SellKey && key.VirtualKey != DisjointKey && !PythonDoorShortcut.IsDirection(key.VirtualKey)))
            return CallNextHookEx(hook, code, message, data);
        uint kind = unchecked((uint)message.ToInt64());
        if (kind is KeyUp or SysKeyUp)
        {
            held.Remove(key.VirtualKey);
            if (key.VirtualKey == fireKey) StopFire();
            if (captured.Remove(key.VirtualKey)) return new IntPtr(1);
        }
        else if (kind is KeyDown or SysKeyDown)
        {
            bool firstPress = held.Add(key.VirtualKey);
            if (captured.Contains(key.VirtualKey)) return new IntPtr(1);
            if (PythonDoorShortcut.IsDirection(key.VirtualKey))
            {
                var settings = AppConfig.Settings.GameAssist;
                IntPtr window = GetForegroundWindow();
                int slot = ForegroundSlot(window);
                string? action = PythonDoorShortcut.Action(key.VirtualKey, settings.InstantDoor, firstPress,
                    IsDown(0x12), IsDown(0x10), IsDown(0x11), IsDown(0x5B) || IsDown(0x5C), false, slot);
                if (action is not null)
                {
                    captured.Add(key.VirtualKey);
                    timer.Dispatcher.BeginInvoke(() => runBatch(slot, window, action));
                    return new IntPtr(1);
                }
            }
            if (firstPress && !HasModifier())
            {
                var settings = AppConfig.Settings.GameAssist;
                IntPtr window = GetForegroundWindow();
                int slot = ForegroundSlot(window);
                if (slot >= 0)
                {
                    if ((key.VirtualKey == SellKey && settings.Sell) || (key.VirtualKey == DisjointKey && settings.Disjoint))
                    {
                        captured.Add(key.VirtualKey);
                        string action = key.VirtualKey == SellKey ? "sell" : "disjoint";
                        timer.Dispatcher.BeginInvoke(() => runBatch(slot, window, action));
                        return new IntPtr(1);
                    }
                    if (settings.AutoFire && key.VirtualKey == ConfiguredFireKey(settings)
                        && key.Scan > 0 && firePulse.Start((ushort)key.Scan,
                            Environment.TickCount64, settings.IntervalMs))
                    {
                        captured.Add(key.VirtualKey);
                        fireWindow = window;
                        fireKey = key.VirtualKey;
                        timer.Start();
                        return new IntPtr(1);
                    }
                }
            }
        }
        return CallNextHookEx(hook, code, message, data);
    }

    internal static int ForegroundSlot(IntPtr window)
    {
        if (window == IntPtr.Zero || GetForegroundWindow() != window) return -1;
        GetWindowThreadProcessId(window, out uint pid);
        for (int slot = 0; slot < GameProcessManager.SlotCount; slot++)
        {
            var process = GameProcessManager.GetProcess(slot);
            if (process is null) continue;
            try { if (process.Id == pid && !process.HasExited) return slot; }
            catch (InvalidOperationException) { }
        }
        return -1;
    }

    private static uint ConfiguredFireKey(GameAssistSettings settings) =>
        settings.FireKey is { Length: 1 } key && key[0] is >= 'A' and <= 'Z' ? key[0] : 'X';

    private static bool HasModifier() =>
        IsDown(0x10) || IsDown(0x11) || IsDown(0x12) || IsDown(0x5B) || IsDown(0x5C);

    private static bool IsDown(uint key) => (GetAsyncKeyState((int)key) & 0x8000) != 0;

    private static bool SendScan(ushort scan, bool release)
    {
        var input = new Input { Type = 1, Data = new InputData {
            Key = new KeyboardInput { Scan = scan, Flags = ScanCode | (release ? Release : 0) }
        } };
        return SendInput(1, new[] { input }, Marshal.SizeOf<Input>()) == 1;
    }

    private void StopFire()
    {
        timer.Stop();
        firePulse.Stop();
        fireWindow = IntPtr.Zero;
    }

    private void StopHook()
    {
        if (hook != IntPtr.Zero) UnhookWindowsHookEx(hook);
        hook = IntPtr.Zero;
        StopFire();
        held.Clear();
        captured.Clear();
    }

    public void Dispose()
    {
        StopHook();
    }

    [StructLayout(LayoutKind.Sequential)] private struct KeyboardEvent
    {
        public uint VirtualKey, Scan, Flags, Time;
        public UIntPtr Extra;
    }
    [StructLayout(LayoutKind.Sequential)] private struct Input
    {
        public uint Type;
        public InputData Data;
    }
    [StructLayout(LayoutKind.Explicit)] private struct InputData
    {
        [FieldOffset(0)] public KeyboardInput Key;
        [FieldOffset(0)] public MouseInput Mouse;
    }
    [StructLayout(LayoutKind.Sequential)] private struct KeyboardInput
    {
        public ushort VirtualKey, Scan;
        public uint Flags, Time;
        public UIntPtr Extra;
    }
    [StructLayout(LayoutKind.Sequential)] private struct MouseInput
    {
        public int X, Y;
        public uint Data, Flags, Time;
        public UIntPtr Extra;
    }
    private delegate IntPtr HookCallback(int code, IntPtr message, IntPtr data);
    [DllImport("user32.dll")] private static extern IntPtr SetWindowsHookEx(int id, HookCallback callback, IntPtr module, uint thread);
    [DllImport("user32.dll")] private static extern bool UnhookWindowsHookEx(IntPtr hook);
    [DllImport("user32.dll")] private static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr message, IntPtr data);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr GetModuleHandle(string? module);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")] private static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll")] private static extern uint SendInput(uint count, Input[] input, int size);
}
