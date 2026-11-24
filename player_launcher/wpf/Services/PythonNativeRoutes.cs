#if PYTHON90
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using Microsoft.Win32;

namespace DNFLogin.Services;

internal static class PythonTcpOwner
{
    [StructLayout(LayoutKind.Sequential)]
    private struct BasicProcess {internal IntPtr ExitStatus,Peb,Affinity,Priority,Pid,Parent;}
    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(IntPtr handle,int informationClass,out BasicProcess information,int size,out int returned);
    internal static int Parent(Process process)=>NtQueryInformationProcess(process.Handle,0,out var info,Marshal.SizeOf<BasicProcess>(),out _)==0?checked((int)info.Parent.ToInt64()):0;

    [DllImport("iphlpapi.dll",SetLastError=true)]
    private static extern uint GetExtendedTcpTable(IntPtr table,ref int size,bool order,int family,int tableClass,uint reserved);
    internal static int Find(int localPort,int remotePort)
    {
        var size=0;GetExtendedTcpTable(IntPtr.Zero,ref size,false,2,5,0);
        if(size<4||size>16*1024*1024)return 0;
        var memory=Marshal.AllocHGlobal(size);
        try
        {
            if(GetExtendedTcpTable(memory,ref size,false,2,5,0)!=0)return 0;
            var count=Marshal.ReadInt32(memory);
            if(count<0||count>(size-4)/24)return 0;
            for(var i=0;i<count;i++)
            {
                var row=IntPtr.Add(memory,4+i*24);
                int Port(int offset)=>(Marshal.ReadByte(row,offset)<<8)|Marshal.ReadByte(row,offset+1);
                if(Port(8)==localPort&&Port(16)==remotePort&&Marshal.ReadInt32(row,12)==0x0100007f)
                    return Marshal.ReadInt32(row,20);
            }
            return 0;
        }
        finally{Marshal.FreeHGlobal(memory);}
    }
}

// Each native connection is identified by its actual owning game PID. Tokens
// never depend on the last account to log in or on the shared public IP.
internal sealed class PythonNativeRoutePool(Action<string>? report=null)
{
    private readonly object gate=new();
    private readonly List<PythonChannelRelay> relays=[];
    private readonly List<GameLease> games=[];
    internal static readonly SemaphoreSlim LaunchGate=new(1,1);

    internal GameLease Acquire(PythonClientPlan plan,PythonSession session)
    {
        var address=IPAddress.Parse(session.Stage.ChannelHost);
        if(IPAddress.IsLoopback(address))throw new InvalidOperationException("游戏连接需要独立的远程服务器地址；本地服务端占用相同端口。");
        var ports=session.Stage.GamePorts.Prepend(7001).Distinct().OrderBy(value=>value).ToArray();
        lock(gate)
        {
            if (games.Count != 0) throw new InvalidOperationException("当前游戏仍在运行，请先退出再登录。");
            try
            {
                foreach(var port in ports)relays.Add(new PythonChannelRelay(address,port,port,report,Resolve));
            }
            catch
            {
                foreach(var relay in relays)relay.Dispose();relays.Clear();
                throw new InvalidOperationException("本机游戏端口被占用，请退出其他登录器或本地服务端。");
            }
            var lease=new GameLease(this,plan.Exe,new PythonNativeCredential(session.SessionId,session.AccessToken));games.Add(lease);return lease;
        }
    }
    private PythonNativeCredential? Resolve(TcpClient client)
    {
        if(client.Client.RemoteEndPoint is not IPEndPoint peer||client.Client.LocalEndPoint is not IPEndPoint local)return null;
        var pid=PythonTcpOwner.Find(peer.Port,local.Port);
        if(pid<=0)return null;
        lock(gate)
        {
            var lease=games.SingleOrDefault(g=>g.Pid==pid);
            if(lease is not null)return lease.Credential;
            var pending=games.SingleOrDefault(g=>g.Pid==0);
            if(pending is null)return null;
            try
            {
                using var process=Process.GetProcessById(pid);
                for(var attempt=0;pending.InjectorPid==0&&attempt<100;attempt++)Thread.Sleep(1);
                if(pending.InjectorPid==0||PythonTcpOwner.Parent(process)!=pending.InjectorPid)return null;
                if(!string.Equals(process.MainModule?.FileName,pending.Exe,StringComparison.OrdinalIgnoreCase)||process.StartTime.ToUniversalTime()<pending.Created.AddSeconds(-1))return null;
                pending.Pid=pid;return pending.Credential;
            }
            catch{return null;}
        }
    }
    internal sealed class GameLease:IDisposable
    {
        private readonly PythonNativeRoutePool pool;
        private readonly CancellationTokenSource ended=new();
        internal readonly string Exe;
        internal readonly PythonNativeCredential Credential;
        internal GameLease(PythonNativeRoutePool pool,string exe,PythonNativeCredential credential)
        {
            this.pool=pool;Exe=exe;Credential=credential with{Lifetime=ended.Token};
        }
        internal readonly DateTime Created=DateTime.UtcNow;
        internal int Pid;
        internal volatile int InjectorPid;
        internal void Bind(int pid)
        {
            lock(pool.gate)
            {
                if(Pid!=0&&Pid!=pid)throw new InvalidOperationException("游戏连接进程校验不一致。");
                Pid=pid;
            }
        }
        public void Dispose()
        {
            lock(pool.gate)
            {
                if(!pool.games.Remove(this))return;
                // A shared listener can stay up for the other game. End only
                // this game's authenticated streams when its process exits.
                ended.Cancel();ended.Dispose();
                if(pool.games.Count==0){foreach(var relay in pool.relays)relay.Dispose();pool.relays.Clear();}
            }
        }
    }
}
#endif
