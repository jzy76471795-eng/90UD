#if PYTHON90
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using DNFLogin.Services;

namespace DNFLogin;

public partial class LoginWindow
{
    private readonly Dictionary<int,(PythonLauncherApi Api,PythonSession Session,string Runtime,string Account)> pythonSessions=new();
    private bool pythonChecking;
    private bool pythonInitialCheckDone;
    private bool pythonUpdating;
    private bool pythonRestarting;
    private Border? pythonUpdateProgress;
    private TextBlock? pythonUpdateProgressText;
    private void ShowPythonUpdateProgress(string message)
    {
        if(!pythonUpdating)return;
        if(pythonUpdateProgressText is not null){pythonUpdateProgressText.Text=message;return;}
        pythonUpdateProgressText=new TextBlock{Text=message,Foreground=Brushes.White,FontSize=13,
            TextWrapping=TextWrapping.Wrap,VerticalAlignment=VerticalAlignment.Center,Margin=new Thickness(12,10,14,10)};
        var grid=new Grid();grid.ColumnDefinitions.Add(new ColumnDefinition{Width=new GridLength(3)});
        grid.ColumnDefinitions.Add(new ColumnDefinition());
        var bar=new Border{Background=Brushes.LightGray,CornerRadius=new CornerRadius(1.5)};
        Grid.SetColumn(pythonUpdateProgressText,1);grid.Children.Add(bar);grid.Children.Add(pythonUpdateProgressText);
        pythonUpdateProgress=new Border{Background=new SolidColorBrush(Color.FromArgb(0xF2,0x18,0x18,0x1C)),
            CornerRadius=new CornerRadius(10),Margin=new Thickness(0,0,0,8),Child=grid};
        ToastContainer.Children.Add(pythonUpdateProgress);
    }
    private void ClearPythonUpdateProgress()
    {
        if(pythonUpdateProgress is not null)ToastContainer.Children.Remove(pythonUpdateProgress);
        pythonUpdateProgress=null;pythonUpdateProgressText=null;
    }

    private readonly System.Threading.SemaphoreSlim pythonResourceGate=new(1,1);
    private string pythonLastUpdateAttempt="";
    private DateTime pythonNextUpdateAttempt;
    private string pythonLastReadyResourceStamp="";
    private PythonCdkRedeemControl? _pythonCdkPage;
    private void ApplyPythonUi()
    {
        Title=LauncherPackage.Current?.DisplayName??"90US Python HTTPS 登录器";



        NavCdk.Visibility=Visibility.Visible;

        RegisterButton.Visibility=Visibility.Visible;
        ForgotPasswordButton.Visibility=Visibility.Visible;
        ForgotPasswordButton.Content="找回密码";
        ForgotPasswordButton.ToolTip="通过注册时设置的密保问题和答案重设密码";
        ConnectionStatusPanel.Visibility=Visibility.Collapsed;
        RememberCheckBox.IsEnabled=true;
        RememberCheckBox.ToolTip="在本机加密保存账号密码；取消勾选会删除保存的信息。";
        RestorePythonLogin();
        ModeStatusText.Text="Python HTTPS";

        StartPythonNotifications();

        if (_pythonCdkPage is null)
        {
            _pythonCdkPage = new PythonCdkRedeemControl(() => pythonSessions.Select(pair => new PythonCdkAccount(pair.Key, pair.Value.Account, pair.Value.Session.AccessToken)).ToArray()) { Margin = CdkHost.Margin, Visibility = Visibility.Collapsed };
            CdkHost.Children.Add(_pythonCdkPage);
        }
    }
    private bool pythonRememberEventsAttached;
    private void RestorePythonLogin()
    {
        if(pythonRememberEventsAttached)return;
        try
        {
            var saved=PythonRememberedLogin.Current().Read();
            foreach(var row in saved)
                { AccountTextBox.Text=row.Account; PasswordBox.Password=row.Password; }
            RememberCheckBox.IsChecked=saved.Count>0;
        }
        catch { ShowStatus("保存的密码无法读取，请重新输入账号和密码。",Brushes.OrangeRed); }
        RememberCheckBox.Unchecked+=(_,_)=>ClearPythonLogin();
        pythonRememberEventsAttached=true;
    }
    private void ClearPythonLogin()
    {
        try{PythonRememberedLogin.Current().Clear();}
        catch { ShowStatus("清除已保存密码失败，请检查登录器数据目录权限。",Brushes.OrangeRed); }
    }
    private async Task CheckPythonOnlineAsync()
    {
        if(pythonChecking||pythonUpdating)return;
        pythonChecking=true;
        var startup=!pythonInitialCheckDone;
        RefreshLoginButtons();
        try
        {
            using var api=PythonClientLauncher.CreateApi();
            if(startup)ShowStatus("正在检查客户端更新…",Brushes.LightGray);
            var stage=await api.MetaAsync();_isOnline=true;
            OnlineStatusText.Text="Python 登录服务就绪";OnlineStatusText.Foreground=Brushes.LightGreen;
            if(!GameProcessManager.AnyRunning)
                try{await EnsurePythonResourcesAsync(api,stage,false);if(startup&&!pythonRestarting)ShowStatus("客户端检查完成，可以登录。",Brushes.LightGreen);}catch(InvalidOperationException error){ShowStatus(error.Message,Brushes.OrangeRed);}
        }
        catch(InvalidOperationException ex){_isOnline=false;OnlineStatusText.Text=ex.Message;OnlineStatusText.Foreground=Brushes.OrangeRed;if(startup)ShowStatus(OnlineStatusText.Text.TrimEnd('。','，')+"，可点击登录重试。",Brushes.OrangeRed);}
        catch{_isOnline=false;OnlineStatusText.Text="Python HTTPS 配置或证书不可用";OnlineStatusText.Foreground=Brushes.OrangeRed;if(startup)ShowStatus(OnlineStatusText.Text+"，可点击登录重试。",Brushes.OrangeRed);}
        finally{pythonChecking=false;pythonInitialCheckDone=true;if(!pythonUpdating)ModeStatusText.Text="Python HTTPS";RefreshLoginButtons();}
    }

