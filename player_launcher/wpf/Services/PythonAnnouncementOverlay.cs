#if PYTHON90
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;

namespace DNFLogin.Services;

// Click-through, no-activation banner follows only a game launched by this launcher.
// This display path never patches DFO memory or introduces a game packet decoder.
public sealed class PythonAnnouncementOverlay : Window
{
    [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr window, out Rect rect);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr window);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr window);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongW")] private static extern int GetWindowStyle(IntPtr window, int index);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongW")] private static extern int SetWindowStyle(IntPtr window, int index, int value);
    [DllImport("user32.dll")] private static extern IntPtr GetDC(IntPtr window);
    [DllImport("user32.dll")] private static extern int ReleaseDC(IntPtr window, IntPtr dc);
    [DllImport("gdi32.dll")] private static extern int GetDeviceCaps(IntPtr dc, int index);
    private static uint WindowDpi(IntPtr window)
    {
        if(OperatingSystem.IsWindowsVersionAtLeast(10,0,14393)) return GetDpiForWindow(window);
        var dc=GetDC(window);if(dc==IntPtr.Zero)return 96;
        try{return (uint)Math.Max(96,GetDeviceCaps(dc,88));}finally{ReleaseDC(window,dc);}
    }
    private readonly IntPtr target;
    private readonly DispatcherTimer timer;
    private DateTime? expires;
    private bool displayed;
    public event Action? FirstDisplayed;

    public PythonAnnouncementOverlay(IntPtr target, string text, uint aabbggrr)
    {
        this.target = target;
        WindowStyle = WindowStyle.None; ResizeMode = ResizeMode.NoResize;
        AllowsTransparency = true; Background = Brushes.Transparent;
        ShowActivated = false; ShowInTaskbar = false; Topmost = true;
        SizeToContent = SizeToContent.Height; MinHeight = 48; MaxHeight = 180; IsHitTestVisible = false;
        var color = Color.FromArgb(255, (byte)aabbggrr, (byte)(aabbggrr >> 8), (byte)(aabbggrr >> 16));
        Content = new Border { Background = new SolidColorBrush(Color.FromArgb(160, 12, 12, 18)), CornerRadius = new CornerRadius(6), Padding = new Thickness(12, 8, 12, 8), Child = new TextBlock { Text = text, Foreground = new SolidColorBrush(color), FontSize = 20, FontWeight = FontWeights.SemiBold, TextAlignment = TextAlignment.Center, TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center } };
        SourceInitialized += (_, _) =>
        {
            var handle = new WindowInteropHelper(this).Handle;
            var style = GetWindowStyle(handle, -20);
            SetWindowStyle(handle, -20, style | 0x20 | 0x80 | 0x08000000);
        };
        timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(200) };
        timer.Tick += (_, _) => UpdatePosition();
        Closed += (_, _) => timer.Stop();
    }

    public bool Begin()
    {
        if (!UpdatePosition()) return false;
        timer.Start();
        return IsVisible;
    }

    private bool UpdatePosition()
    {
        if ((expires.HasValue && DateTime.UtcNow >= expires.Value) || !IsWindow(target)) { Close(); return false; }
        if (IsIconic(target) || GetForegroundWindow() != target || !GetWindowRect(target, out var bounds)) { Hide(); return true; }
        var dpi = Math.Max(96u, WindowDpi(target)); var scale = dpi / 96.0;
        Width = Math.Max(240, Math.Min(1000, (bounds.Right - bounds.Left - 50) / scale));
        Left = bounds.Left / scale + ((bounds.Right - bounds.Left) / scale - Width) / 2;
        Top = bounds.Top / scale + 65;
        if (!IsVisible) Show();
        if (!displayed) { displayed = true; expires = DateTime.UtcNow.AddSeconds(8); FirstDisplayed?.Invoke(); }
        return true;
    }
}
#endif
