using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using AduSkin.Controls.Metro;

namespace DNFLogin { public partial class LoginWindow : AduWindow {
private bool _isLoggingIn;

/// <summary>将窗口带到前台：窗口隐藏（托盘）时恢复显示，可见时直接激活</summary>
public void BringToFront()
{
    if (_trayIcon is not null)
    {
        RestoreFromTray();
        return;
    }

    if (WindowState == WindowState.Minimized)
    {
        WindowState = WindowState.Normal;
    }

    Show();
    Activate();
    Topmost = true;
    Topmost = false;
}

/// <summary>双击托盘图标恢复窗口</summary>
private void RestoreFromTray()
{
    if (_trayIcon is not null)
    {
        _trayIcon.Visible = false;
    }

    Show();
    WindowState = WindowState.Normal;
    Activate();
    Topmost = true;
    Topmost = false;
}

private System.Windows.Forms.NotifyIcon? _trayIcon;

private void SetupBackground() {
 _bgTimer?.Stop();_bgTimer=null;_bgImages.Clear();_playOrder.Clear();_playPos=0;_bgIndex=0;
 BackgroundLayer1.Source=BackgroundLayer2.Source=null;
 foreach(var file in Services.ClientImages.BackgroundFiles()) {
  var bitmap=Services.ClientImages.ReadFile(file,1280);
  if(bitmap is not null)_bgImages.Add(bitmap);
 }
 OnAllBackgroundLoaded();
}

private readonly List<int> _playOrder = new(); // 播放顺序（顺序=自然序，随机=洗牌后）


private readonly List<BitmapImage> _bgImages = new();

private DispatcherTimer? _bgTimer;

private int _playPos; // 当前播放位置（显示 _playOrder[_playPos]）


private int _bgIndex;

/// <summary>
/// 全部图片加载尝试完毕后的收尾：
/// 构建播放顺序并显示第一张；只有 1 张图时不轮换、进度条隐藏。
/// </summary>
private void OnAllBackgroundLoaded()
{
    if (_bgImages.Count == 0)
    {
        HideBgProgress();
        return;
    }

    RebuildPlayOrder(keepFirst: true);
    BackgroundLayer1.Source = _bgImages[_playOrder[0]];
    BackgroundLayer1.Opacity = 1;
    BackgroundLayer2.Opacity = 0;
    if (_bgImages.Count < 2)
    {
        // 只有 1 张图：不轮换，进度条消失
        HideBgProgress();
        return;
    }

    ShowBgProgress();
    StartBgRotation();
}

/// <summary>
/// 构建播放顺序：顺序轮播=按文件名自然序；随机轮播=洗牌循环。
/// keepFirst=true 时第一个位置保持 0（初始同步显示的那张），其余洗牌；
/// 一轮播完后调用 keepFirst=false 全量洗牌。
/// </summary>
private void RebuildPlayOrder(bool keepFirst = false)
{
    _playOrder.Clear();
    for (int i = 0; i < _bgImages.Count; i++)
    {
        _playOrder.Add(i);
    }

    if (!AppConfig.Settings.BackgroundShuffle || _playOrder.Count <= 1)
    {
        return;
    }

    var rng = new Random();
    int start = keepFirst ? 1 : 0;
    for (int i = _playOrder.Count - 1; i > start; i--)
    {
        int j = rng.Next(start, i + 1);
        (_playOrder[i], _playOrder[j]) = (_playOrder[j], _playOrder[i]);
    }
}

/// <summary>隐藏底部进度条</summary>
private void HideBgProgress()
{
    BgProgressBar.BeginAnimation(FrameworkElement.WidthProperty, null);
    BgProgressWrap.Visibility = Visibility.Collapsed;
}

/// <summary>显示底部进度条</summary>
private void ShowBgProgress()
{
    BgProgressWrap.Visibility = Visibility.Visible;
}

private void StartBgRotation()
{
    if (_bgTimer is not null)
    {
        return;
    }

    _bgTimer = new DispatcherTimer
    {
        Interval = TimeSpan.FromSeconds(BgRotationSeconds)
    };
    _bgTimer.Tick += OnBgRotationTick;
    _bgTimer.Start();
    // 进度条与轮换计时同步：启动即从 0 开始线性读条
    RestartBgProgress();
}

/// <summary>
/// 每 5 秒轮换：后层载入下一张图并淡入，前层淡出，纯交叉过渡无缩放。
/// 播放顺序走 _playOrder：一轮播完（随机模式）重新洗牌。
/// </summary>
private void OnBgRotationTick(object? sender, EventArgs e)
{
    if (_bgImages.Count < 2)
    {
        return;
    }

    // 切换完成：进度条重置，重新开始下一轮读条
    RestartBgProgress();
    _playPos++;
    if (_playPos >= _playOrder.Count)
    {
        // 一轮播完：随机模式重新洗牌，顺序模式回到开头
        RebuildPlayOrder();
        _playPos = 0;
    }

    _bgIndex = _playOrder[_playPos];
    BackgroundLayer2.Source = _bgImages[_bgIndex];
    BackgroundLayer2.Opacity = 0;
    var duration = TimeSpan.FromMilliseconds(BgFadeMilliseconds);
    var fadeIn = new DoubleAnimation(0, 1, duration)
    {
        EasingFunction = new CubicEase
        {
            EasingMode = EasingMode.EaseInOut
        }
    };
    var fadeOut = new DoubleAnimation(1, 0, duration)
    {
        EasingFunction = new CubicEase
        {
            EasingMode = EasingMode.EaseInOut
        }
    };
    fadeIn.Completed += (_, _) =>
    {
        // 过渡完成：前层承接新图，后层复位待用
        BackgroundLayer1.Source = BackgroundLayer2.Source;
        BackgroundLayer1.Opacity = 1;
        BackgroundLayer2.Opacity = 0;
    };
    BackgroundLayer2.BeginAnimation(UIElement.OpacityProperty, fadeIn);
    BackgroundLayer1.BeginAnimation(UIElement.OpacityProperty, fadeOut);
}

private const int BgFadeMilliseconds = 1200; // 交叉淡入淡出时长


/// <summary>
/// 重置并重启底部进度条：从 0 线性增长到窗口宽度，时长与轮换周期一致（5 秒）。
/// 无 EasingFunction 即线性增长。
/// </summary>
private void RestartBgProgress()
{
    if (BgProgressBar is null || BgProgressWrap.Visibility != Visibility.Visible)
    {
        return;
    }

    var targetWidth = Math.Max(RootContainer.ActualWidth, 1);
    // 停止旧动画并归零，避免残留值
    BgProgressBar.BeginAnimation(FrameworkElement.WidthProperty, null);
    BgProgressBar.Width = 0;
    var animation = new DoubleAnimation(0, targetWidth, TimeSpan.FromSeconds(BgRotationSeconds));
    animation.Freeze();
    BgProgressBar.BeginAnimation(FrameworkElement.WidthProperty, animation);
}

private const int BgRotationSeconds = 5; // 每 5 秒轮换一次


// ── 主框登录 ────────────────────────────────────────────
private async void LoginButton_Click(object sender, RoutedEventArgs e)
{
    await LoginPythonAsync(AccountTextBox.Text?.Trim() ?? "", PasswordBox.Password ?? "");
    return;
}

private void Window_Loaded(object sender,RoutedEventArgs e) {
 if(GetTemplateChild("PART_BorderTop") is FrameworkElement borderTop)borderTop.IsHitTestVisible=false;
 ApplyPythonUi();LoginVersionText.Text="90US";
 _=CheckOnlineAsync();StartOnlineTimer();
}

private async Task CheckOnlineAsync()
{
    await CheckPythonOnlineAsync();
    return;
}

/// <summary>每 10 秒轮询服务器状态，刷新在线/离线与人数；切页另有即时刷新兜底</summary>
private void StartOnlineTimer()
{
    _onlineTimer = new DispatcherTimer
    {
        Interval = TimeSpan.FromSeconds(10)
    };
    _onlineTimer.Tick += async (_, _) => await CheckOnlineAsync();
    _onlineTimer.Start();
}

private DispatcherTimer? _onlineTimer;


private void MinimizeButton_Click(object sender, RoutedEventArgs e)
{
    WindowState = WindowState.Minimized;
}

protected override void OnContentRendered(EventArgs e)
{
    base.OnContentRendered(e);
    App.LogStartup("LoginWindow: OnContentRendered 触发（_hasShown=" + _hasShown + "）");
    if (_hasShown)
    {
        return;
    }

    _hasShown = true;
    FadeInWindow();
    App.LogStartup("LoginWindow: FadeIn 淡入已启动");
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

private bool _hasShown;

// ── 界面辅助 ────────────────────────────────────────────
private void RootContainer_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
{
    if (e.ButtonState == MouseButtonState.Pressed)
    {
        DragMove();
    }
}

private void LoginInput_KeyDown(object sender, KeyEventArgs e)
{
    if (e.Key != Key.Enter)
    {
        return;
    }

    e.Handled = true;
    if (ReferenceEquals(sender, AccountTextBox) || ReferenceEquals(sender, PasswordBox))
    {
        if (!_isLoggingIn && !_mainLoggedIn)
        {
            LoginButton_Click(sender, e);
        }
    }
}


private bool _mainLoggedIn;

private void OnGameExited(int slot) {
 RemovePythonSession(slot);
 if(slot==GameProcessManager.MainSlot){_mainLoggedIn=false;MainLoggedInText.Visibility=Visibility.Collapsed;}
 RefreshLoginButtons();
}

private void RefreshLoginButtons()
{
    RefreshLoginButtonsCore();
    if (pythonUpdating || !pythonInitialCheckDone)
    {
        LoginButton.IsEnabled = false;
        if (!_mainLoggedIn)
            LoginButton.Content = pythonUpdating ? "正在更新…" : "正在检查更新…";
    }

    return;
}

private void RefreshLoginButtonsCore()
{
    LoginButton.IsEnabled = !_isLoggingIn && !_mainLoggedIn;
    LoginButton.Content = _mainLoggedIn ? "游戏运行中" : (_isOnline ? "登 录" : "重试连接");
}

private bool _isOnline;


/// <summary>
/// 提示：迁移到右上侧气泡 Toast（保留原签名，title 并入正文展示）
/// </summary>
private void ShowStatus(string message, System.Windows.Media.Brush brush, string? title = null)
{
    var text = string.IsNullOrWhiteSpace(title) || string.Equals(title, "提示", System.StringComparison.Ordinal) ? message : title + "：" + message;
    ShowToast(text, brush);
}

// ── 右上侧 Toast 通知系统 ─────────────────────────────────
/// <summary>
/// 右上侧气泡 Toast：右滑入 + 淡入（约 250ms），3 秒后自动滑出收回；
/// 多条依次排列在 ToastContainer 中，各自独立计时。
/// </summary>
private void ShowToast(string message, System.Windows.Media.Brush? accent = null)
{
    var accentBrush = accent ?? new SolidColorBrush(Color.FromRgb(0xE8, 0x4A, 0x3A));
    var border = new Border
    {
        Background = new SolidColorBrush(Color.FromArgb(0xF2, 0x18, 0x18, 0x1C)),
        CornerRadius = new CornerRadius(10),
        Margin = new Thickness(0, 0, 0, 8)
    };
    var grid = new Grid();
    grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(3) });
    grid.ColumnDefinitions.Add(new ColumnDefinition());
    var bar = new Border
    {
        Background = accentBrush,
        CornerRadius = new CornerRadius(1.5)
    };
    Grid.SetColumn(bar, 0);
    var text = new TextBlock
    {
        Text = message,
        Foreground = Brushes.White,
        FontSize = 13,
        TextWrapping = TextWrapping.Wrap,
        VerticalAlignment = VerticalAlignment.Center,
        Margin = new Thickness(12, 10, 14, 10)
    };
    Grid.SetColumn(text, 1);
    grid.Children.Add(bar);
    grid.Children.Add(text);
    border.Child = grid;
    var translate = new TranslateTransform
    {
        X = 32
    };
    border.RenderTransform = translate;
    border.Opacity = 0;
    ToastContainer.Children.Add(border);
    // 进入动画：从右滑入 + 淡入（250ms）
    var slideIn = new DoubleAnimation(32, 0, TimeSpan.FromMilliseconds(250))
    {
        EasingFunction = new CubicEase
        {
            EasingMode = EasingMode.EaseOut
        }
    };
    var fadeIn = new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(250));
    translate.BeginAnimation(TranslateTransform.XProperty, slideIn);
    border.BeginAnimation(OpacityProperty, fadeIn);
    // 3 秒后自动收回
    var timer = new DispatcherTimer
    {
        Interval = TimeSpan.FromSeconds(3)
    };
    timer.Tick += (_, _) =>
    {
        timer.Stop();
        var slideOut = new DoubleAnimation(0, 32, TimeSpan.FromMilliseconds(250))
        {
            EasingFunction = new CubicEase
            {
                EasingMode = EasingMode.EaseIn
            }
        };
        var fadeOut = new DoubleAnimation(1, 0, TimeSpan.FromMilliseconds(250));
        fadeOut.Completed += (_, _) => ToastContainer.Children.Remove(border);
        translate.BeginAnimation(TranslateTransform.XProperty, slideOut);
        border.BeginAnimation(OpacityProperty, fadeOut);
    };
    timer.Start();
}