    private async Task<bool> EnsurePythonResourcesAsync(PythonLauncherApi api,PythonStage3 stage,bool force)
    {
        // Manual logins share the client directory and must each finish their resource check.
        if (!await pythonResourceGate.WaitAsync(force ? Timeout.Infinite : 0)) return false;
        try
        {
            if (pythonRestarting || _forceClose) return true;
            return await EnsurePythonResourcesCoreAsync(api, stage, force);
        }
        finally { pythonResourceGate.Release(); }
    }
    private async Task<bool> EnsurePythonResourcesCoreAsync(PythonLauncherApi api,PythonStage3 stage,bool force)
    {
        if(pythonUpdating)throw new InvalidOperationException("客户端正在更新，请稍候。");
        var root=PythonLauncherPaths.LauncherDirectory;
        await Task.Run(()=>PythonUpdateTransaction.Recover(root,PythonResourceUpdater.CacheDirectory));
        var exe=System.IO.Path.Combine(root,"DFO.exe");var pvf=System.IO.Path.Combine(root,"Script.pvf");
        string Stamp(string path){var info=new System.IO.FileInfo(path);return info.Exists?$"{info.Length}:{info.LastWriteTimeUtc.Ticks}":"missing";}
        var resourceStamp=stage.PvfSha256+"|"+Stamp(pvf);
        // Background status polling need not rehash an unchanged, already checked PVF.
        // An explicit login always hashes the PVF again before authentication.
        if(!force&&pythonLastReadyResourceStamp==resourceStamp&&System.IO.File.Exists(exe))return false;
        var validExe=await Task.Run(()=>System.IO.File.Exists(exe));
        var localPvf=await Task.Run(()=>System.IO.File.Exists(pvf)?PythonClientLauncher.Sha256(pvf):"");
        bool needsRepair = false;
        if (validExe && localPvf == stage.PvfSha256)
        {
            // A manual login validates the full client immediately after this check.
            try { await Task.Run(() => PythonClientLauncher.ValidateComponents(root, stage.Patches)); }
            catch (InvalidOperationException) { needsRepair = true; }
            if (!needsRepair) { pythonLastReadyResourceStamp=resourceStamp; return false; }
        }
        pythonLastReadyResourceStamp="";
        var release=await api.PvfLatestAsync();
        if(release.FileSha256!=stage.PvfSha256)throw new InvalidOperationException("服务器发布版本刚发生变化，请稍后重新检查。");
        var signature=release.Version+"|"+release.DownloadUrl+"|"+release.ClientDownloadUrl+"|"+localPvf;
        if(!force && pythonLastUpdateAttempt==signature && DateTime.UtcNow < pythonNextUpdateAttempt)
            throw new InvalidOperationException("客户端等待重试，稍后会自动检查。");
        pythonNextUpdateAttempt=DateTime.UtcNow.AddSeconds(30);
        pythonLastUpdateAttempt=signature;
        var address=PythonResourceUpdater.DownloadAddress(release,validExe && (!needsRepair || string.IsNullOrWhiteSpace(release.ClientDownloadUrl)));
        if(GameProcessManager.AnyRunning||PythonResourceUpdater.ClientIsRunning(root))throw new InvalidOperationException("请先退出游戏，再更新客户端。");
        var original=Environment.GetEnvironmentVariable("PY90_LAUNCHER_EXE")??Environment.ProcessPath??"";
        // Validate restart location before changing any client files.
        var restart=PythonResourceUpdater.RestartPlan(original,root,Environment.ProcessId);
        pythonUpdating=true;RefreshLoginButtons();
        ShowStatus("客户端版本不匹配，正在自动下载更新；完成后会重新打开登录器。",Brushes.LightGray);
        try
        {
            var progress=new Progress<string>(ShowPythonUpdateProgress);
            using var downloadClient=api.DownloadClient(address);
            await PythonResourceUpdater.UpdateAsync(address,root,stage.PvfSha256,stage.ExeSha256,original,
                ()=>PythonClientLauncher.ValidateClient(exe,stage),progress,suppliedClient:downloadClient,
                trustedCertificatePem:api.UpdateCertificatePem,expectedVersion:release.Version,
                preservedRelativePaths:PythonClientLauncher.PairedComponentsForUpdate(root, allowRepair:true));
            using var helper=System.Diagnostics.Process.Start(restart)??throw new InvalidOperationException("更新已完成，自动重开失败，请手动重新打开登录器。");
            pythonRestarting=true;_forceClose=true;System.Windows.Application.Current.Shutdown();return true;
        }
        catch(InvalidOperationException){throw;}
        catch(Exception){throw new InvalidOperationException("客户端自动更新未完成，请检查下载链接、网络或文件占用后重试。");}
        finally{pythonUpdating=false;ClearPythonUpdateProgress();RefreshLoginButtons();}
    }

