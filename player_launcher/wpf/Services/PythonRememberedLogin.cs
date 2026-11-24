#if PYTHON90
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace DNFLogin.Services;

public sealed record PythonSavedLogin(int Slot,string Account,string Password)
{ public override string ToString()=>"PythonSavedLogin (credentials redacted)"; }

// Portable, per-server storage. DPAPI binds the encrypted payload to this Windows user.
// The application never puts passwords in config.json or Windows Credential Manager.
public sealed class PythonRememberedLogin(string directory,string scope)
{
    private string FilePath => Path.Combine(directory,"remembered-login.bin");
    private byte[] Entropy => SHA256.HashData(Encoding.UTF8.GetBytes("PY90-remember-v1|"+scope));
    [StructLayout(LayoutKind.Sequential)] private struct Blob { public int Size; public IntPtr Data; }
    [DllImport("crypt32.dll",SetLastError=true,CharSet=CharSet.Unicode)]
    private static extern bool CryptProtectData(ref Blob input,string? description,ref Blob entropy,IntPtr reserved,IntPtr prompt,uint flags,out Blob output);
    [DllImport("crypt32.dll",SetLastError=true,CharSet=CharSet.Unicode)]
    private static extern bool CryptUnprotectData(ref Blob input,IntPtr description,ref Blob entropy,IntPtr reserved,IntPtr prompt,uint flags,out Blob output);
    [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);

    private static byte[] Transform(byte[] data,byte[] entropy,bool protect)
    {
        var input=new Blob { Size=data.Length,Data=Marshal.AllocHGlobal(data.Length) };
        var salt=new Blob { Size=entropy.Length,Data=Marshal.AllocHGlobal(entropy.Length) };
        Blob output=default;
        try
        {
            Marshal.Copy(data,0,input.Data,data.Length);Marshal.Copy(entropy,0,salt.Data,entropy.Length);
            var ok=protect?CryptProtectData(ref input,null,ref salt,IntPtr.Zero,IntPtr.Zero,1,out output):CryptUnprotectData(ref input,IntPtr.Zero,ref salt,IntPtr.Zero,IntPtr.Zero,1,out output);
            if(!ok)throw new CryptographicException("无法访问此 Windows 用户保存的登录信息。");
            var result=new byte[output.Size];Marshal.Copy(output.Data,result,0,result.Length);return result;
        }
        finally
        {
            for(var i=0;i<input.Size;i++)Marshal.WriteByte(input.Data,i,0);
            Marshal.FreeHGlobal(input.Data);Marshal.FreeHGlobal(salt.Data);
            if(output.Data!=IntPtr.Zero){for(var i=0;i<output.Size;i++)Marshal.WriteByte(output.Data,i,0);LocalFree(output.Data);}
        }
    }
    public IReadOnlyList<PythonSavedLogin> Read()
    {
        if(!File.Exists(FilePath))return [];
        if(new FileInfo(FilePath).Length>65536)throw new InvalidOperationException("保存的登录信息格式无效，请清除后重新登录。");
        var plain=Transform(File.ReadAllBytes(FilePath),Entropy,false);
        try
        {
            var items=JsonSerializer.Deserialize<PythonSavedLogin[]>(plain)??[];
            if(items.Length>2||items.Select(v=>v.Slot).Distinct().Count()!=items.Length||items.Any(v=>v is null||v.Password is null||v.Slot is <0 or >1||string.IsNullOrWhiteSpace(v.Account)||v.Account.Length>64||v.Password.Length is <1 or >128))throw new InvalidOperationException("保存的登录信息格式无效。");
            return items.Where(v => v.Slot == GameProcessManager.MainSlot).ToArray();
        }
        finally { CryptographicOperations.ZeroMemory(plain); }
    }
    public void Save(int slot,string account,string password)
    {
        if(slot != GameProcessManager.MainSlot||string.IsNullOrWhiteSpace(account)||account.Length>64||password.Length is <1 or >128)throw new ArgumentException("账号信息无效。");
        Write([new PythonSavedLogin(slot,account,password)]);
    }
    private void Write(PythonSavedLogin[] items)
    {
        var plain=JsonSerializer.SerializeToUtf8Bytes(items);byte[] encrypted;
        try { encrypted=Transform(plain,Entropy,true); } finally { CryptographicOperations.ZeroMemory(plain); }
        Directory.CreateDirectory(directory);
        var temp=Path.Combine(directory,".remember-"+Guid.NewGuid().ToString("N"));
        try
        {
            using(var output=new FileStream(temp,FileMode.CreateNew,FileAccess.Write,FileShare.None)){output.Write(encrypted);output.Flush(true);}
            File.Move(temp,FilePath,true);
        }
        finally { if(File.Exists(temp))File.Delete(temp); }
    }
    public void Forget(string account) {
        var items=Read().Where(v=>!string.Equals(v.Account,account,StringComparison.OrdinalIgnoreCase)).ToArray();
        if(items.Length==0)Clear();else Write(items);
    }
    public void Clear() { if(File.Exists(FilePath))File.Delete(FilePath); }
    public static PythonRememberedLogin Current()
    {
        var package=LauncherPackage.Current??throw new InvalidOperationException("服务器配置尚未加载。");
        var root = LauncherPackage.ConfigurationDirectory;
        var url = package.LoginUrl.AbsoluteUri;
        var endpoint = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(url)));
        var current = new PythonRememberedLogin(Path.Combine(root,endpoint),package.ServerId.ToLowerInvariant()+"|"+url);
        if (!File.Exists(current.FilePath))
        {
            var legacy = new PythonRememberedLogin(root,package.ServerId+"|"+url);
            if (File.Exists(legacy.FilePath))
            {
                try {
                    var entries = legacy.Read().ToArray();
                    if (entries.Length > 0) { current.Write(entries); legacy.Clear(); }
                } catch (CryptographicException) { /* Saved for a different endpoint. */ }
            }
        }
        return current;
    }
}
#endif