/// <summary>重新加载背景：停止旧轮换与进度条动画，按当前配置重建（设置变更后立即生效）</summary>
public void ReloadBackground()
{
    _bgTimer?.Stop();
    _bgTimer = null;
    BgProgressBar.BeginAnimation(FrameworkElement.WidthProperty, null);
    SetupBackground();
}

private bool _forceClose;

public LoginWindow() {
 InitializeComponent();
 Title=Services.LauncherPackage.Current?.DisplayName??"90US 登陆器";
 SetupBackground();
 GameProcessManager.GameExited+=OnGameExited;
 GameProcessManager.AllGamesExited+=OnAllGamesExited;
 NavGame.IsChecked=true;
}

/// <summary>全部游戏退出 → 托盘自动弹出登录器界面（exitGamePopup 开关控制）</summary>
private void OnAllGamesExited()
{
    if (!AppConfig.Settings.ExitGamePopup || _forceClose)
    {
        return;
    }

    if (!IsVisible)
    {
        RestoreFromTray();
    }
}


protected override void OnClosing(CancelEventArgs e) {
 if(pythonUpdating&&!pythonRestarting){e.Cancel=true;ShowStatus("客户端正在更新，请等待更新完成。",Brushes.OrangeRed);return;}
 if(!pythonRestarting&&!_forceClose&&(GameProcessManager.AnyRunning||_isLoggingIn)){e.Cancel=true;HideToTray();return;}
 if(!_forceClose&&AppConfig.CloseAction==CloseAction.MinimizeToTray){e.Cancel=true;HideToTray();return;}
 _bgTimer?.Stop();_onlineTimer?.Stop();pythonNotifications?.Cancel();
 SettingsPage.SaveState();
 GameProcessManager.GameExited-=OnGameExited;GameProcessManager.AllGamesExited-=OnAllGamesExited;
 GameProcessManager.KillAll();
 foreach(var slot in pythonSessions.Keys.ToArray())RemovePythonSession(slot);
 DisposeTrayIcon();base.OnClosing(e);
}