    private async Task LoginPythonAsync(string account,string password)
    {
        const int slot = GameProcessManager.MainSlot;
        if (pythonUpdating || !pythonInitialCheckDone || _isLoggingIn || _mainLoggedIn || GameProcessManager.AnyRunning) return;
        _isLoggingIn = true;
        RefreshLoginButtons();
        PythonLauncherApi? api=null;PythonSession? session=null;string? runtime=null;
        try
        {
            if(string.IsNullOrWhiteSpace(account)||string.IsNullOrEmpty(password))throw new InvalidOperationException("请填写账号和密码。");
            api=PythonClientLauncher.CreateApi();
            ShowStatus("正在校验 Python 登录服务和客户端…",Brushes.LightGray);
            var meta=await api.MetaAsync();
            if(await EnsurePythonResourcesAsync(api,meta,true))return;
            var clientExe=PythonClientLocation.RequireExecutable();
            await Task.Run(()=>PythonClientLauncher.ValidateClient(clientExe,meta));
            ShowStatus("正在通过 HTTPS 验证账号…",Brushes.LightGray);
            session=await api.LoginAsync(account,password,meta);
            var plan=await Task.Run(()=>PythonClientLauncher.ValidateClient(clientExe,session.Stage));
            var key=session.Stage.PvfProfile=="keyset96"?await api.KeyAsync(session):null;
            ShowStatus("认证成功，正在校验 PVF 并挂起启动客户端…",Brushes.LightGray);
            var relay=await api.PartyRelayAsync(session);
            var launched=await PythonClientLauncher.LaunchAsync(plan,session,key,relay);runtime=launched.RuntimeDirectory;key=null;
            pythonSessions[slot]=(api,session,runtime,account);api=null;session=null;
            _pythonCdkPage?.RefreshAccounts();
            _mainLoggedIn = true;
            MainLoggedInText.Text = "已登录："+account;
            MainLoggedInText.Visibility = Visibility.Visible;
            PasswordBox.Password = "";
            GameProcessManager.RegisterProcess(slot,launched.Process,account);
            ShowStatus("客户端已启动",Brushes.LightGreen);
            if(RememberCheckBox.IsChecked==true)
            {
                try{PythonRememberedLogin.Current().Save(slot,account,password);}
                catch { ShowStatus("游戏已启动，但密码未能保存，请检查数据目录权限。",Brushes.OrangeRed); }
            }
            else ClearPythonLogin();
            WindowState=WindowState.Minimized;
        }
        catch(InvalidOperationException ex){ShowStatus(ex.Message,Brushes.OrangeRed);}
        catch{ShowStatus("Python 客户端启动未完成，请核对 HTTPS 配置、证书和完整客户端。",Brushes.OrangeRed);}
        finally
        {
            if(api is not null){if(session is not null)try{await api.LogoutAsync(session);}catch{}api.Dispose();if(runtime is not null)PythonClientLauncher.Cleanup(runtime);}
            _isLoggingIn = false;
            RefreshLoginButtons();
        }
    }
    private async void RemovePythonSession(int slot)
    {
        if(!pythonSessions.Remove(slot,out var saved))return;
        _pythonCdkPage?.RefreshAccounts();
        try{await saved.Api.LogoutAsync(saved.Session);}catch{}finally{saved.Api.Dispose();PythonClientLauncher.Cleanup(saved.Runtime);}
    }
}
#endif
