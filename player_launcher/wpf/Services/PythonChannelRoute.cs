#if PYTHON90
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;

namespace DNFLogin.Services;
internal sealed record PythonNativeCredential(string SessionId,string AccessToken,CancellationToken Lifetime=default);

// The pinned US client obtains its channel list through 127.0.0.1:7001 even
// when its launch argument names the public host. Forward that channel stream
// unchanged; the server still authenticates the HTTP session and native wire.
internal sealed class PythonChannelRoutePool(Action<string>? report=null,int localPort=7001,int remotePort=7001)
{
    private readonly object gate=new();
    private PythonChannelRelay? relay;
    private int users;
    internal int LocalPort { get {lock(gate)return relay?.LocalPort??0;} }

    internal IDisposable Acquire(string host)
    {
        if(!IPAddress.TryParse(host,out var address)||address.AddressFamily!=AddressFamily.InterNetwork||address.ToString()!=host)
            throw new InvalidOperationException("频道 IPv4 地址无效。");
        // A local server already owns the frozen channel port; never relay it
        // back into this listener or take over somebody else's local server.
        if(IPAddress.IsLoopback(address)&&localPort==remotePort)return new Lease(()=>{});
        lock(gate)
        {
            if(relay is not null&&!relay.RemoteAddress.Equals(address))
                throw new InvalidOperationException("现有游戏正在连接其他服务器，请先退出游戏。");
            if(relay is null)
            {
                try {relay=new PythonChannelRelay(address,remotePort,localPort,report);}
                catch(SocketException error)
                {
                    throw new InvalidOperationException("本机频道端口 7001 无法使用，请先退出其他本地服务端或登录器。",error);
                }
            }
            users++;
            return new Lease(Release);
        }
    }

    private void Release()
    {
        lock(gate)
        {
            if(--users!=0)return;
            relay?.Dispose();relay=null;
        }
    }

    private sealed class Lease(Action release):IDisposable
    {
        private Action? releaseOnce=release;
        public void Dispose()=>Interlocked.Exchange(ref releaseOnce,null)?.Invoke();
    }
}

internal sealed class PythonChannelRelay:IDisposable
{
    private readonly TcpListener listener;
    private readonly CancellationTokenSource stopping=new();
    private readonly CancellationToken token;
    private readonly ConcurrentDictionary<TcpClient,byte> connections=new();
    private readonly Action<string>? report;
    private readonly int remotePort;
    private readonly Func<TcpClient,PythonNativeCredential?>? authenticate;
    private int disposed;
    internal IPAddress RemoteAddress {get;}
    internal int LocalPort {get;}

    internal PythonChannelRelay(IPAddress remote,int remotePort,int localPort,Action<string>? report=null,Func<TcpClient,PythonNativeCredential?>? authenticate=null)
    {
        RemoteAddress=remote;this.authenticate=authenticate;this.remotePort=remotePort;this.report=report;token=stopping.Token;
        listener=new TcpListener(IPAddress.Loopback,localPort);
        listener.Server.ExclusiveAddressUse=true;
        try {listener.Start();}
        catch {listener.Stop();stopping.Dispose();throw;}
        LocalPort=((IPEndPoint)listener.LocalEndpoint).Port;
        Report($"listening 127.0.0.1:{LocalPort} target={remote}:{remotePort}");
        _=AcceptAsync();
    }

    private void Report(string text){try{report?.Invoke(text);}catch{/* Diagnostics do not affect transport. */}}

    private async Task AcceptAsync()
    {
        try
        {
            while(!token.IsCancellationRequested)
            {
                var local=await listener.AcceptTcpClientAsync(token).ConfigureAwait(false);
                if(token.IsCancellationRequested||connections.Count>=32){local.Dispose();continue;}
                connections.TryAdd(local,0);
                _=HandleAsync(local);
            }
        }
        catch(OperationCanceledException){}
        catch(ObjectDisposedException){}
        catch(SocketException error){if(!token.IsCancellationRequested)Report($"listener error={error.SocketErrorCode}");}
    }