// ── 托盘 ────────────────────────────────────────────────
/// <summary>隐藏窗口并显示系统托盘图标（托盘图标悬浮文字不做）</summary>
private void HideToTray()
{
    if (_trayIcon is null)
    {
        _trayIcon = new System.Windows.Forms.NotifyIcon
        {
            Visible = true
        };
        _trayIcon.DoubleClick += (_, _) => RestoreFromTray();
        // 右键菜单：打开主界面 / 退出
        var menu = new System.Windows.Forms.ContextMenuStrip();
        menu.Items.Add("打开主界面", null, (_, _) => BringToFront());
        menu.Items.Add(new System.Windows.Forms.ToolStripSeparator());
        menu.Items.Add("退出", null, (_, _) => ExitFromTray());
        _trayIcon.ContextMenuStrip = menu;
        // 从 exe 自身提取关联图标作为托盘图标
        try
        {
            var exePath = Environment.ProcessPath;
            if (!string.IsNullOrEmpty(exePath))
            {
                _trayIcon.Icon = System.Drawing.Icon.ExtractAssociatedIcon(exePath);
            }
        }
        catch
        {
        // 提取失败时保持默认空图标，不影响托盘功能
        }
    }
    else
    {
        _trayIcon.Visible = true;
    }

    Hide();
}

