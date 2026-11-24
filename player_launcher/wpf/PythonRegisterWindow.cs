#if PYTHON90
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using DNFLogin.Services;

namespace DNFLogin;

public sealed class PythonRegisterWindow : Window
{
    private readonly TextBox account = new(), code = new();
    private readonly PasswordBox password = new(), confirm = new();
    private readonly ComboBox question = new() { ItemsSource = PythonLauncherApi.SecurityQuestions, DisplayMemberPath = "Value", SelectedValuePath = "Key" };
    private readonly PasswordBox answer = new();
    private readonly TextBlock status = new() { TextWrapping = TextWrapping.Wrap, Foreground = Brushes.OrangeRed, Margin = new Thickness(0, 12, 0, 0) };
    private readonly Button submit = new() { Content = "注册账号", Height = 36, Margin = new Thickness(0, 16, 0, 0) };
    private readonly StackPanel codePanel = new() { Visibility = Visibility.Collapsed };
    private bool submitting;
    public string RegisteredAccount { get; private set; } = "";
    public PythonRegisterWindow()
    {
        Title = "注册账号"; Width = 440; SizeToContent = SizeToContent.Height; MaxHeight = SystemParameters.WorkArea.Height - 32; ResizeMode = ResizeMode.NoResize;
        WindowStartupLocation = WindowStartupLocation.CenterOwner; Background = new SolidColorBrush(Color.FromRgb(34, 35, 41));
        var panel = new StackPanel { Margin = new Thickness(28) }; Content = new ScrollViewer { Content = panel, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        Resources.MergedDictionaries.Add(new ResourceDictionary { Source = new Uri("CommonStyles.xaml",UriKind.Relative) });
        submit.Style=(Style)FindResource("ServerPrimaryBtn");
        question.Style=(Style)FindResource("DbComboStyle");
        panel.Children.Add(new TextBlock { Text = "注册账号", FontSize = 22, Foreground = Brushes.White, Margin = new Thickness(0, 0, 0, 16) });
        AddField(panel, "账号", account); AddField(panel, "密码（8–128 位）", password); AddField(panel, "确认密码", confirm);
        AddField(panel, "密保问题", question); AddField(panel, "密保答案（2–100 个字符，请牢记）", answer);
        AddField(codePanel, "注册码", code); panel.Children.Add(codePanel); panel.Children.Add(status);
        panel.Children.Add(submit);
        submit.Click += Submit;
        var refresh = new System.Windows.Threading.DispatcherTimer { Interval = TimeSpan.FromSeconds(10) };
        bool checking = false;
        async Task CheckRegistration()
        {
            if (checking || RegisteredAccount.Length != 0 || submitting) return;
            checking = true;
            try {
                using var api = PythonClientLauncher.CreateApi();
                var policy = await api.RegistrationPolicyAsync();
                if (!IsLoaded || submitting || RegisteredAccount.Length != 0) return;
                codePanel.Visibility = policy.GetProperty("code_required").GetBoolean() ? Visibility.Visible : Visibility.Collapsed;
                bool closed = policy.GetProperty("closed").GetBoolean();
                submit.IsEnabled = !closed;
                if (closed) status.Text = "服务器已关闭注册。";
                else if (status.Text == "服务器已关闭注册。") status.Text = "";
            } catch (InvalidOperationException ex) { if (IsLoaded && !submitting) status.Text = ex.Message; }
            finally { checking = false; }
        }
        refresh.Tick += async (_, _) => await CheckRegistration();
        Loaded += async (_, _) => { refresh.Start(); await CheckRegistration(); };
        Closed += (_, _) => refresh.Stop();
    }
    internal static void AddField(Panel panel, string label, Control control)
    {
        panel.Children.Add(new TextBlock { Text = label, Foreground = Brushes.White, Margin = new Thickness(0, 10, 0, 5) });
        control.Height = 32; control.FontSize = 14; control.Padding = new Thickness(6, 3, 6, 3); panel.Children.Add(control);
        if(control is TextBox or PasswordBox) { control.Background=new SolidColorBrush(Color.FromRgb(20,21,25));control.Foreground=Brushes.White;control.BorderBrush=new SolidColorBrush(Color.FromRgb(65,66,72)); }
    }
    private async void Submit(object sender, RoutedEventArgs e)
    {
        if (!submit.IsEnabled) return;
        if (RegisteredAccount.Length > 0) { DialogResult = true; return; }
        if (password.Password != confirm.Password) { status.Text = "两次输入的密码不一致。"; return; }
        submitting = true; submit.IsEnabled = false; status.Text = "正在注册…";
        try { using var api = PythonClientLauncher.CreateApi(); var result = await api.RegisterAsync(account.Text, password.Password, code.Text, question.SelectedValue as string ?? "", answer.Password); RegisteredAccount=result.Account;password.Clear();confirm.Clear();answer.Clear();account.IsReadOnly=true;password.IsEnabled=confirm.IsEnabled=code.IsEnabled=question.IsEnabled=answer.IsEnabled=false;status.Foreground=Brushes.LightGreen;status.Text="注册成功。请牢记账号和密保答案，以后仅通过密保问题重设密码。";submit.Content="返回登录"; }
        catch (InvalidOperationException ex) { status.Text = ex.Message; if (ex.Message.Contains("注册码")) codePanel.Visibility = Visibility.Visible; }
        catch { status.Text = "注册未完成，请检查网络后重试。"; }
        finally { submitting = false; submit.IsEnabled = true; }
    }
}
#endif
