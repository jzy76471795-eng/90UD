#if PYTHON90
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace DNFLogin.Services;

public sealed class PythonClientPlan
{
    public required string Root {get;init;}
    public required string Exe {get;init;}
    public required string Pvf {get;init;}
    public required List<string> Dlls {get;init;}
}

public static class PythonClientLauncher
{
    private static readonly PythonNativeRoutePool NativeRoutes=new(ReportChannelRoute);
    private static readonly PythonChannelRoutePool ChannelRoutes=new(ReportChannelRoute);
    private static void ReportChannelRoute(string text)
    {
        ClientDiagnostics.Append(LauncherPackage.ConfigurationDirectory, "channel-route.log", text);
    }
    public static void LockSettings()
    {
        var settings=AppConfig.Settings;
        var first=!File.Exists(AppConfig.ConfigPath);
        settings.CurrentProfileId="90";
        var profile=AppConfig.CurrentProfile;
        profile.LoginMode="Remote";
        if(first && LauncherPackage.Current is null)
        {
            profile.RemotePort="17605";
            profile.RemoteIp="https://127.0.0.1:17605";
            profile.RemoteCAFile=Path.Combine(AppContext.BaseDirectory,"server-cert.pem");
        }
        profile.CopyToTopLevel(settings);
        // Always follow this executable; never reuse another client's saved path.
        settings.GamePath = profile.GamePath = PythonClientLocation.FindExecutable(PythonLauncherPaths.LauncherDirectory);
        AppConfig.Mode=LoginMode.Remote;
        LauncherPackage.ApplyConnection(settings);
    }

    public static PythonLauncherApi CreateApi()
    {
        var host=LauncherPackage.Current?.LoginUrl.GetLeftPart(UriPartial.Authority)??AppConfig.Settings.RemoteIp.Trim();
        if(string.IsNullOrWhiteSpace(host))throw new InvalidOperationException("登录器缺少服务器地址，请使用完整的玩家登录器。");
        var text=host.StartsWith("https://",StringComparison.OrdinalIgnoreCase) ? host : $"https://{host}:{AppConfig.Settings.RemotePort}";
        var localCa = Path.Combine(PythonLauncherPaths.LauncherDirectory,"server-cert.pem");
        if (!File.Exists(localCa)) localCa = Path.Combine(AppContext.BaseDirectory,"server-cert.pem");
        var configuredCa = AppConfig.Settings.RemoteCAFile;
        var ca = LauncherPackage.ResolveCaFile(File.Exists(localCa) ? localCa : configuredCa);
        return new PythonLauncherApi(PythonLauncherApi.ValidateBaseUri(text),trustedCaFile:ca,trustedCaPem:LauncherPackage.Current?.CertificatePem);
    }

    private static Stream Resource(string name) => Assembly.GetExecutingAssembly().GetManifestResourceStream("DNFLogin.Resources."+name) ?? throw new InvalidOperationException("缺少内嵌 Py90 启动组件："+name);
    public static string Sha256(string file)
    {
        using var stream=File.OpenRead(file);
        return Convert.ToHexString(CompatHash.Stream(stream)).ToLowerInvariant();
    }

    public static PythonClientPlan ValidateClient(string selectedPath,PythonStage3 stage)
    {
        if(string.IsNullOrWhiteSpace(selectedPath))throw new InvalidOperationException("请选择冻结 90US 客户端目录（包含 DFO.exe 与 Script.pvf）。");
        var root=Path.GetFullPath(File.Exists(selectedPath)?Path.GetDirectoryName(selectedPath)!:selectedPath);
        var exe=Path.Combine(root,"DFO.exe");var pvf=Path.Combine(root,"Script.pvf");
        PythonUpdateTrust.NoLinks(exe);PythonUpdateTrust.NoLinks(pvf);
        if(!File.Exists(exe))throw new InvalidOperationException("客户端缺少 DFO.exe，请选择完整客户端目录。");
        if(!File.Exists(pvf)||Sha256(pvf)!=stage.PvfSha256)throw new InvalidOperationException("本地 Script.pvf 与服务器发布 SHA256 不一致，请先更新资源；游戏尚未启动。");
        return new PythonClientPlan {Root=root,Exe=exe,Pvf=pvf,Dlls=ValidateComponents(root,stage.Patches)};
    }