/// <summary>从托盘右键菜单真正退出登录器（OnClosing 中会结束所有 DNF 进程）</summary>
private void ExitFromTray()
{
    _forceClose = true;
    DisposeTrayIcon();
    Close();
}

/// <summary>清理托盘图标</summary>
private void DisposeTrayIcon()
{
    if (_trayIcon is null)
    {
        return;
    }

    _trayIcon.Visible = false;
    _trayIcon.Dispose();
    _trayIcon = null;
}

private void RegisterButton_Click(object sender, RoutedEventArgs e)
{
    var pythonRegister = new PythonRegisterWindow
    {
        Owner = this
    };
    if (pythonRegister.ShowDialog() == true)
    {
        AccountTextBox.Text = pythonRegister.RegisteredAccount;
        PasswordBox.Password = "";
    }

    return;
}

// ── 忘记密码（90 版本：账号 + 救援代码找回，无需原密码） ──
private void ForgotPasswordButton_Click(object sender, RoutedEventArgs e)
{
    var recovery = new PythonRecoveryWindow
    {
        Owner = this
    };
    if (recovery.ShowDialog() == true)
    {
        AccountTextBox.Text = recovery.RecoveredAccount;
        PasswordBox.Password = "";
    }

    return;
}

private IReadOnlyList<Services.PlayerNotice>? _serverNotices;

