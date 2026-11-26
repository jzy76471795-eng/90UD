using System.Diagnostics;
using System.IO.Pipes;
using System.Net.Http.Headers;
using System.Net.Security;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;

record Bootstrap(string Pipe,string Cookie,int Pid,long StartTicks,string Exe,string Endpoint,string Token,string Certificate);
record Request(uint Action,uint Pickup,uint Mask);
static class Program
{
 const uint Magic=0x31414755;
 [DllImport("kernel32.dll",EntryPoint="GetNamedPipeClientProcessId",SetLastError=true)] static extern bool GetNamedPipeClientProcessId(Microsoft.Win32.SafeHandles.SafePipeHandle pipe,out uint id);
 static async Task<int> Main(string[] args)
 {
  if(args.SequenceEqual(new[]{"--self-test"})){Validate(new Request(1,1,0));try{Validate(new Request(7,0,128));return 2;}catch(InvalidDataException){}Console.WriteLine("bridge input guards passed");return 0;}
  try
  {
   string? line=await Console.In.ReadLineAsync();if(line is null||line.Length>65536)return 3;
   var boot=JsonSerializer.Deserialize<Bootstrap>(line)??throw new InvalidDataException();
   if(!boot.Pipe.StartsWith("USDOF-GameAssist-",StringComparison.Ordinal)||boot.Cookie.Length!=64||!boot.Cookie.All(Uri.IsHexDigit))return 4;
   var game=Process.GetProcessById(boot.Pid);
   if(game.StartTime.ToUniversalTime().Ticks!=boot.StartTicks||!string.Equals(game.MainModule?.FileName,boot.Exe,StringComparison.OrdinalIgnoreCase))return 5;
   using var stop=new CancellationTokenSource();game.EnableRaisingEvents=true;game.Exited+=(_,_)=>stop.Cancel();if(game.HasExited)return 0;
   using var http=Http(boot);long nextBatch=0;
   var adventure=new Adventure();
   while(!stop.IsCancellationRequested)
   {
    using var pipe=new NamedPipeServerStream(boot.Pipe,PipeDirection.InOut,1,PipeTransmissionMode.Message,PipeOptions.Asynchronous|PipeOptions.CurrentUserOnly,4096,Adventure.MaxReplyBytes);
    await pipe.WaitForConnectionAsync(stop.Token);
    if(!GetNamedPipeClientProcessId(pipe.SafePipeHandle,out uint pid)||pid!=boot.Pid)continue;
    using var timeout=CancellationTokenSource.CreateLinkedTokenSource(stop.Token);timeout.CancelAfter(TimeSpan.FromSeconds(12));
    try
    {
     byte[] wire=new byte[48];await pipe.ReadExactlyAsync(wire,timeout.Token);
     if(BitConverter.ToUInt32(wire)!=Magic||!CryptographicOperations.FixedTimeEquals(wire.AsSpan(4,32),Convert.FromHexString(boot.Cookie)))continue;
     var req=new Request(BitConverter.ToUInt32(wire,36),BitConverter.ToUInt32(wire,40),BitConverter.ToUInt32(wire,44));Validate(req);
     if (req.Action >= 9)
     {
      var state=await adventure.Run(req,data=>Post(http,data,timeout.Token));
      await Reply(pipe,0,state,timeout.Token);
      continue;
     }
     if(req.Action is 2 or 3){if(Environment.TickCount64<nextBatch)throw new InvalidOperationException("操作过快，请稍候");nextBatch=Environment.TickCount64+800;}
     var configured=await Post(http,new {action="configure",pickup=req.Pickup==1,request_id=Guid.NewGuid().ToString("N")},timeout.Token);
     string text=req.Action==1?"设置已同步":"";
     if(req.Action is 2 or 3)
     {
      string target=Str(configured,"target");if(target.Length==0)throw new InvalidOperationException("请在角色城镇中使用");
      if(req.Mask==0)throw new InvalidOperationException("请先选择装备品阶");
      var result=await Post(http,new {action=req.Action==2?"sell":"disjoint",target,rarities=Enumerable.Range(0,7).Where(i=>(req.Mask&(1u<<i))!=0).ToArray(),request_id=Guid.NewGuid().ToString("N")},timeout.Token);
      int n=result.GetProperty("processed").GetInt32();text=$"已{(req.Action==2?"出售":"分解")} {n} 件装备";
     }
     else if(req.Action>=4)
     {
      string target=Str(configured,"door_target");if(target.Length==0)throw new InvalidOperationException("请等待副本房间加载完成");
      var result=await Post(http,new {action=new[]{"door-left","door-up","door-right","door-down","door-cancel"}[req.Action-4],target,request_id=Guid.NewGuid().ToString("N")},timeout.Token);
      text=Str(result,"state")=="waiting"?"等待队友选择同方向过门":"过门请求已提交";
     }
     await Reply(pipe,0,text,timeout.Token);
    }
    catch(Exception e) when(e is not OutOfMemoryException)
    {
     if(pipe.IsConnected)try{await Reply(pipe,1,Safe(e),stop.Token);}catch(IOException){}catch(OperationCanceledException){}
    }
   }
  }
  catch(OperationCanceledException){return 0;}
  catch(Exception){return 6;} // No credentials or auth responses in diagnostics.
  return 0;
 }
 internal static void Validate(Request r)
 {
  if (r.Action is >= 1 and <= 8 && r.Pickup <= 1 && r.Mask <= 127 || Adventure.IsRequest(r)) return;
  throw new InvalidDataException("无效的辅助请求");
 }
 static string Str(JsonElement e,string key)=>e.TryGetProperty(key,out var v)&&v.ValueKind==JsonValueKind.String?v.GetString()??"":"";
 static string Safe(Exception e)=>e is InvalidOperationException or InvalidDataException?e.Message:"连接未完成，请稍后重试";
 static HttpClient Http(Bootstrap b)
 {
  var uri=new Uri(b.Endpoint);if(uri.Scheme!="https"||uri.AbsolutePath!="/"||uri.UserInfo.Length!=0)throw new InvalidDataException();
  var roots=new X509Certificate2Collection();if(b.Certificate.Length!=0)roots.ImportFromPem(b.Certificate);
  var h=new HttpClientHandler{SslProtocols=System.Security.Authentication.SslProtocols.Tls12,AllowAutoRedirect=false,UseProxy=false,UseCookies=false};
  if(roots.Count!=0)h.ServerCertificateCustomValidationCallback=(_,cert,_,errors)=>
  {
   if(cert is null||(errors&(SslPolicyErrors.RemoteCertificateNameMismatch|SslPolicyErrors.RemoteCertificateNotAvailable))!=0)return false;
   using var chain=new X509Chain();chain.ChainPolicy.TrustMode=X509ChainTrustMode.CustomRootTrust;chain.ChainPolicy.CustomTrustStore.AddRange(roots);chain.ChainPolicy.RevocationMode=X509RevocationMode.NoCheck;chain.ChainPolicy.ApplicationPolicy.Add(new Oid("1.3.6.1.5.5.7.3.1"));return chain.Build(cert);
  };
  var client=new HttpClient(h){BaseAddress=uri,Timeout=TimeSpan.FromSeconds(10),MaxResponseContentBufferSize=65536};client.DefaultRequestHeaders.Authorization=new AuthenticationHeaderValue("Bearer",b.Token);return client;
 }
 static async Task<JsonElement> Post(HttpClient http,object data,CancellationToken stop)
 {
  using var body=new StringContent(JsonSerializer.Serialize(data),Encoding.UTF8,"application/json");using var response=await http.PostAsync("/launcher/v1/assist",body,stop);
  string raw=await response.Content.ReadAsStringAsync(stop);if(raw.Length>65536)throw new InvalidDataException();
  using var doc=JsonDocument.Parse(raw);
  if(!response.IsSuccessStatusCode){string message="辅助请求未确认";if(doc.RootElement.TryGetProperty("error",out var error))message=Str(error,"message") is {Length:>0} s?s:message;throw new InvalidOperationException(message);}
  return doc.RootElement.Clone();
 }
 static Task Reply(Stream pipe,uint code,string text,CancellationToken stop) =>
  Reply(pipe,code,Encoding.UTF8.GetBytes(text.Length>120?text[..120]:text),stop);
 static async Task Reply(Stream pipe,uint code,byte[] message,CancellationToken stop)
 {
  byte[] wire=new byte[12+message.Length];BitConverter.GetBytes(Magic).CopyTo(wire,0);BitConverter.GetBytes(code).CopyTo(wire,4);BitConverter.GetBytes(message.Length).CopyTo(wire,8);message.CopyTo(wire,12);await pipe.WriteAsync(wire,stop);await pipe.FlushAsync(stop);
 }
}
