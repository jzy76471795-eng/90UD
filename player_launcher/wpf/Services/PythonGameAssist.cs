using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
namespace DNFLogin.Services;

// Launch-only bootstrap. The game owns settings/input and its bridge lifetime.
internal static class PythonGameAssist
{
 internal sealed record StartInfo(string Pipe,string Cookie);
 [DllImport("kernel32.dll",EntryPoint="WritePrivateProfileStringW",CharSet=CharSet.Unicode)]static extern bool WritePrivateProfileString(string section,string key,string value,string file);
 internal static StartInfo Prepare(string root,ProcessStartInfo game)
 {
  var info=new StartInfo("USDOF-GameAssist-"+Guid.NewGuid().ToString("N"),Convert.ToHexString(RandomNumberGenerator.GetBytes(32)));
  game.Environment["USDOF_ASSIST_PIPE"]=info.Pipe;game.Environment["USDOF_ASSIST_COOKIE"]=info.Cookie;
  string ini=Path.Combine(root,"us_extend_dll","GameAssist.ini");
  if(!File.Exists(ini))
  {
   var s=AppConfig.Settings.GameAssist;
   int Mask(int[]? values)=>(values??[]).Where(i=>i>=0&&i<7).Aggregate(0,(m,i)=>m|(1<<i));
   foreach(var pair in new Dictionary<string,int>{{"Pickup",s.Pickup?1:0},{"InstantDoor",s.InstantDoor?1:0},{"AutoFire",s.AutoFire?1:0},{"Sell",s.Sell?1:0},{"Disjoint",s.Disjoint?1:0},{"FireKey",s.FireKey is {Length:1} k&&k[0]>='A'&&k[0]<='Z'?k[0]:'X'},{"IntervalMs",s.IntervalMs},{"SellMask",Mask(s.SellRarities)},{"DisjointMask",Mask(s.DisjointRarities)}})
    if(!WritePrivateProfileString("Assist",pair.Key,pair.Value.ToString(System.Globalization.CultureInfo.InvariantCulture),ini))throw new IOException("无法保存游戏内辅助设置。");
  }
  return info;
 }
 internal static async Task StartAsync(Process game,string runtime,PythonSession session,StartInfo info)
 {
  foreach(string name in new[]{"GameAssistBridge.dll","GameAssistBridge.runtimeconfig.json"})
  {
   using var resource=Assembly.GetExecutingAssembly().GetManifestResourceStream("DNFLogin.Resources."+name)??throw new InvalidOperationException("缺少游戏内辅助启动组件。");
   using var output=new FileStream(Path.Combine(runtime,name),FileMode.Create,FileAccess.Write,FileShare.Read);resource.CopyTo(output);output.Flush(true);
  }
  string dotnet=Path.GetFullPath(Path.Combine(RuntimeEnvironment.GetRuntimeDirectory(),"..","..","..","dotnet.exe"));
  if(!File.Exists(dotnet))throw new InvalidOperationException("游戏内辅助运行环境不完整。");
  var start=new ProcessStartInfo(dotnet){UseShellExecute=false,CreateNoWindow=true,RedirectStandardInput=true,WorkingDirectory=runtime};start.ArgumentList.Add(Path.Combine(runtime,"GameAssistBridge.dll"));
  foreach(string key in new[]{"TEMP","TMP","TMPDIR"})start.Environment[key]=Path.Combine(runtime,"game-temp");
  start.Environment["DOTNET_CLI_TELEMETRY_OPTOUT"]="1";start.Environment["DOTNET_EnableDiagnostics"]="0";
  using var api=PythonClientLauncher.CreateApi();
  using var bridge=Process.Start(start)??throw new InvalidOperationException("游戏内辅助启动失败。");
  // Secrets travel once over a private inherited stdin pipe, never command-line/files.
  await bridge.StandardInput.WriteLineAsync(JsonSerializer.Serialize(new{info.Pipe,info.Cookie,Pid=game.Id,StartTicks=game.StartTime.ToUniversalTime().Ticks,Exe=game.MainModule!.FileName,Endpoint=api.Endpoint.AbsoluteUri,Token=session.AccessToken,Certificate=api.UpdateCertificatePem}));
  bridge.StandardInput.Close();
  // Disposing the local Process object does not stop the game-owned bridge.
 }
}