private void ApplyNoticesToPanel(IReadOnlyList<Services.PlayerNotice> notices)
{
    if (_serverNotices is not null && _serverNotices.SequenceEqual(notices))
        return;
    _serverNotices = notices;
    AllNoticesButton.IsEnabled = notices.Count > 0;
    RefreshNoticeList();
}

private void NoticeTab_Click(object sender, RoutedEventArgs e) => RefreshNoticeList();

private void RefreshNoticeList()
{
    if (_serverNotices is null)
        return;
    Services.NoticeCategory? category = NoticeUpdates.IsChecked == true ? Services.NoticeCategory.Update
        : NoticeGuides.IsChecked == true ? Services.NoticeCategory.Guide : null;
    ServerNoticeList.Children.Clear();
    foreach (var notice in _serverNotices)
    {
        if (category is null || notice.Category == category)
            ServerNoticeList.Children.Add(BuildNoticeRow(notice));
    }
    ServerNoticeStatus.Text = category switch
    {
        Services.NoticeCategory.Update => "暂无更新公告",
        Services.NoticeCategory.Guide => "暂无指南",
        _ => "暂无公告"
    };
    ServerNoticeStatus.Visibility = ServerNoticeList.Children.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
}

private Button BuildNoticeRow(Services.PlayerNotice notice)
{
    var category = notice.Category switch
    {
        Services.NoticeCategory.Update => "[更新]",
        Services.NoticeCategory.Guide => "[指南]",
        _ => "[公告]"
    };
    var row = new Grid();
    row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
    row.ColumnDefinitions.Add(new ColumnDefinition());
    row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
    row.Children.Add(new TextBlock
    {
        Text = category, Foreground = new SolidColorBrush(Color.FromRgb(0xFF, 0xB7, 0xAA)),
        FontSize = 11, Margin = new Thickness(0, 0, 9, 0), VerticalAlignment = VerticalAlignment.Center
    });
    var titleText = new TextBlock
    {
        Text = notice.Title, FontSize = 13, TextTrimming = TextTrimming.CharacterEllipsis,
        VerticalAlignment = VerticalAlignment.Center
    };
    Grid.SetColumn(titleText, 1);
    row.Children.Add(titleText);
    var date = new TextBlock
    {
        Text = FormatNoticeDate(notice.NoticeDate), FontSize = 11, MaxWidth = 80,
        Foreground = new SolidColorBrush(Color.FromRgb(0xB0, 0xBB, 0xCC)),
        Margin = new Thickness(10, 0, 0, 0), TextTrimming = TextTrimming.CharacterEllipsis,
        VerticalAlignment = VerticalAlignment.Center
    };
    Grid.SetColumn(date, 2);
    row.Children.Add(date);

    var button = new Button
    {
        Content = row, Height = 32, ToolTip = notice.Title,
        Style = (Style)FindResource("NoticeButtonStyle")
    };
    System.Windows.Automation.AutomationProperties.SetName(button, $"{category} {notice.Title} {date.Text}");
    button.Click += (_, _) =>
    {
        var body = new StackPanel { Margin = new Thickness(22, 18, 22, 22) };
        if (!string.IsNullOrWhiteSpace(notice.NoticeDate))
            body.Children.Add(new TextBlock { Text = notice.NoticeDate, Foreground = Brushes.LightGray, Margin = new Thickness(0, 0, 0, 8) });
        body.Children.Add(new TextBlock
        {
            Text = string.IsNullOrWhiteSpace(notice.Body) ? "暂无正文" : notice.Body,
            Foreground = Brushes.White, FontSize = 14, TextWrapping = TextWrapping.Wrap, LineHeight = 22
        });
        ShowNoticeWindow(Window.GetWindow(button) ?? this, notice.Title, body);
    };
    return button;
}

