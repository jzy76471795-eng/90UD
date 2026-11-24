using System.Buffers.Binary;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;

namespace DNFLogin.Services;

public sealed class PythonRelayAuthorization(string endpoint,JsonElement grant,string certificatePem)
{
    public string Endpoint {get;}=endpoint;
    public JsonElement Grant {get;}=grant;
    public string CertificatePem {get;}=certificatePem;
    public override string ToString()=>"PythonRelayAuthorization (credentials redacted)";
}

// The DLL pins the launcher's PID+creation time. Its private pipe outlives the
// network worker and returns an empty required view throughout recovery.
public sealed class PythonPartyRelay : IDisposable
{
    private Process? process;
    private readonly string executable,runtime,readyPath,caPath,imageHash;
    private readonly Func<Task<PythonRelayAuthorization>>? renew;
    private readonly SemaphoreSlim workerGate=new(1,1);
    private readonly CancellationTokenSource stopped=new();
    private readonly TaskCompletionSource bound=new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly long ownerCreated;
    private readonly int ownerPid;
    private int disposed,gamePid;
    private long gameCreated;
    private ulong exchangeSequence;
    private NamedPipeServerStream? nativePipe;
    private readonly object eventGate=new();
    private readonly NativeRelayProofRecovery proofRecovery=new();
    public string PipeName {get;}
    public int ProcessId=>process?.Id??0;
    public long Created=>process?.StartTime.ToUniversalTime().ToFileTimeUtc()??0;
    public int PipeOwnerId=>ownerPid;
    public long PipeOwnerCreated=>ownerCreated;

