#if PYTHON90
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using DNFLogin.Services;
namespace DNFLogin;
public sealed class PythonCdkAccount(int slot,string username,string accessToken)
{
    public int Slot { get; } = slot;
    public string Username { get; } = username;
    public string AccessToken { get; } = accessToken;
    public string Label => Username;
    public override string ToString() => Label;
}
public sealed class PythonCdkRedeemControl : UserControl
{
    private readonly Func<IReadOnlyList<PythonCdkAccount>> getAccounts;
    private readonly Func<PythonLauncherApi> createApi;
    private readonly ComboBox accounts = new() { DisplayMemberPath="Label" }, characters = new() { DisplayMemberPath="Label" };
    private readonly TextBox code = new();
    private readonly TextBlock status = new() { Foreground=Brushes.White,TextWrapping=TextWrapping.Wrap,Margin=new Thickness(0,12,0,0) };
    private readonly TextBox history = new() { IsReadOnly=true,TextWrapping=TextWrapping.Wrap,VerticalScrollBarVisibility=ScrollBarVisibility.Auto,Height=120,Margin=new Thickness(0,12,0,0),Background=new SolidColorBrush(Color.FromRgb(20,21,25)),Foreground=Brushes.White };
    private readonly Button redeem = new() { Content="兑换 CDK",Height=36,IsEnabled=false,Margin=new Thickness(0,14,0,0) };
    private readonly Button refresh = new() { Content="刷新角色和兑换记录",Height=32,IsEnabled=false,Margin=new Thickness(0,10,0,0) };
    private int generation;
    private bool busy,updating;
    private sealed record Character(long Id,string Label);
    public PythonCdkRedeemControl(Func<IReadOnlyList<PythonCdkAccount>> getAccounts,Func<PythonLauncherApi>? apiFactory=null)
    {
        this.getAccounts=getAccounts;
        createApi=apiFactory??PythonClientLauncher.CreateApi;
        Resources.MergedDictionaries.Add(new ResourceDictionary { Source=new Uri("CommonStyles.xaml",UriKind.Relative) });
        accounts.Style=characters.Style=(Style)FindResource("DbComboStyle");
        redeem.Style=(Style)FindResource("ServerPrimaryBtn");refresh.Style=(Style)FindResource("ServerSecondaryBtn");
        var panel=new StackPanel { Width=560,Margin=new Thickness(24),HorizontalAlignment=HorizontalAlignment.Center };
        Content=new ScrollViewer { VerticalScrollBarVisibility=ScrollBarVisibility.Auto,Content=panel };
        panel.Children.Add(new TextBlock { Text="CDK 兑换",FontSize=24,Foreground=Brushes.White });
        panel.Children.Add(new TextBlock { Text="选择角色后兑换，物品在游戏内邮箱领取，点券直接到账。",Foreground=Brushes.LightGray,Margin=new Thickness(0,8,0,0) });
        PythonRegisterWindow.AddField(panel,"当前登录账号",accounts);
        PythonRegisterWindow.AddField(panel,"领取角色",characters);
        PythonRegisterWindow.AddField(panel,"兑换码",code);
        panel.Children.Add(redeem);panel.Children.Add(status);panel.Children.Add(history);panel.Children.Add(refresh);
        accounts.SelectionChanged+=async (_,_)=> { if(!updating)await LoadAccount(); };
        redeem.Click+=Redeem;refresh.Click+=async (_,_)=> { if(!busy)await LoadAccount(); };
        RefreshAccounts();
    }
    public void RefreshAccounts()
    {
        var previous=(accounts.SelectedItem as PythonCdkAccount)?.AccessToken;
        var choices=getAccounts();
        generation++;
        updating=true;accounts.ItemsSource=choices;accounts.SelectedIndex=choices.Count==0 ? -1 : Math.Max(0,choices.ToList().FindIndex(row=>row.AccessToken==previous));updating=false;
        if(choices.Count==0) { characters.Items.Clear();history.Clear();SetBusy(false);status.Text="请先进行账号登录，再进行兑换。";return; }
        _=LoadAccount();
    }
    private void SetBusy(bool value) { busy=value;accounts.IsEnabled=!value;var logged=accounts.SelectedItem is PythonCdkAccount;characters.IsEnabled=code.IsEnabled=!value&&logged;redeem.IsEnabled=!value&&logged&&characters.Items.Count>0;refresh.IsEnabled=!value&&logged; }
    private async Task LoadAccount()
    {
        if(accounts.SelectedItem is not PythonCdkAccount selected)return;
        var current=++generation;SetBusy(true);characters.Items.Clear();
        try
        {
            using var api=createApi();
            var doc=await api.CdkCharactersAsync(selected.AccessToken);
            if(current!=generation)return;
            foreach(var item in doc.GetProperty("characters").EnumerateArray())characters.Items.Add(new Character(item.GetProperty("id").GetInt64(),item.GetProperty("name").GetString()+" · Lv."+item.GetProperty("level").GetInt32()));
            if(characters.Items.Count>0)characters.SelectedIndex=0;
            status.Text=characters.Items.Count>0 ? "请选择角色并填写兑换码。" : "当前账号没有角色，请先在游戏中创建角色。";
            var records=await api.CdkHistoryAsync(selected.AccessToken);if(current==generation)ShowHistory(records);
        }
        catch(InvalidOperationException ex) { if(current==generation)status.Text=ex.Message; }
        catch { if(current==generation)status.Text="读取角色未完成，请稍后刷新。"; }
        finally { if(current==generation)SetBusy(false); }
    }
    private void ShowHistory(System.Text.Json.JsonElement doc)
    {
        history.Text=string.Join(Environment.NewLine,doc.GetProperty("redemptions").EnumerateArray().Select(row=>row.GetProperty("created_at").GetString()+"  "+row.GetProperty("name").GetString()+"  "+row.GetProperty("code").GetString()));
        if(history.Text.Length==0)history.Text="暂无兑换记录。";
    }
    private async void Redeem(object sender,RoutedEventArgs e)
    {
        if(busy)return;
        if(accounts.SelectedItem is not PythonCdkAccount selected) { status.Text="请先进行账号登录，再进行兑换。";return; }
        if(characters.SelectedItem is not Character character||string.IsNullOrWhiteSpace(code.Text)){status.Text="请选择角色并填写兑换码。";return;}
        var current=generation;SetBusy(true);
        try
        {
            using var api=createApi();var result=await api.RedeemAsync(selected.AccessToken,code.Text,character.Id,Guid.NewGuid().ToString("N"));
            if(current!=generation)return;status.Text=result.GetProperty("message").GetString();code.Clear();
            var records=await api.CdkHistoryAsync(selected.AccessToken);if(current==generation)ShowHistory(records);
        }
        catch(InvalidOperationException ex){if(current==generation)status.Text=ex.Message+" 如连接中断，请先刷新兑换记录核对结果。";}
        catch { if(current==generation)status.Text="兑换结果未确认，请刷新记录核对后再操作。"; }
        finally { if(current==generation)SetBusy(false); }
    }
}
#endif
