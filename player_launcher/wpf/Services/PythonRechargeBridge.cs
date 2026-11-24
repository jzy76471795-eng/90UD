#if PYTHON90
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace DNFLogin.Services;

// Browser navigation stays on loopback until the normal pinned HTTPS client
// resolves the current administrator setting. Account text in the native URL
// is deliberately ignored and never forwarded to a payment site.
internal sealed class PythonRechargeBridge:IDisposable
{
    private readonly TcpListener listener;
    private readonly Func<CancellationToken,Task<Uri?>> resolve;
    private readonly Action<string>? report;
    private readonly CancellationTokenSource stopping=new();
    private readonly ConcurrentDictionary<TcpClient,byte> connections=new();
    private int disposed;
    internal int LocalPort {get;}

    internal PythonRechargeBridge(Func<CancellationToken,Task<Uri?>> resolve,Action<string>? report=null,int port=17608)
    {
        this.resolve=resolve;this.report=report;
        listener=new TcpListener(IPAddress.Loopback,port);
        listener.Server.ExclusiveAddressUse=true;
        try{listener.Start();}catch{listener.Stop();stopping.Dispose();throw;}
        LocalPort=((IPEndPoint)listener.LocalEndpoint).Port;
        _=AcceptAsync();
    }
    private async Task AcceptAsync()
    {
        try{
            while(!stopping.IsCancellationRequested){
                var client=await listener.AcceptTcpClientAsync(stopping.Token).ConfigureAwait(false);
                if(connections.Count>=8){client.Dispose();continue;}
                connections.TryAdd(client,0);_=HandleAsync(client);
            }
        }catch(OperationCanceledException){}catch(ObjectDisposedException){}
        catch(SocketException){if(!stopping.IsCancellationRequested)Report("listener unavailable");}
    }
    private void Report(string message){try{report?.Invoke("recharge "+message);}catch{}}
    private async Task HandleAsync(TcpClient client)
    {
        using(client)
        using(var lifetime=CancellationTokenSource.CreateLinkedTokenSource(stopping.Token))
        {
            lifetime.CancelAfter(TimeSpan.FromSeconds(10));
            try{
                client.NoDelay=true;var stream=client.GetStream();
                var bytes=new byte[8192];var used=0;var complete=false;
                while(used<bytes.Length){
                    var n=await stream.ReadAsync(bytes.AsMemory(used),lifetime.Token).ConfigureAwait(false);
                    if(n==0)return;used+=n;
                    if(used>=4&&Encoding.ASCII.GetString(bytes,0,used).Contains("\r\n\r\n",StringComparison.Ordinal)){complete=true;break;}
                }
                if(!complete){await Respond(stream,431,"请求过长。",null,false,lifetime.Token);return;}
                var first=Encoding.ASCII.GetString(bytes,0,used).Split("\r\n",StringSplitOptions.None)[0].Split(' ');
                if(first.Length!=3||!first[2].StartsWith("HTTP/1.",StringComparison.Ordinal)){await Respond(stream,400,"请求无效。",null,false,lifetime.Token);return;}
                var head=first[0]=="HEAD";
                if(first[0]!="GET"&&!head){await Respond(stream,405,"仅支持打开充值页面。",null,false,lifetime.Token);return;}
                var path=first[1].Split('?',2)[0];
                if(path!="/recharge"){await Respond(stream,404,"页面不存在。",null,head,lifetime.Token);return;}
                Uri? address;
                try{address=await resolve(lifetime.Token).ConfigureAwait(false);}
                catch(Exception error) when(error is not OperationCanceledException){Report("lookup failed "+error.GetType().Name);await Respond(stream,503,"充值入口暂时不可用，请稍后重试。",null,head,lifetime.Token);return;}
                if(address is null){await Respond(stream,503,"充值暂未开放，请联系管理员。",null,head,lifetime.Token);return;}
                if(!address.IsAbsoluteUri||(address.Scheme!="http"&&address.Scheme!="https")||address.Host.Length==0||address.UserInfo.Length!=0||address.AbsoluteUri.Length>2048||address.OriginalString.IndexOfAny(['\r','\n','\0'])>=0){await Respond(stream,503,"充值网址无效，请联系管理员。",null,head,lifetime.Token);return;}
                var normalized=new UriBuilder(address){Host=address.IdnHost}.Uri.GetComponents(UriComponents.AbsoluteUri,UriFormat.UriEscaped);
                await Respond(stream,302,"正在打开充值页面。",normalized,head,lifetime.Token);
            }catch(OperationCanceledException){}catch(IOException){}catch(SocketException){}catch(ObjectDisposedException){}
            catch(Exception error){Report("request failed "+error.GetType().Name);}
            finally{connections.TryRemove(client,out _);}
        }
    }
    private static async Task Respond(NetworkStream stream,int status,string message,string? location,bool head,CancellationToken token)
    {
        var body=Encoding.UTF8.GetBytes("<!doctype html><meta charset=utf-8><title>充值入口</title><h2>"+WebUtility.HtmlEncode(message)+"</h2>");
        var reason=status==302?"Found":status==503?"Service Unavailable":status==404?"Not Found":status==405?"Method Not Allowed":status==431?"Request Header Fields Too Large":"Bad Request";
        var headers=$"HTTP/1.1 {status} {reason}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {body.Length}\r\nConnection: close\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nX-Content-Type-Options: nosniff\r\nContent-Security-Policy: default-src 'none'\r\n";
        if(location is not null)headers+="Location: "+location+"\r\n";
        if(status==405)headers+="Allow: GET, HEAD\r\n";
        await stream.WriteAsync(Encoding.ASCII.GetBytes(headers+"\r\n"),token).ConfigureAwait(false);
        if(!head)await stream.WriteAsync(body,token).ConfigureAwait(false);
    }
    public void Dispose()
    {
        if(Interlocked.Exchange(ref disposed,1)!=0)return;
        stopping.Cancel();listener.Stop();foreach(var client in connections.Keys)client.Dispose();
    }
}
#endif
