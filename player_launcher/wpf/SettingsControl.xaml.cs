using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using DNFLogin.Services;

namespace DNFLogin {public partial class SettingsControl : UserControl {
public SettingsControl(){InitializeComponent();AboutVersionText.Text=LauncherVersion.DisplayText;
 NavBase.Checked+=SubNav_Checked;NavAbout.Checked+=SubNav_Checked;

 ToggleExitPopup.IsChecked=AppConfig.Settings.ExitGamePopup;ToggleExitPopup.Checked+=ToggleExitPopup_Changed;ToggleExitPopup.Unchecked+=ToggleExitPopup_Changed;
 if(AppConfig.CloseAction==CloseAction.MinimizeToTray)RadioTray.IsChecked=true;else RadioExit.IsChecked=true; }
public void SaveState(){AppConfig.Save();}
private void SubNav_Checked(object sender,RoutedEventArgs e){if(sender is not RadioButton rb)return;PageSettings.Visibility=ReferenceEquals(rb,NavBase)?Visibility.Visible:Visibility.Collapsed;PageAbout.Visibility=ReferenceEquals(rb,NavAbout)?Visibility.Visible:Visibility.Collapsed;}

/// <summary>
/// 「关于」页 Discord 邀请链接：用系统默认浏览器打开。
/// 为什么必须自己处理 RequestNavigate：WPF 的 Hyperlink 默认把跳转交给宿主导航容器
/// （NavigationWindow / Frame），本项目是普通 Window，没有 NavigationService，
/// 不接管点击的话点下去毫无反应。这里显式 UseShellExecute 交给 Shell 打开，
/// 无论玩家装的默认浏览器是什么都能正常拉起；失败时把链接显示出来让玩家手动访问。
/// </summary>
private void AboutDcLink_RequestNavigate(object sender, System.Windows.Navigation.RequestNavigateEventArgs e)
{
    try
    {
        System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo { FileName = e.Uri.AbsoluteUri, UseShellExecute = true });
    }
    catch (Exception ex)
    {
        MessageBox.Show("无法自动打开 Discord 链接，请手动在浏览器中访问：\n" + e.Uri.AbsoluteUri + "\n\n原因：" + ex.Message, "提示", MessageBoxButton.OK, MessageBoxImage.Warning);
    }

    // 标记已处理：阻止 WPF 再去走它自己那套导航（会抛"找不到导航容器"之类的异常）
    e.Handled = true;
}
// ── 基础设置 ─────────────────────────────────────
private void ToggleExitPopup_Changed(object sender, RoutedEventArgs e)
{
    AppConfig.Settings.ExitGamePopup = ToggleExitPopup.IsChecked == true;
    AppConfig.Save();
}
/// <summary>关闭窗口行为：最小化到托盘 / 退出启动器</summary>
private void CloseBehavior_Checked(object sender, RoutedEventArgs e)
{
    AppConfig.Settings.CloseAction = RadioTray.IsChecked == true ? nameof(CloseAction.MinimizeToTray) : nameof(CloseAction.Exit);
    AppConfig.Save();
}
/// <summary>清除所有配置（危险操作）：删除配置文件后退出启动器</summary>
private void ClearAllConfigButton_Click(object sender, RoutedEventArgs e)
{
    var r = MessageBox.Show("确定要清除所有配置吗？\n\n将删除账号、密码、服务器配置、DLL 列表等全部本地数据，且不可恢复！", "清除所有配置", MessageBoxButton.YesNo, MessageBoxImage.Warning);
    if (r != MessageBoxResult.Yes)
    {
        return;
    }

    try
    {
#if PYTHON90
        PythonRememberedLogin.Current().Clear();
#endif
        if (File.Exists(AppConfig.ConfigPath))
        {
            File.Delete(AppConfig.ConfigPath);
        }
    }
    catch (Exception)
    {
        MessageBox.Show("清除配置失败，请先关闭正在使用登录器配置的进程后重试。", "清除所有配置", MessageBoxButton.OK, MessageBoxImage.Error);
        return;
    }

    // 阻止退出流程（关窗兜底 SaveState 等）把旧配置重新写回磁盘
    AppConfig.ConfigCleared = true;
    // 清除成功后自动重启登录器（以全新配置状态启动）
    try
    {
        // 先释放单实例互斥，否则新实例会因"已有实例"而立即退出
        ((App)Application.Current).PrepareForRestart();
        System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo { FileName = Environment.ProcessPath, UseShellExecute = true });
    }
    catch (Exception)
    {
        MessageBox.Show("清除配置成功，但自动重启失败，请手动重新打开登录器。", "清除所有配置", MessageBoxButton.OK, MessageBoxImage.Warning);
    }

    Application.Current.Shutdown();
}
}}
