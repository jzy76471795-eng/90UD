using AduSkin.Controls.Metro;
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;

namespace DNFLogin {public partial class MainWindow : AduWindow {
public MainWindow()
{
    InitializeComponent();
    Title = "";
    Title = Services.LauncherPackage.Current?.DisplayName ?? "";
    SetDefaultBackground();
}
private void Window_Loaded(object sender, RoutedEventArgs e)
{
    _ = RunDemoFlowAsync();
}
protected override void OnContentRendered(EventArgs e)
{
    base.OnContentRendered(e);
    if (_hasShown)
    {
        return;
    }

    _hasShown = true;
    FadeInWindow();
}
private void RootContainer_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
{
    if (e.ButtonState == MouseButtonState.Pressed)
    {
        DragMove();
    }
}
private void FadeInWindow()
{
    if (Opacity >= 1)
    {
        return;
    }

    var animation = new DoubleAnimation
    {
        To = 1,
        Duration = TimeSpan.FromMilliseconds(320),
        EasingFunction = new CubicEase
        {
            EasingMode = EasingMode.EaseOut
        }
    };
    BeginAnimation(OpacityProperty, animation);
}
private ImageBrush? TryGetBackgroundBrush()
{
    if (_backgroundBrush is not null)
    {
        return _backgroundBrush;
    }

    _backgroundBrush = FindName("BackgroundImageBrush") as ImageBrush;
    return _backgroundBrush;
}
private void SetDefaultBackground(){if(TryGetBackgroundBrush() is ImageBrush brush)brush.ImageSource=Services.ClientImages.Read("bg-default-Bg1.jpg",1280);}
private async Task RunDemoFlowAsync(){await Task.Delay(100);OpenLoginWindow();}
private void OpenLoginWindow()
{
    if (_loginWindowOpened)
    {
        return;
    }

    _loginWindowOpened = true;
    App.LogStartup("OpenLoginWindow: 开始创建 LoginWindow（当前线程 UI=" + CheckAccess() + "）");
    try
    {
        // 注意：必须同步创建并 Show 之后再 Close 旧窗口，
        // 若用 Dispatcher.InvokeAsync 异步切换，可能旧窗口关闭后应用处于零窗口状态触发 OnLastWindowClose 退出
        var loginWindow = new LoginWindow();
        App.LogStartup("OpenLoginWindow: LoginWindow 构造完成");
        Application.Current.MainWindow = loginWindow;
        loginWindow.Show();
        App.LogStartup("OpenLoginWindow: LoginWindow.Show() 已调用");
    }
    catch (Exception ex)
    {
        App.LogStartup("OpenLoginWindow: 创建/显示异常 -> " + ex);
        throw;
    }

    Close();
    App.LogStartup("OpenLoginWindow: 旧 MainWindow 已关闭");
}
private ImageBrush? _backgroundBrush;
private bool _hasShown;
private bool _loginWindowOpened;
}}