    // Resolve the files required to launch; game file version/hash policy is
    // PVF-only. Server patch revisions never overwrite local extension DLLs.
    public static IReadOnlySet<string> PairedComponentsForUpdate(string root, bool allowRepair=false)
    {
        string[] required={"us_extend_dll/usdof_game_assist.dll","us_extend_dll/usdof_adventure_tooltip.dll"};
        root=Path.GetFullPath(root);PythonUpdateTrust.NoLinks(root);
        using var stream=Resource("PythonClientPins.json");using var pins=JsonDocument.Parse(stream);
        var keep=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach(var relative in required)
        {
            var pin=pins.RootElement.GetProperty("optional_dlls").EnumerateArray().Single(row=>row.GetProperty("relative_path").GetString()==relative);
            var file=PythonUpdateTrust.Within(root,relative);PythonUpdateTrust.NoLinks(file);
            if(!File.Exists(file)||new FileInfo(file).Length!=pin.GetProperty("bytes").GetInt64()||Sha256(file)!=pin.GetProperty("sha256").GetString())
            {
                if(allowRepair) continue;
                throw new InvalidOperationException("游戏组件与登录器不配套，请使用完整更新包："+Path.GetFileName(file));
            }
            keep.Add(relative);
        }
        return keep;
    }

    public static List<string> ValidateComponents(string root,PythonPatchManifest? patches=null)
    {
        root=Path.GetFullPath(root);PythonUpdateTrust.NoLinks(root);
        using var pinStream=Resource("PythonClientPins.json");using var pins=JsonDocument.Parse(pinStream);
        var paths=new List<string>();
        var external=Path.Combine(root,"us_extend_dll");
        void Add(string relative,bool optional)
        {
            var path=PythonUpdateTrust.Within(root,relative);
            PythonUpdateTrust.NoLinks(path);
            if(!File.Exists(path))
            {
                if(optional)return;
                throw new InvalidOperationException("启动所需文件缺失："+Path.GetFileName(path));
            }
            if(!string.Equals(Path.GetFileName(path),"forbidden-func.dll",StringComparison.OrdinalIgnoreCase)
                &&!paths.Contains(path,StringComparer.OrdinalIgnoreCase))paths.Add(path);
        }
        foreach(var row in pins.RootElement.GetProperty("dlls").EnumerateArray())
        {
            var relative=row.GetProperty("relative_path").GetString()!;
            Add(relative,relative.StartsWith("us_extend_dll/",StringComparison.OrdinalIgnoreCase));
        }
        // Require the paired Chinese extension only for this shipped main executable.
        var exe=Path.Combine(root,"DFO.exe");
        const string cnPath="us_extend_dll/usdof_cn_fix.dll";
        var gameHash=File.Exists(exe)?Sha256(exe):null;
        const string combinedGame = "5385a7c47d49517d5834c1c0000d28e363165e411f5ef2079e9e627595883097";
        if(gameHash is ("0d2da067512bc812d2430afeadb2c13a4d53bd0e7f76801fab228bef52a1ee8e" or combinedGame))
        {
            Add(cnPath,false);
            var release=gameHash==combinedGame?"combined-native-compat-20261122":"pinyin-focus-20261120";
            var expected=pins.RootElement.GetProperty("dlls").EnumerateArray().Single(r=>r.GetProperty("relative_path").GetString()==cnPath)
                .GetProperty("approved_variants").EnumerateArray().Where(r=>
                {
                    var pinnedRelease = r.GetProperty("release").GetString();
                    return pinnedRelease == release || pinnedRelease == release + "-player-logs" ||
                        (gameHash == combinedGame && (pinnedRelease is "chat-hotkey-focus-20261125" or "chat-cn-native-return-20261126" or "chat-cn-preedit-release-20261126" or "unicode-packet-frame-20261126"));
                });
            var cn=PythonUpdateTrust.Within(root,cnPath);
            var length=new FileInfo(cn).Length;
            var sha=Sha256(cn);
            if(!expected.Any(r=>length==r.GetProperty("bytes").GetInt64() && sha==r.GetProperty("sha256").GetString()))
                throw new InvalidOperationException("中文扩展与当前游戏版本不配套，请更新资源。");
        }
        const string assistRelative="us_extend_dll/usdof_game_assist.dll";
        Add(assistRelative,false);
        _=PairedComponentsForUpdate(root);
        if(pins.RootElement.TryGetProperty("optional_dlls",out var optional))
            foreach(var row in optional.EnumerateArray())Add(row.GetProperty("relative_path").GetString()!,true);
        if(Directory.Exists(external))
            foreach(var file in Directory.GetFiles(external,"*.dll").OrderBy(p=>p,StringComparer.OrdinalIgnoreCase))
                Add("us_extend_dll/"+Path.GetFileName(file),true);
        return paths;
    }

