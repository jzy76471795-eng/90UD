using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace DNFLogin.Services;

// The per-client lock is shared by resource and DLL updates. A durable journal is
// committed before any replacement and recovered before checking client stamps.
public static class PythonUpdateTransaction
{
    public sealed record Entry(string Path,string? Before,string After);
    public sealed record Journal(int Version,string Root,string Job,Entry[] Files);
    private static string Key(string root)=>Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(System.IO.Path.GetFullPath(root).TrimEnd('\\').ToUpperInvariant())));
    private static string Pending(string root,string cache)=>System.IO.Path.Combine(cache,Key(root)+".transaction.json");
    public static FileStream Lock(string root,string cache)
    {
        PythonUpdateTrust.NoLinks(root);PythonUpdateTrust.NoLinks(cache);Directory.CreateDirectory(cache);
        var path=System.IO.Path.Combine(cache,Key(root)+".lock");PythonUpdateTrust.NoLinks(path);
        try{return new FileStream(path,FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None);}
        catch(IOException){throw new InvalidOperationException("此客户端正在更新，请等待另一个登录器完成。");}
    }
    public static void Durable(string path,byte[] bytes)
    {
        using var f=new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.None);f.Write(bytes);f.Flush(true);
    }
    private static void CopyDurable(string source,string destination)
    {
        using var input=File.OpenRead(source);using var output=new FileStream(destination,FileMode.CreateNew,FileAccess.Write,FileShare.None);
        input.CopyTo(output);output.Flush(true);
    }
    private static string? Current(string path)=>File.Exists(path)?PythonUpdateTrust.Hash(path):null;
    public static void Recover(string root,string cache)
    {
        using var gate=Lock(root,cache);RecoverLocked(root,cache);
    }
    public static void RecoverLocked(string root,string cache)
    {
        root=System.IO.Path.GetFullPath(root);cache=System.IO.Path.GetFullPath(cache);
        var pending=Pending(root,cache);PythonUpdateTrust.NoLinks(pending);
        if(!File.Exists(pending))return;
        if(new FileInfo(pending).Length>PythonUpdateTrust.MaxManifestBytes)throw new InvalidOperationException("更新恢复记录异常，请保留缓存并联系管理员。");
        var journal=JsonSerializer.Deserialize<Journal>(File.ReadAllBytes(pending))??throw new InvalidOperationException("更新恢复记录缺失。");
        if(journal.Version!=1||!string.Equals(root,journal.Root,StringComparison.OrdinalIgnoreCase)||!string.Equals(System.IO.Path.GetDirectoryName(journal.Job),cache,StringComparison.OrdinalIgnoreCase)||!System.IO.Path.GetFileName(journal.Job).StartsWith("transaction-",StringComparison.Ordinal)||journal.Files.Length>100000)
            throw new InvalidOperationException("更新恢复记录不匹配，未修改客户端。");
        PythonUpdateTrust.NoLinks(journal.Job);
        if(PythonResourceUpdater.ClientIsRunning(root))throw new InvalidOperationException("有未完成的更新，请先退出游戏后恢复。");
        var names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        // Validate every backup and target before restoring any file.
        foreach(var entry in journal.Files)
        {
            var target=PythonUpdateTrust.Within(root,entry.Path);PythonUpdateTrust.NoLinks(target);
            if(!names.Add(entry.Path)||!IsHash(entry.After)||(entry.Before is not null&&!IsHash(entry.Before)))throw new InvalidOperationException("更新恢复记录无效。");
            var current=Current(target);
            if(current!=entry.Before&&current!=entry.After)throw new InvalidOperationException("更新文件发生外部修改，已保留旧文件备份："+journal.Job);
            if(entry.Before is not null)
            {
                var backup=PythonUpdateTrust.Within(System.IO.Path.Combine(journal.Job,"previous"),entry.Path);PythonUpdateTrust.NoLinks(backup);
                if(Current(backup)!=entry.Before)throw new InvalidOperationException("更新备份校验失败，未继续写入："+journal.Job);
            }
        }
        foreach(var entry in journal.Files.Reverse())
        {
            var target=PythonUpdateTrust.Within(root,entry.Path);PythonUpdateTrust.NoLinks(target);
            var current=Current(target);if(current==entry.Before)continue;
            if(current!=entry.After)throw new InvalidOperationException("恢复期间文件发生变化，已保留备份。");
            if(entry.Before is null)File.Delete(target);
            else
            {
                var temporary=System.IO.Path.Combine(System.IO.Path.GetDirectoryName(target)!,".py90-restore-"+Guid.NewGuid().ToString("N"));
                try{CopyDurable(PythonUpdateTrust.Within(System.IO.Path.Combine(journal.Job,"previous"),entry.Path),temporary);File.Move(temporary,target,true);}
                finally{if(File.Exists(temporary))File.Delete(temporary);}
                if(Current(target)!=entry.Before)throw new InvalidOperationException("旧文件恢复校验失败，已保留备份。");
            }
        }
        File.Delete(pending);
    }
    private static bool IsHash(string v)=>v.Length==64&&v.All(c=>c is >= '0' and <= '9' or >= 'a' and <= 'f');
    public static PythonUpdateResult ApplyLocked(string root,string cache,IReadOnlyDictionary<string,string> files,Action validate,Func<bool> running,Action<int>? afterReplacement=null)
    {
        root=System.IO.Path.GetFullPath(root);cache=System.IO.Path.GetFullPath(cache);RecoverLocked(root,cache);
        if(running())throw new InvalidOperationException("请先退出游戏再更新。");
        var job=System.IO.Path.Combine(cache,"transaction-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(job);
        var entries=new List<Entry>();var seen=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach(var pair in files)
        {
            var target=PythonUpdateTrust.Within(root,pair.Key);PythonUpdateTrust.NoLinks(target);PythonUpdateTrust.NoLinks(pair.Value);
            if(!seen.Add(pair.Key)||Directory.Exists(target))throw new InvalidOperationException("更新目标重名或与目录冲突。");
            var before=Current(target);var after=PythonUpdateTrust.Hash(pair.Value);
            var staged=PythonUpdateTrust.Within(System.IO.Path.Combine(job,"new"),pair.Key);Directory.CreateDirectory(System.IO.Path.GetDirectoryName(staged)!);CopyDurable(pair.Value,staged);
            if(PythonUpdateTrust.Hash(staged)!=after)throw new InvalidOperationException("待安装文件发生变化。");
            if(before is not null)
            {
                var backup=PythonUpdateTrust.Within(System.IO.Path.Combine(job,"previous"),pair.Key);Directory.CreateDirectory(System.IO.Path.GetDirectoryName(backup)!);CopyDurable(target,backup);
                if(PythonUpdateTrust.Hash(backup)!=before)throw new InvalidOperationException("备份期间文件发生变化。");
            }
            entries.Add(new(pair.Key,before,after));
        }
        var pending=Pending(root,cache);var temporaryJournal=System.IO.Path.Combine(job,"journal.json");
        Durable(temporaryJournal,JsonSerializer.SerializeToUtf8Bytes(new Journal(1,root,job,entries.ToArray())));File.Move(temporaryJournal,pending);
        try
        {
            int index=0;
            foreach(var entry in entries)
            {
                if(running())throw new InvalidOperationException("游戏已启动，更新已停止。");
                var target=PythonUpdateTrust.Within(root,entry.Path);PythonUpdateTrust.NoLinks(target);
                if(Current(target)!=entry.Before)throw new InvalidOperationException("客户端文件已变化，未覆盖外部修改。");
                Directory.CreateDirectory(System.IO.Path.GetDirectoryName(target)!);PythonUpdateTrust.NoLinks(target);
                var temporary=System.IO.Path.Combine(System.IO.Path.GetDirectoryName(target)!,".py90-update-"+Guid.NewGuid().ToString("N"));
                try{CopyDurable(PythonUpdateTrust.Within(System.IO.Path.Combine(job,"new"),entry.Path),temporary);File.Move(temporary,target,entry.Before is not null);}
                finally{if(File.Exists(temporary))File.Delete(temporary);}
                if(Current(target)!=entry.After)throw new InvalidOperationException("安装后的文件校验失败。");
                afterReplacement?.Invoke(++index);
            }
            validate();
            File.Delete(pending); // Commit point: after all installed-file validation.
            return new(System.IO.Path.Combine(job,"previous"),entries.Count);
        }
        catch
        {
            RecoverLocked(root,cache);throw;
        }
    }
}
