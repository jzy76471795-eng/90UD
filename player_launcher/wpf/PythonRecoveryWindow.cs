#if PYTHON90
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using DNFLogin.Services;
namespace DNFLogin;
public sealed class PythonRecoveryWindow : Window
{
    private readonly PasswordBox password=new(),confirm=new(),answer=new();
    private readonly TextBox account=new();
    private readonly ComboBox question=new() { ItemsSource=PythonLauncherApi.SecurityQuestions,DisplayMemberPath="Value",SelectedValuePath="Key" };
    private readonly TextBlock status=new() { Foreground=Brushes.OrangeRed,TextWrapping=TextWrapping.Wrap,Margin=new Thickness(0,12,0,0) };
    private readonly Button submit=new() { Content="验证密保并重设密码",Height=36,Margin=new Thickness(0,16,0,0) };
    public string RecoveredAccount { get; private set; }="";
    public PythonRecoveryWindow()
    {
        Title="找回密码";Width=480;SizeToContent=SizeToContent.Height;MaxHeight=SystemParameters.WorkArea.Height-32;ResizeMode=ResizeMode.NoResize;WindowStartupLocation=WindowStartupLocation.CenterOwner;Background=new SolidColorBrush(Color.FromRgb(34,35,41));
        Resources.MergedDictionaries.Add(new ResourceDictionary { Source=new Uri("CommonStyles.xaml",UriKind.Relative) });submit.Style=(Style)FindResource("ServerPrimaryBtn");
        question.Style=(Style)FindResource("DbComboStyle");
        var panel=new StackPanel { Margin=new Thickness(28) };Content=new ScrollViewer { Content=panel,VerticalScrollBarVisibility=ScrollBarVisibility.Auto };
        panel.Children.Add(new TextBlock { Text="找回密码",FontSize=22,Foreground=Brushes.White });
        panel.Children.Add(new TextBlock { Text="请输入账号，并填写注册时设置的密保问题和答案。",Foreground=Brushes.LightGray,TextWrapping=TextWrapping.Wrap,Margin=new Thickness(0,10,0,0) });
        PythonRegisterWindow.AddField(panel,"注册账号",account);PythonRegisterWindow.AddField(panel,"密保问题",question);PythonRegisterWindow.AddField(panel,"密保答案",answer);
        PythonRegisterWindow.AddField(panel,"新密码（8–128 位）",password);PythonRegisterWindow.AddField(panel,"确认新密码",confirm);
        panel.Children.Add(status);panel.Children.Add(submit);submit.Click+=Submit;
    }
    private async void Submit(object sender,RoutedEventArgs e)
    {
        if(!submit.IsEnabled)return;
        if(RecoveredAccount.Length>0){DialogResult=true;return;}
        if(string.IsNullOrWhiteSpace(account.Text)||question.SelectedValue is not string||string.IsNullOrWhiteSpace(answer.Password)){status.Text="请完整填写账号、密保问题和答案。";return;}
        if(password.Password.Length is <8 or >128){status.Text="新密码需要 8–128 位。";return;}
        if(password.Password!=confirm.Password){status.Text="两次输入的新密码不一致。";return;}
        submit.IsEnabled=false;status.Text="正在验证密保…";
        try {
            using var api=PythonClientLauncher.CreateApi();
            var result=await api.RecoverBySecurityAsync(account.Text,(string)question.SelectedValue,answer.Password,password.Password);
            RecoveredAccount=result.Account;password.Clear();confirm.Clear();answer.Clear();
            password.IsEnabled=confirm.IsEnabled=account.IsEnabled=question.IsEnabled=answer.IsEnabled=false;
            try { PythonRememberedLogin.Current().Forget(RecoveredAccount); } catch { }
            status.Foreground=Brushes.LightGreen;status.Text="密码已重设，请使用新密码登录。";submit.Content="返回登录";
        }
        catch(InvalidOperationException ex){status.Text=ex.Message;}
        catch{status.Text="找回结果未确认，请使用新密码尝试登录核对。";}
        finally{submit.IsEnabled=true;}
    }
}
#endif