    private static string LaunchComponent(string runtime, string component)
    {
        const string embeddedName = "usdof_game_assist.dll";
        if (!Path.GetFileName(component).Equals(embeddedName, StringComparison.OrdinalIgnoreCase))
            return component;
        using var resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("DNFLogin.Resources." + embeddedName);
        if (resource is null) return component;
        var target = Path.Combine(runtime, embeddedName);
        using var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
        resource.CopyTo(output);
        output.Flush(true);
        return target;
    }

    private static string Extract(string directory,string resource)
    {
        var file=Path.Combine(directory,resource);
        using var src=Resource(resource);using var dst=new FileStream(file,FileMode.CreateNew,FileAccess.Write,FileShare.Read);
        src.CopyTo(dst);dst.Flush(true);return file;
    }

    // The native x86 launcher creates suspended, injects all components, checks key-ready, then resumes.
    public static async Task<(Process Process,string RuntimeDirectory)> LaunchAsync(PythonClientPlan plan,PythonSession session,string? key,PythonRelayAuthorization? relayAuthorization=null,Func<Task<PythonRelayAuthorization>>? renewRelay=null)
    {
        var custom=session.Stage.PvfProfile=="keyset96";
        if(custom&&(key is null||key.Length!=96||!key.All(Uri.IsHexDigit)))throw new InvalidOperationException("缺少已认证的 PVF96 密钥。");
        var runtime=Path.Combine(LauncherPackage.RuntimeDirectory,Guid.NewGuid().ToString("N"));
        PythonUpdateTrust.NoLinks(runtime);Directory.CreateDirectory(runtime);
        var injector=Extract(runtime,"launch_py90_client.exe");
        var helper=custom?Extract(runtime,"DNFHelper-Py90.dll"):null;
        var eventName=@"Local\DNFLogin-Py90-Pvf-"+Guid.NewGuid().ToString("N");
        using var ready=new EventWaitHandle(false,EventResetMode.ManualReset,eventName);
        var psi=new ProcessStartInfo(injector){UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=plan.Root,RedirectStandardOutput=true,RedirectStandardError=true};
        psi.ArgumentList.Add("--exe");psi.ArgumentList.Add(plan.Exe);psi.ArgumentList.Add("--workdir");psi.ArgumentList.Add(plan.Root);
        if(helper is not null){psi.ArgumentList.Add("--dll");psi.ArgumentList.Add(helper);}
        foreach(var dll in plan.Dlls){psi.ArgumentList.Add("--dll");psi.ArgumentList.Add(LaunchComponent(runtime,dll));}
        psi.ArgumentList.Add("--args");psi.ArgumentList.Add(session.Stage.LaunchArgument);
        foreach(var name in psi.Environment.Keys.ToArray())if(name.StartsWith("USDOF_",StringComparison.OrdinalIgnoreCase)||name.StartsWith("DNF_REMOTE_",StringComparison.OrdinalIgnoreCase)||name.StartsWith("DNF_PYTHON_",StringComparison.OrdinalIgnoreCase)||name=="DNF_PVF_KEYSET")psi.Environment.Remove(name);
        var logDirectory=LauncherPackage.ConfigurationDirectory;
        var detailedDiagnostics=ClientDiagnostics.Detailed(logDirectory);
        if(detailedDiagnostics)psi.Environment["USDOF_PLAYER_DIAGNOSTICS"]="1";
        if(session.Stage.NativeTunnelVersion==2)psi.Environment["DNF_PYTHON_ALLOW_SECOND_INSTANCE"]="1";
        var temp=Path.Combine(runtime,"game-temp");Directory.CreateDirectory(temp);
        foreach(var name in new[]{"TEMP","TMP","TMPDIR"})psi.Environment[name]=temp;
        var sidechannels=new Dictionary<string,string>{["USDOF_NATIVE_LOADOUT_PLAN_PATH"]="USDOFLoadoutNativePlan.bin",["USDOF_NATIVE_LOADOUT_ACK_PATH"]="USDOFLoadoutNativeAck.bin",["USDOF_NATIVE_LOADOUT_THREAD_PATH"]="USDOFNativeLoadoutThread.bin",["USDOF_FUNC_EXTEND_TOAST_REQUEST_PATH"]="USDOFFuncExtendToastRequest.bin",["USDOF_FUNC_EXTEND_THREAD_PATH"]="USDOFFuncExtendThread.bin",["USDOF_FUNC_EXTEND_VIDEO_COMMAND_PATH"]="USDOFFuncExtendVideoCommand.bin"};
        foreach(var pair in sidechannels)psi.Environment[pair.Key]=Path.Combine(temp,pair.Value);
        var adventureExtension = plan.Dlls.FirstOrDefault(dll =>
            string.Equals(Path.GetFileName(dll), "cn2018_adventure_statistics.dll", StringComparison.OrdinalIgnoreCase));
        var weeklyExtension = plan.Dlls.FirstOrDefault(dll =>
            string.Equals(Path.GetFileName(dll), "usdof_weekly_attendance.dll", StringComparison.OrdinalIgnoreCase));
        bool trustedAdventure = adventureExtension is not null &&
            Sha256(adventureExtension) is ("5dd8f5c6795d49a3f0b82b6a35bfe85889c963203641b9fa904f06a8eb8912c1" or "3b689027a581834f967bb8d4aa43b869f4771b7cc0db49daf77c36f841565c5d");
        bool trustedAttendance = weeklyExtension is not null && Sha256(weeklyExtension) == "0cababd13083deb5c23cc25a11916e2fcd5436a87270147f19d10397af1f439f";
        string? adventurePipe = trustedAdventure || trustedAttendance
            ? "USDOF-Adventure-" + Guid.NewGuid().ToString("N") : null;
        if (adventurePipe is not null) psi.Environment["USDOF_ADVENTURE_PIPE"] = adventurePipe;
        foreach(var pair in new Dictionary<string,string>{["__COMPAT_LAYER"]="DPIUNAWARE",["USDOF_CLIENT_DIR"]=plan.Root,["USDOF_CLIENT_EXE"]=plan.Exe,["USDOF_PVF_PATH"]=plan.Pvf,["USDOF_EXPECTED_PVF_PROFILE"]=session.Stage.PvfProfile,["USDOF_EXPECTED_PVF_SHA256"]=session.Stage.PvfSha256,["USDOF_PVF_BRIDGE"]=session.Stage.PvfProfile=="utility-guarded"?"1":"0",["USDOF_SKIP_PVF_STARTUP_PREPROCESS"]="1",["USDOF_FUNC_EXTEND_VIDEO"]="1",["USDOF_FUNC_EXTEND_VIDEO_HOOK"]="1",["USDOF_GAME_PLAIN_WIRE"]="0",["USDOF_GAME_C2S_PLAIN_WIRE"]="1",["USDOF_GAME_S2C_PLAIN_WIRE"]="0",["USDOF_GAME_PLAIN_WIRE_HEADER"]="16"})psi.Environment[pair.Key]=pair.Value;
        psi.Environment["USDOF_GAME_PROBE_IPV4"]=session.Stage.ChannelHost;
        psi.Environment["DNF_PYTHON_CLIENT_CODE_KEY"]=ClientCodeProtection.CodeKey();
        if(custom){psi.Environment["DNF_PYTHON_PVF_REQUIRED"]="1";psi.Environment["DNF_PYTHON_PVF_READY_EVENT"]=eventName;psi.Environment["DNF_PYTHON_PVF_KEYSET"]=key;}
        var gameAssist=PythonGameAssist.Prepare(plan.Root,psi);
        IDisposable? channelRoute=null;
        PythonPartyRelay? relay=null;
        Process? startedGame=null;
        await PythonNativeRoutePool.LaunchGate.WaitAsync();
        try
        {
            channelRoute=session.Stage.NativeTunnelVersion==2?NativeRoutes.Acquire(plan,session):ChannelRoutes.Acquire(session.Stage.ChannelHost);
            if(session.Stage.PartyRelayVersion==1) {
                if(relayAuthorization is null)throw new InvalidOperationException("缺少已认证的组队启动凭据。");
                renewRelay??=async()=>{using var api=CreateApi();return await api.PartyRelayAsync(session).ConfigureAwait(false)??throw new InvalidOperationException("组队恢复授权不可用。");};
                relay=await PythonPartyRelay.StartAsync(relayAuthorization,Extract(runtime,"party-relay.exe"),runtime,renewRelay);
                relay.Configure(psi);
            }
            using var launcher=Process.Start(psi)??throw new InvalidOperationException("Py90 挂起启动器未运行。");
            if(channelRoute is PythonNativeRoutePool.GameLease pendingNative)pendingNative.InjectorPid=launcher.Id;
            psi.Environment.Remove("DNF_PYTHON_PVF_KEYSET");
            psi.Environment.Remove("DNF_PYTHON_CLIENT_CODE_KEY");
            var stdout=launcher.StandardOutput.ReadToEndAsync();var stderr=launcher.StandardError.ReadToEndAsync();
            await launcher.WaitForExitAsync();var output=await stdout;var errors=await stderr;
            var match=Regex.Match(output,@"\binjected and resumed pid=([1-9][0-9]*)\b");
            bool readySignaled=!custom||ready.WaitOne(0);
            if(launcher.ExitCode!=0||!match.Success||!readySignaled||detailedDiagnostics)
                ClientDiagnostics.Append(logDirectory,"launch-diagnostics.log",
                    $"build={typeof(PythonClientLauncher).Assembly.GetName().Version} exit={launcher.ExitCode} pid_reported={match.Success} pvf_ready={readySignaled}");
            if(detailedDiagnostics)
            {
                var safe=Regex.Matches(output+"\n"+errors,@"\[launch\] (?:instance branch before resume=[0-9A-F]{2}|child exited before startup acknowledgement pid=[0-9]+ code=[0-9A-F]{8}|scoped one game instance mutex slots=[0-9]+|instance mutex scope ownership failed; suspended child stopped)").Select(m=>m.Value);
                foreach(var line in safe)ClientDiagnostics.Append(logDirectory,"launch-diagnostics.log",line);
            }
            if(launcher.ExitCode==6)throw new InvalidOperationException("客户端主程序与加密登录器不配套或文件已损坏，请安装配套客户端更新包。");
            if(launcher.ExitCode==7)throw new InvalidOperationException("游戏在启动期间提前退出，请保留启动诊断日志以便检查。");
            if(launcher.ExitCode!=0||!match.Success||!readySignaled)throw new InvalidOperationException("Py90 注入或 PVF 签名校验未通过；游戏未恢复。请核对冻结客户端及密钥发布。");
            var process=Process.GetProcessById(int.Parse(match.Groups[1].Value));
            if(PythonTcpOwner.Parent(process)!=launcher.Id||!string.Equals(process.MainModule?.FileName,plan.Exe,StringComparison.OrdinalIgnoreCase)||process.StartTime.ToUniversalTime()<launcher.StartTime.ToUniversalTime().AddSeconds(-1))
                throw new InvalidOperationException("游戏启动进程身份不匹配。");
            startedGame=process;
            await PythonGameAssist.StartAsync(process,runtime,session,gameAssist);
            psi.Environment.Remove("USDOF_ASSIST_COOKIE");
            if(channelRoute is PythonNativeRoutePool.GameLease native)native.Bind(process.Id);
            if(relay is not null)await relay.BindAsync(process);
            var ownedRoute=channelRoute;
            var ownedRelay=relay;
            process.Exited+=(_,_)=>{ownedRelay?.Dispose();ownedRoute.Dispose();};
            process.EnableRaisingEvents=true;
            if(process.HasExited)throw new InvalidOperationException("游戏进程已提前退出。");
            if (adventurePipe is not null) _ = PythonAdventurePipe.RunAsync(adventurePipe, process, session);
            channelRoute=null; // The game's exit now owns this idempotent lease.
            relay=null;
            startedGame=null;
            return(process,runtime);
        }
        catch {
            relay?.Dispose();channelRoute?.Dispose();
            if(startedGame is not null)try{if(!startedGame.HasExited){startedGame.Kill();await startedGame.WaitForExitAsync();}}catch(InvalidOperationException){}catch(System.ComponentModel.Win32Exception){}
            Cleanup(runtime);throw;
        }
        finally{PythonNativeRoutePool.LaunchGate.Release();psi.Environment.Remove("DNF_PYTHON_PVF_KEYSET");psi.Environment.Remove("DNF_PYTHON_CLIENT_CODE_KEY");}
    }
    public static void Cleanup(string directory)
    {
        try {
            var path=Path.GetFullPath(directory);var root=Path.GetFullPath(LauncherPackage.RuntimeDirectory);
            if(!string.Equals(Path.GetDirectoryName(path),root,StringComparison.OrdinalIgnoreCase)||!Regex.IsMatch(Path.GetFileName(path),@"\A[0-9a-f]{32}\z"))return;
            PythonUpdateTrust.NoLinks(path);Directory.Delete(path,true);
        }catch(IOException){}catch(UnauthorizedAccessException){}catch(InvalidOperationException){}
    }
}
#endif
