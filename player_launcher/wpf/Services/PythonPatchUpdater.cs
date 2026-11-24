using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace DNFLogin.Services;

public sealed record PythonPatch(string FileName,string Sha256,long Bytes);
public sealed record PythonPatchManifest(long Revision,PythonPatch[] Items)
{ public JsonElement? Signed {get;init;} }

public static class PythonPatchUpdater
{
    public static PythonPatchManifest Parse(JsonElement stage)
    {
        if(!stage.TryGetProperty("client_patches",out var document))return new(0,[]);
        try
        {
            var revision=document.GetProperty("revision").GetInt64();
            var rows=document.GetProperty("items");
            if(revision<0||rows.ValueKind!=JsonValueKind.Array||rows.GetArrayLength()>50)throw new InvalidOperationException();
            var items=new List<PythonPatch>();var names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach(var row in rows.EnumerateArray())
            {
                var name=row.GetProperty("file_name").GetString()??"";var hash=row.GetProperty("sha256").GetString()??"";var size=row.GetProperty("bytes").GetInt64();
                if(!Regex.IsMatch(name,@"\A[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,95}\.dll\z",RegexOptions.IgnoreCase)||Regex.IsMatch(name,@"\A(con|prn|aux|nul|com[0-9]|lpt[0-9])\.",RegexOptions.IgnoreCase)||!Regex.IsMatch(hash,@"\A[0-9a-f]{64}\z")||size is <256 or >786432||!names.Add(name)||!row.GetProperty("enabled").GetBoolean()||row.GetProperty("on_mismatch").GetString()!="block")throw new InvalidOperationException();
                items.Add(new(name,hash,size));
            }
            if(revision==0&&items.Count!=0)throw new InvalidOperationException();
            return new(revision,items.ToArray()){Signed=document.TryGetProperty("signed",out var signed)?signed.Clone():null};
        }
        catch(Exception error) when(error is JsonException or InvalidOperationException or KeyNotFoundException or FormatException or OverflowException){throw new InvalidOperationException("服务器补丁清单格式无效，未更新客户端。");}
    }

#if PYTHON90
    private static void NoLinks(string path)
    {
        for(var p=Path.GetFullPath(path);!string.IsNullOrEmpty(p);p=Path.GetDirectoryName(p))
            if((File.Exists(p)||Directory.Exists(p))&&(File.GetAttributes(p)&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException("补丁目录包含重解析链接，未更新。");
    }
    private static string Hash(byte[] data)=>Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();
    private static void Durable(string path,byte[] bytes){using var file=new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.None);file.Write(bytes);file.Flush(true);}
    public static async Task ApplyAsync(PythonLauncherApi api,string root,PythonPatchManifest manifest,Action<string>? progress=null)
    {
        if(manifest.Signed is not JsonElement envelope)throw new InvalidOperationException("服务器补丁缺少数字签名，未安装。");
        var verified=PythonUpdateTrust.Verify(envelope,api.UpdateCertificatePem,"patches");
        PythonUpdateTrust.Fields(verified,"revision","items");
        var clean=Parse(JsonSerializer.SerializeToElement(new{client_patches=verified}));
        if(clean.Revision!=manifest.Revision||!clean.Items.SequenceEqual(manifest.Items))throw new InvalidOperationException("补丁清单与数字签名不一致。");
        await ApplyCoreAsync(root,manifest,api.PatchFileAsync,()=>PythonResourceUpdater.ClientIsRunning(root),PythonResourceUpdater.CacheDirectory,progress);
    }
    public static async Task ApplyCoreAsync(string root,PythonPatchManifest manifest,Func<string,Task<JsonElement>> download,Func<bool> running,string cache,Action<string>? progress=null)
    {
        manifest=Parse(JsonSerializer.SerializeToElement(new{client_patches=new{revision=manifest.Revision,items=manifest.Items.Select(p=>new{file_name=p.FileName,sha256=p.Sha256,bytes=p.Bytes,enabled=true,on_mismatch="block"})}}));
        root=Path.GetFullPath(root);cache=Path.GetFullPath(cache);NoLinks(root);NoLinks(cache);
        if(manifest.Items.Length==0)return;
        string Target(PythonPatch p)=>Path.Combine(root,"us_extend_dll",p.FileName);
        bool Matches(PythonPatch p){var path=Target(p);NoLinks(path);return File.Exists(path)&&new FileInfo(path).Length==p.Bytes&&PythonClientLauncher.Sha256(path)==p.Sha256;}
        if(manifest.Items.All(Matches))return;
        if(running())throw new InvalidOperationException("有游戏正在使用此客户端，请完全退出游戏后更新补丁。");
        using(var gate=PythonUpdateTransaction.Lock(root,cache))
        {
            PythonUpdateTransaction.RecoverLocked(root,cache);
            var pending=manifest.Items.Where(p=>!Matches(p)).ToArray();if(pending.Length==0)return;
            var job=Path.Combine(cache,"patch-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(job);
            var staged=new Dictionary<string,string>(StringComparer.OrdinalIgnoreCase);
            foreach(var p in pending)
            {
                progress?.Invoke("正在下载补丁："+p.FileName);
                var reply=await download(p.Sha256);
                var encoded=reply.GetProperty("content_base64").GetString()??"";
                if(encoded.Length>1048580)throw new InvalidOperationException("补丁下载大小超过限制。");
                byte[] bytes;
                try{bytes=Convert.FromBase64String(encoded);}catch(FormatException){throw new InvalidOperationException("补丁下载内容无效。");}
                if(bytes.Length!=p.Bytes||Hash(bytes)!=p.Sha256||reply.GetProperty("sha256").GetString()!=p.Sha256||reply.GetProperty("bytes").GetInt64()!=p.Bytes)throw new InvalidOperationException("补丁下载校验失败："+p.FileName);
                var path=Target(p);NoLinks(path);
                if(File.Exists(path)&&new FileInfo(path).Length>786432)throw new InvalidOperationException("现有补丁大小异常："+p.FileName);
                var file=Path.Combine(job,p.FileName+".new");Durable(file,bytes);
                staged.Add("us_extend_dll/"+p.FileName,file);
            }
            if(running())throw new InvalidOperationException("游戏已启动，本次补丁尚未安装；请退出后重试。");
            PythonUpdateTransaction.ApplyLocked(root,cache,staged,()=>{
                if(!manifest.Items.All(Matches))throw new InvalidOperationException("安装后补丁校验失败。");
            },running);
            progress?.Invoke("客户端补丁已更新并通过校验。");
        }
    }
#endif
}