private void AllNotices_Click(object sender, RoutedEventArgs e)
{
    if (_serverNotices is null || _serverNotices.Count == 0)
        return;
    var list = new StackPanel { Margin = new Thickness(14, 10, 14, 14) };
    foreach (var notice in _serverNotices)
        list.Children.Add(BuildNoticeRow(notice));
    ShowNoticeWindow(this, "全部公告", list);
}

private static void ShowNoticeWindow(Window owner, string title, FrameworkElement content)
{
    var window = new Window
    {
        Owner = owner, Title = title, Width = 570, Height = 390, MinWidth = 340, MinHeight = 220,
        WindowStartupLocation = WindowStartupLocation.CenterOwner, ShowInTaskbar = false,
        Background = new SolidColorBrush(Color.FromRgb(0x19, 0x1D, 0x27)),
        Content = new ScrollViewer { Content = content, VerticalScrollBarVisibility = ScrollBarVisibility.Auto }
    };
    window.InputBindings.Add(new KeyBinding(SystemCommands.CloseWindowCommand, new KeyGesture(Key.Escape)));
    window.CommandBindings.Add(new CommandBinding(SystemCommands.CloseWindowCommand, (_, _) => window.Close()));
    window.ShowDialog();
}

private static string FormatNoticeDate(string date)
{
    var raw = date.Trim();
    var culture = System.Globalization.CultureInfo.InvariantCulture;
    return DateTime.TryParseExact(raw, "yyyy-MM-dd", culture, System.Globalization.DateTimeStyles.None, out var parsed)
        ? parsed.ToString("MM/dd", culture) : raw;
}

/// <summary>清除：不弹窗，直接清主/副两组输入框 + 凭据管理器中所有 DNFLogin_* 条目 + JSON 账号字段</summary>
private void ClearButton_Click(object sender, RoutedEventArgs e)
{
    AccountTextBox.Text = string.Empty;
    PasswordBox.Password = string.Empty;
    ClearPythonLogin();
    RememberCheckBox.IsChecked = false;
    AppConfig.Settings.SetLastAccount(AppConfig.CurrentProfile.Id, string.Empty);
    AppConfig.Save();
}

protected override void OnSourceInitialized(EventArgs e)
{
    base.OnSourceInitialized(e);
    _hwndSource = (HwndSource? )PresentationSource.FromVisual(this);
    _hwndSource?.AddHook(WndProc);
}

// ── 字段 ────────────────────────────────────────────────
private HwndSource? _hwndSource;