    private PythonPartyRelay(string executable,string runtime,Func<Task<PythonRelayAuthorization>>? renew)
    {
        this.executable=Path.GetFullPath(executable);this.runtime=Path.GetFullPath(runtime);this.renew=renew;
        readyPath=Path.Combine(runtime,"party-relay-ready.json");caPath=Path.Combine(runtime,"party-relay-ca.pem");
        using var parent=Process.GetCurrentProcess();ownerPid=parent.Id;ownerCreated=parent.StartTime.ToUniversalTime().ToFileTimeUtc();
        PipeName=@"\\.\pipe\90us-relay-"+Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        using var file=File.OpenRead(executable);imageHash=Convert.ToHexString(CompatHash.Stream(file));
    }
    private static async Task WriteAsync(Process child,object value,CancellationToken cancellation)
    {
        byte[] bytes=JsonSerializer.SerializeToUtf8Bytes(value);
        try{
            if(bytes.Length>32768)throw new InvalidOperationException("组队私有消息过大。");
            await child.StandardInput.BaseStream.WriteAsync(bytes,cancellation).ConfigureAwait(false);
            await child.StandardInput.BaseStream.WriteAsync(new byte[]{10},cancellation).ConfigureAwait(false);
            await child.StandardInput.BaseStream.FlushAsync(cancellation).ConfigureAwait(false);
        }finally{CryptographicOperations.ZeroMemory(bytes);}
    }
    private void RecordEvent(string kind,Process? child=null,string? detail=null,int knownPid=0)
    {
        // Bounded local diagnostics. Never record grants, frames, endpoints,
        // account data, or arbitrary exception messages.
        try{
            lock(eventGate){
                var directory=Path.GetDirectoryName(runtime)??runtime;
                Directory.CreateDirectory(directory);
                var path=Path.Combine(directory,"party-relay-events.jsonl");
                if(File.Exists(path)&&new FileInfo(path).Length>262144)
                    File.Move(path,path+".previous",true);
                int pid=knownPid;int? exit=null;
                try{if(child is not null){pid=child.Id;if(child.HasExited)exit=child.ExitCode;}}
                catch(InvalidOperationException){}
                File.AppendAllText(path,JsonSerializer.Serialize(new {at=DateTimeOffset.UtcNow,kind,pid,exit,detail})+Environment.NewLine);
            }
        }catch(IOException){}catch(UnauthorizedAccessException){}catch(InvalidOperationException){}
    }
    private async Task RecordWorkerErrorsAsync(Process child)
    {
        int pid=child.Id;
        try{
            while(!stopped.IsCancellationRequested){
                var line=await BoundedLineAsync(child.StandardError,stopped.Token).ConfigureAwait(false);
                const string prefix="edge stopped code=";
                if(line.StartsWith(prefix,StringComparison.Ordinal)){
                    var code=line[prefix.Length..];
                    if(code.Length is >0 and <=48&&code.All(c=>c is >= 'a' and <= 'z'||c=='_'))
                        RecordEvent("worker_fault",detail:code,knownPid:pid);
                }
            }
        }catch(IOException){}catch(OperationCanceledException){}catch(ObjectDisposedException){}catch(InvalidOperationException){}
    }
    private void StopWorker(string reason="worker_replaced")
    {
        var child=process;process=null;exchangeSequence=0;
        if(child is null)return;
        RecordEvent(reason,child);
        try{child.StandardInput.Close();if(!child.WaitForExit(100)){child.Kill();child.WaitForExit(1000);}}
        catch(InvalidOperationException){}catch(System.ComponentModel.Win32Exception){}catch(IOException){}
        finally{child.Dispose();}
        try{File.Delete(readyPath);}catch(IOException){}catch(UnauthorizedAccessException){}
    }
    private async Task StartWorkerAsync(PythonRelayAuthorization authorization)
    {
        if(string.IsNullOrWhiteSpace(authorization.CertificatePem))throw new InvalidOperationException("组队中继缺少可信服务器证书。");
        StopWorker();
        using var image=new FileStream(executable,FileMode.Open,FileAccess.Read,FileShare.Read);
        if(Convert.ToHexString(CompatHash.Stream(image))!=imageHash)throw new InvalidOperationException("组队中继程序已改变。");
        await File.WriteAllTextAsync(caPath,authorization.CertificatePem,stopped.Token).ConfigureAwait(false);
        var info=new ProcessStartInfo(executable){UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=runtime,RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true};
        info.ArgumentList.Add("-mode");info.ArgumentList.Add("edge");info.ArgumentList.Add("-ready");info.ArgumentList.Add(readyPath);
        foreach(var name in new[]{"TEMP","TMP","TMPDIR"})info.Environment[name]=runtime;
        foreach(var name in info.Environment.Keys.ToArray())if(name.StartsWith("USDOF_",StringComparison.OrdinalIgnoreCase)||name.StartsWith("DNF_PYTHON_",StringComparison.OrdinalIgnoreCase))info.Environment.Remove(name);
        var child=Process.Start(info)??throw new InvalidOperationException("组队中继程序未启动。");process=child;RecordEvent("worker_started",child);
        _=RecordWorkerErrorsAsync(child);
        try{
            await WriteAsync(child,new {grant=authorization.Grant,relay_url=authorization.Endpoint,ca_file=caPath,pipe_name=PipeName,parent_pid=ownerPid,parent_created=ownerCreated,parent_native_ipc=true},stopped.Token).ConfigureAwait(false);
            var timer=Stopwatch.StartNew();
            while(timer.Elapsed<TimeSpan.FromSeconds(8)){
                stopped.Token.ThrowIfCancellationRequested();
                if(child.HasExited)throw new IOException("组队中继连接暂不可用。");
                try{
                    using var doc=JsonDocument.Parse(await File.ReadAllTextAsync(readyPath,stopped.Token).ConfigureAwait(false));var value=doc.RootElement;
                    if(value.GetProperty("pid").GetInt32()!=child.Id||value.GetProperty("role").GetString()!="edge"||!value.GetProperty("native_pipe_ready").GetBoolean()||!value.GetProperty("parent_native_ipc").GetBoolean())throw new InvalidOperationException("组队中继启动身份不一致。");
                    if(gamePid!=0)await WriteAsync(child,new {game_pid=gamePid,game_created=gameCreated},stopped.Token).ConfigureAwait(false);
                    RecordEvent("worker_ready",child);return;
                }catch(FileNotFoundException){}catch(JsonException){}catch(IOException){}
                await Task.Delay(50,stopped.Token).ConfigureAwait(false);
            }
            throw new IOException("组队中继连接超时。");
        }catch{StopWorker();throw;}
    }
    public static async Task<PythonPartyRelay> StartAsync(PythonRelayAuthorization authorization,string executable,string runtime,Func<Task<PythonRelayAuthorization>>? renew=null)
    {
        var lease=new PythonPartyRelay(executable,runtime,renew);
        try{
            lease.nativePipe=lease.CreatePipe();
            await lease.StartWorkerAsync(authorization).ConfigureAwait(false);
            _=lease.ServeNativeAsync();_=lease.SuperviseAsync();return lease;
        }catch{lease.Dispose();throw;}
    }
    public async Task BindAsync(Process game)
    {
        await workerGate.WaitAsync(stopped.Token).ConfigureAwait(false);
        try{
            if(disposed!=0||game.HasExited||gamePid!=0){
                RecordEvent("bind_rejected",process,disposed!=0?"disposed":game.HasExited?"game_exited":"already_bound");
                throw new InvalidOperationException("组队中继绑定的游戏实例已失效。");
            }
            gamePid=game.Id;gameCreated=game.StartTime.ToUniversalTime().ToFileTimeUtc();
            bool restart=process is null||process.HasExited;
            if(!restart){
                try{await WriteAsync(process!,new {game_pid=gamePid,game_created=gameCreated},stopped.Token).ConfigureAwait(false);}
                catch(IOException){restart=true;}
                catch(InvalidOperationException){restart=true;}
            }
            if(restart){
                RecordEvent("prebind_worker_recovery",process);
                StopWorker("prebind_worker_retired");
                if(renew is null)throw new IOException("组队启动恢复需要新的认证授权。");
                var authorization=await renew().ConfigureAwait(false);
                stopped.Token.ThrowIfCancellationRequested();
                if(!GameAlive())throw new InvalidOperationException("组队恢复期间游戏实例已退出。");
                // The restarted worker receives this exact owned Game identity
                // in StartWorkerAsync; never replay the old single-use grant.
                await StartWorkerAsync(authorization).ConfigureAwait(false);
            }
            RecordEvent("game_bound",process);
            bound.TrySetResult();
        }finally{workerGate.Release();}
    }
    public void Configure(ProcessStartInfo injector)
    {
        if(disposed!=0)throw new InvalidOperationException("组队中继进程已失效。");
        injector.Environment["USDOF_RELAY_PIPE"]=PipeName;
        injector.Environment["USDOF_RELAY_OWNER_PID"]=ownerPid.ToString(CultureInfo.InvariantCulture);
        injector.Environment["USDOF_RELAY_OWNER_CREATED"]=ownerCreated.ToString(CultureInfo.InvariantCulture);
    }
    private bool GameAlive()
    {
        try{using var game=Process.GetProcessById(gamePid);return !game.HasExited&&game.StartTime.ToUniversalTime().ToFileTimeUtc()==gameCreated;}
        catch(ArgumentException){return false;}catch(InvalidOperationException){return false;}catch(System.ComponentModel.Win32Exception){return false;}
    }
    private async Task SuperviseAsync()
    {
        try{
            await bound.Task.WaitAsync(stopped.Token).ConfigureAwait(false);int retry=1;
            while(!stopped.IsCancellationRequested&&GameAlive()){
                await Task.Delay(TimeSpan.FromSeconds(retry),stopped.Token).ConfigureAwait(false);
                await workerGate.WaitAsync(stopped.Token).ConfigureAwait(false);
                try{
                    if(process is not null&&!process.HasExited){retry=1;continue;}
                    StopWorker("worker_exited");
                    if(renew is null){retry=15;continue;}
                    var authorization=await renew().ConfigureAwait(false);
                    stopped.Token.ThrowIfCancellationRequested();
                    await StartWorkerAsync(authorization).ConfigureAwait(false);retry=1;
                }catch(Exception)when(!stopped.IsCancellationRequested){StopWorker();retry=Math.Min(15,retry*2);}
                finally{workerGate.Release();}
            }
        }catch(OperationCanceledException){}
    }
    [StructLayout(LayoutKind.Sequential)]private struct SecurityAttributes{public int Length;public IntPtr Descriptor;public int Inherit;}
    [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]private static extern bool ConvertStringSecurityDescriptorToSecurityDescriptor(string text,uint revision,out IntPtr descriptor,out uint size);
    [DllImport("kernel32.dll")]private static extern IntPtr LocalFree(IntPtr memory);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]private static extern SafePipeHandle CreateNamedPipe(string name,uint open,uint mode,uint maximum,uint output,uint input,uint timeout,ref SecurityAttributes security);
    [DllImport("kernel32.dll",SetLastError=true)]private static extern bool GetNamedPipeClientProcessId(SafePipeHandle pipe,out uint pid);
    private NamedPipeServerStream CreatePipe()
    {
        if(!ConvertStringSecurityDescriptorToSecurityDescriptor("D:P(A;;GA;;;SY)(A;;GA;;;OW)",1,out var descriptor,out _))throw new IOException("组队私有管道权限失败。");
        try{
            var security=new SecurityAttributes{Length=Marshal.SizeOf<SecurityAttributes>(),Descriptor=descriptor};
            var handle=CreateNamedPipe(PipeName,3|0x40000000|0x80000,8,1,16384,16384,100,ref security);
            if(handle.IsInvalid){handle.Dispose();throw new IOException("组队私有管道创建失败。");}
            return new NamedPipeServerStream(PipeDirection.InOut,true,false,handle);
        }finally{LocalFree(descriptor);}
    }
    private static byte[] ClosedReply(byte[] frame)
    {
        var result=new byte[64];frame.AsSpan(0,24).CopyTo(result);BinaryPrimitives.WriteUInt16LittleEndian(result.AsSpan(6),2);BinaryPrimitives.WriteUInt32LittleEndian(result.AsSpan(8),40);
        var body=frame.AsSpan(24);body[..4].CopyTo(result.AsSpan(24));body.Slice(20,4).CopyTo(result.AsSpan(28));body.Slice(8,8).CopyTo(result.AsSpan(32));BinaryPrimitives.WriteUInt32LittleEndian(result.AsSpan(56),1);
        return result;
    }
    private static async Task<string> BoundedLineAsync(StreamReader reader,CancellationToken cancellation)
    {
        var text=new char[4096];int used=0;
        while(used<text.Length){if(await reader.ReadAsync(text.AsMemory(used,1),cancellation).ConfigureAwait(false)==0)throw new EndOfStreamException();if(text[used]=='\n')return new string(text,0,used);used++;}
        throw new IOException("组队私有响应过大。");
    }
    private async Task<(byte[] Frame,Process? Worker)> ExchangeAsync(byte[] frame,CancellationToken cancellation)
    {
        // A healthy supervisor briefly owns this gate too. Wait within the
        // caller's single frame deadline; a transient lock is not revocation.
        await workerGate.WaitAsync(cancellation).ConfigureAwait(false);
        Process? used=null;
        try{
            used=process;
            if(used is null||used.HasExited)return (ClosedReply(frame),used);
            var sequence=++exchangeSequence;
            await WriteAsync(process,new {sequence,native_frame=Convert.ToBase64String(frame)},cancellation).ConfigureAwait(false);
            using var reply=JsonDocument.Parse(await BoundedLineAsync(process.StandardOutput,cancellation).ConfigureAwait(false));
            if(reply.RootElement.GetProperty("sequence").GetUInt64()!=sequence)throw new IOException("组队私有响应序号错误。");
            var result=Convert.FromBase64String(reply.RootElement.GetProperty("native_frame").GetString()!);
            if(result.Length is <64 or >184||BinaryPrimitives.ReadUInt64LittleEndian(result.AsSpan(16))!=BinaryPrimitives.ReadUInt64LittleEndian(frame.AsSpan(16)))throw new IOException("组队私有响应长度错误。");
            return (result,used);
        }catch(Exception error)when(!stopped.IsCancellationRequested){RecordEvent("native_exchange_failed",process,error.GetType().Name);StopWorker("native_exchange_stop");return (ClosedReply(frame),used);}
        finally{workerGate.Release();}
    }
    private async Task ServeNativeAsync()
    {
        try{
            await bound.Task.WaitAsync(stopped.Token).ConfigureAwait(false);
            while(!stopped.IsCancellationRequested&&GameAlive()){
                NamedPipeServerStream? pipe=null;Process? pipeWorker=null;bool trustedGame=false,pipeHadConfirmed=false,recoverProof=false;
                try{
                    pipe=nativePipe??=CreatePipe();
                    await pipe.WaitForConnectionAsync(stopped.Token).ConfigureAwait(false);
                    if(!GetNamedPipeClientProcessId(pipe.SafePipeHandle,out var pid)||pid!=gamePid||!GameAlive())throw new IOException("组队管道客户端身份错误。");
                    trustedGame=true;pipeWorker=process;
                    ulong sequence=0;
                    while(!stopped.IsCancellationRequested){
                        var header=new byte[24];await pipe.ReadExactlyAsync(header,stopped.Token).ConfigureAwait(false);sequence++;
                        uint declared=BinaryPrimitives.ReadUInt32LittleEndian(header.AsSpan(8));
                        if(declared is <40 or >200)throw new IOException("组队管道长度无效。");
                        int size=(int)declared;
                        if(!header.AsSpan(0,4).SequenceEqual("RIPC"u8)||BinaryPrimitives.ReadUInt16LittleEndian(header.AsSpan(4))!=1||BinaryPrimitives.ReadUInt16LittleEndian(header.AsSpan(6))!=1||BinaryPrimitives.ReadUInt32LittleEndian(header.AsSpan(12))!=0||sequence==0||BinaryPrimitives.ReadUInt64LittleEndian(header.AsSpan(16))!=sequence||size is <40 or >200)throw new IOException("组队管道消息无效。");
                        var frame=new byte[24+size];header.CopyTo(frame,0);await pipe.ReadExactlyAsync(frame.AsMemory(24),stopped.Token).ConfigureAwait(false);
                        if(BinaryPrimitives.ReadUInt32LittleEndian(frame.AsSpan(24))!=gamePid||BinaryPrimitives.ReadInt64LittleEndian(frame.AsSpan(32))!=gameCreated)throw new IOException("组队管道游戏实例改变。");
                        int peers=BinaryPrimitives.ReadUInt16LittleEndian(frame.AsSpan(58));
                        if(peers>10||size!=40+16*peers)throw new IOException("组队原生快照座位长度无效。");
                        bool confirmed=false;
                        for(int i=0;i<peers;i++){
                            int offset=64+16*i;
                            uint state=BinaryPrimitives.ReadUInt32LittleEndian(frame.AsSpan(offset+8));
                            uint proof=BinaryPrimitives.ReadUInt32LittleEndian(frame.AsSpan(offset+12));
                            if(state>3||proof>1||proof==1&&state!=0)throw new IOException("组队原生快照确认无效。");
                            confirmed|=proof==1;
                        }
                        pipeHadConfirmed=confirmed;
                        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(stopped.Token);deadline.CancelAfter(250);
                        var result=await ExchangeAsync(frame,deadline.Token).ConfigureAwait(false);
                        if(result.Worker is not null)pipeWorker=result.Worker;
                        await pipe.WriteAsync(result.Frame,deadline.Token).ConfigureAwait(false);await pipe.FlushAsync(deadline.Token).ConfigureAwait(false);
                        if(renew is not null&&result.Worker is not null&&proofRecovery.Observe(frame,result.Frame,Environment.TickCount64)){
                            recoverProof=true;break;
                        }
                    }
                }catch(IOException){if(trustedGame)RecordEvent("native_pipe_closed",pipeWorker);}
                catch(OperationCanceledException)when(!stopped.IsCancellationRequested){RecordEvent("native_pipe_deadline",pipeWorker);}
                finally{
                    pipe?.Dispose();nativePipe=null;
                    // The DLL clears its real handshake evidence on pipe loss.
                    // Retire only the worker used by this authenticated pipe;
                    // a fresh authorization changes endpoints and causes Game
                    // to perform its actual handshake again. A late old-pipe
                    // callback cannot stop an already-replaced worker.
                    if(trustedGame&&(pipeHadConfirmed||recoverProof)&&pipeWorker is not null&&!stopped.IsCancellationRequested&&GameAlive()){
                        await workerGate.WaitAsync(stopped.Token).ConfigureAwait(false);
                        try{if(ReferenceEquals(process,pipeWorker))StopWorker(recoverProof?"native_proof_recovery":"native_pipe_lost");}
                        finally{workerGate.Release();}
                    }
                }
                await Task.Delay(50,stopped.Token).ConfigureAwait(false);
            }
        }catch(OperationCanceledException){}catch(ObjectDisposedException)when(stopped.IsCancellationRequested){}
    }
    public void Dispose()
    {
        if(Interlocked.Exchange(ref disposed,1)!=0)return;
        stopped.Cancel();nativePipe?.Dispose();
        if(workerGate.Wait(1000)){try{StopWorker();}finally{workerGate.Release();}}
        else _=Task.Run(async()=>{await workerGate.WaitAsync().ConfigureAwait(false);try{StopWorker();}finally{workerGate.Release();}});
    }
}