    private async Task HandleAsync(TcpClient local)
    {
        using(local)
        using(var remote=new TcpClient(AddressFamily.InterNetwork))
        using(var lifetime=CancellationTokenSource.CreateLinkedTokenSource(token))
        {
            CancellationTokenRegistration gameExited=default;
            try
            {
                local.NoDelay=remote.NoDelay=true;
                PythonNativeCredential? identity = null;
                if (authenticate is not null)
                {
                    // Resolve the local socket owner before the server starts its protocol timer.
                    // The lookup can block on a newly started game. Keep the accept loop responsive.
                    identity = await Task.Run(() => authenticate(local), lifetime.Token).ConfigureAwait(false)
                        ?? throw new UnauthorizedAccessException();
                    if (identity.SessionId.Length != 32) throw new UnauthorizedAccessException();
                    gameExited = identity.Lifetime.Register(() => lifetime.Cancel());
                    lifetime.Token.ThrowIfCancellationRequested();
                }
                using(var timeout=CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token))
                {
                    timeout.CancelAfter(TimeSpan.FromSeconds(5));
                    await remote.ConnectAsync(RemoteAddress,remotePort,timeout.Token).ConfigureAwait(false);
                }
                var localStream=local.GetStream();var remoteStream=remote.GetStream();
                if(identity is not null)
                {
                    var prefix=new byte[24];
                    System.Text.Encoding.ASCII.GetBytes("PY90NT2\n").CopyTo(prefix,0);
                    Convert.FromHexString(identity.SessionId).CopyTo(prefix,8);
                    using var authTimeout=CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
                    authTimeout.CancelAfter(TimeSpan.FromSeconds(5));
                    await remoteStream.WriteAsync(prefix,authTimeout.Token).ConfigureAwait(false);
                    var nonce=new byte[32];await remoteStream.ReadExactlyAsync(nonce,authTimeout.Token).ConfigureAwait(false);
                    var message=prefix.Concat(nonce).ToArray();
                    var bearer=System.Text.Encoding.ASCII.GetBytes(identity.AccessToken);
                    var key=System.Security.Cryptography.SHA256.HashData(bearer);
                    var proof=System.Security.Cryptography.HMACSHA256.HashData(key,message);
                    try{await remoteStream.WriteAsync(proof,authTimeout.Token).ConfigureAwait(false);}
                    finally
                    {
                        System.Security.Cryptography.CryptographicOperations.ZeroMemory(key);
                        System.Security.Cryptography.CryptographicOperations.ZeroMemory(bearer);
                        System.Security.Cryptography.CryptographicOperations.ZeroMemory(proof);
                    }
                }
                var up=CopyAsync(localStream,remoteStream,remote.Client,lifetime);
                var down=CopyAsync(remoteStream,localStream,local.Client,lifetime);
                await Task.WhenAll(up,down).ConfigureAwait(false);
            }
            catch(OperationCanceledException){}
            catch(ObjectDisposedException){}
            catch(SocketException error){Report($"connection error={error.SocketErrorCode}");}
            catch(UnauthorizedAccessException){Report("native connection owner rejected");}
            catch(IOException){Report("connection error=IOException");}
            finally{gameExited.Dispose();connections.TryRemove(local,out _);}
        }
    }

    private static async Task CopyAsync(NetworkStream input,NetworkStream output,Socket destination,CancellationTokenSource lifetime)
    {
        try
        {
            var buffer=new byte[16384];
            while(true)
            {
                var count=await input.ReadAsync(buffer,lifetime.Token).ConfigureAwait(false);
                if(count==0)
                {
                    // Preserve a TCP half-close: the other side can still send
                    // a delayed response after receiving the complete request.
                    try{destination.Shutdown(SocketShutdown.Send);}catch(SocketException){}
                    return;
                }
                await output.WriteAsync(buffer.AsMemory(0,count),lifetime.Token).ConfigureAwait(false);
            }
        }
        catch {lifetime.Cancel();throw;}
    }

    public void Dispose()
    {
        if(Interlocked.Exchange(ref disposed,1)!=0)return;
        stopping.Cancel();listener.Stop();
        foreach(var local in connections.Keys)local.Dispose();
        stopping.Dispose();Report("listener stopped");
    }
}
#endif