/// <summary>
/// AduWindow 在 Win32 层把顶部区域判为标题栏（HT_CAPTION），拦截发生在 WPF 命中测试之前，
/// 导致顶部自定义按钮"不跟手"。这里在按钮区域强制返回 HTCLIENT，把点击交还给 WPF 层。
/// </summary>
private IntPtr WndProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
{
    // 顶部按钮命中测试
    if (msg != WM_NCHITTEST || _hwndSource is null)
    {
        return IntPtr.Zero;
    }

    int screenX = (short)(lParam.ToInt32() & 0xFFFF);
    int screenY = (short)((lParam.ToInt32() >> 16) & 0xFFFF);
    var clientPoint = PointFromScreen(new System.Windows.Point(screenX, screenY));
    foreach (var btn in new System.Windows.Controls.Button[]
    {
        MinimizeButton,
        CloseButton
    }

    )
    {
        if (btn is null || !btn.IsVisible)
        {
            continue;
        }

        var origin = btn.TranslatePoint(new System.Windows.Point(0, 0), this);
        var rect = new System.Windows.Rect(origin.X, origin.Y, btn.ActualWidth, btn.ActualHeight);
        rect.Inflate(3, 3);
        if (rect.Contains(clientPoint))
        {
            handled = true;
            return new IntPtr(HTCLIENT);
        }
    }

    return IntPtr.Zero;
}

// ── Win32 常量（仅 WM_NCHITTEST） ──────────────────────
private const int WM_NCHITTEST = 0x0084;

private const int HTCLIENT = 1;

private void Nav_Checked(object sender,RoutedEventArgs e) {
 if(sender is not RadioButton rb || PageLogin is null || SettingsPage is null)return;
 bool game=ReferenceEquals(rb,NavGame);
 PageLogin.Visibility=game?Visibility.Visible:Visibility.Collapsed;
 SettingsPage.Visibility=ReferenceEquals(rb,NavSettings)?Visibility.Visible:Visibility.Collapsed;
 CdkHost.Visibility=ReferenceEquals(rb,NavCdk)?Visibility.Visible:Visibility.Collapsed;
 if(_pythonCdkPage is not null){_pythonCdkPage.Visibility=ReferenceEquals(rb,NavCdk)?Visibility.Visible:Visibility.Collapsed;if(ReferenceEquals(rb,NavCdk))_pythonCdkPage.RefreshAccounts();}
 GlassOverlay.Visibility=game?Visibility.Collapsed:Visibility.Visible;
 if(game&&_bgImages.Count>1)ShowBgProgress();else HideBgProgress();
}

private void CloseButton_Click(object sender, RoutedEventArgs e)
{
    if (!pythonRestarting && (GameProcessManager.AnyRunning || _isLoggingIn))
    {
        // Do not let an older remembered Exit choice tear down the relay.
        _forceClose = false;
        Close();
        return;
    }

    // 已记住关闭行为（CloseActionAsked=1）：直接按行为执行（OnClosing 按 CloseAction 拦截到托盘或退出）
    if (AppConfig.Settings.CloseActionAsked == 1)
    {
        Close();
        return;
    }

    // 未询问过（0/缺失）：弹窗询问 关闭登录器 / 最小化到托盘，可勾选记住并写入 JSON
    ShowCloseActionDialog();
}

/// <summary>
/// 首次点击右上角 X 的询问弹窗：选择关闭行为，可勾选"记住此选项，以后不再询问"。
/// 弹窗仅收集选择，勾选记住时同步写入 JSON（CloseActionAsked=1 后不再询问，选择存于 CloseAction）。
/// </summary>
private void ShowCloseActionDialog()
{
    var dlg = new CloseActionDialog
    {
        Owner = this
    };
    if (dlg.ShowDialog() != true)
    {
        return;
    }

    if (dlg.Remember)
    {
        AppConfig.Settings.CloseAction = dlg.SelectedAction;
        AppConfig.Settings.CloseActionAsked = 1;
        AppConfig.Save();
    }

    if (dlg.SelectedAction == CloseAction.Exit.ToString())
    {
        // 真正退出：结束托管 DNF 进程等由 OnClosing 处理
        _forceClose = true;
        Close();
    }
    else
    {
        HideToTray();
    }
}
}}
