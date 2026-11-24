#if PYTHON90
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using SharpCompress.Archives;

namespace DNFLogin.Services;

public sealed record PythonUpdateResult(string BackupDirectory,int InstalledFiles);

public static class PythonResourceUpdater
{
    private const long MaxDownload=32L*1024*1024*1024,MaxExpanded=64L*1024*1024*1024;
    public static string CacheDirectory => Directory.Exists(@"E:\codex") ? @"E:\codex\登录器缓存\updates" : Path.Combine(LauncherPackage.ConfigurationDirectory,"updates");
    public static Uri DownloadAddress(PythonPvfRelease release,bool hasValidExecutable)
    {
        var text=hasValidExecutable && !string.IsNullOrWhiteSpace(release.DownloadUrl) ? release.DownloadUrl : release.ClientDownloadUrl;
        if(string.IsNullOrWhiteSpace(text))throw new InvalidOperationException(hasValidExecutable ? "客户端需要更新，但 GM 尚未设置更新包直链。" : "客户端不完整，GM 尚未设置客户端整包直链。");
        return ValidateDownloadAddress(text);
    }
    public static Uri ValidateDownloadAddress(string text)=>PythonUpdateTrust.Https(text);
    private static string Hash(string path)=>PythonUpdateTrust.Hash(path);
    private static string Within(string root,string relative)=>PythonUpdateTrust.Within(root,relative);
    private static void NoLinks(string path)=>PythonUpdateTrust.NoLinks(path);
    public static bool ClientIsRunning(string root)
    {
        foreach(var process in Process.GetProcessesByName("DFO"))
        using(process)
        {
            try {if(string.Equals(Path.GetDirectoryName(process.MainModule?.FileName),Path.GetFullPath(root),StringComparison.OrdinalIgnoreCase))return true;}
            catch(System.ComponentModel.Win32Exception){return true;}
            catch(InvalidOperationException){ }
        }
        return false;
    }
    public static async Task<PythonUpdateResult> UpdateAsync(Uri url,string clientRoot,string expectedPvf,string expectedExe,string originalLauncher,
        Action validateInstalled,IProgress<string>? progress=null,CancellationToken cancellation=default,HttpClient? suppliedClient=null,string? cacheDirectory=null,IReadOnlySet<string>? preservedRelativePaths=null,string? trustedCertificatePem=null,string? expectedVersion=null)
    {
        url=ValidateDownloadAddress(url.AbsoluteUri);
        if(expectedPvf.Length!=64||!expectedPvf.All(Uri.IsHexDigit))throw new InvalidOperationException("服务器发布的 PVF SHA256 无效，未下载。");
        expectedPvf=expectedPvf.ToLowerInvariant();
        clientRoot=Path.GetFullPath(clientRoot);NoLinks(clientRoot);
        if(ClientIsRunning(clientRoot))throw new InvalidOperationException("请先退出游戏，再更新客户端。");
        var cache=Path.GetFullPath(cacheDirectory??CacheDirectory);
        using(var updateLock=PythonUpdateTransaction.Lock(clientRoot,cache))
        {
            PythonUpdateTransaction.RecoverLocked(clientRoot,cache);
            var job=Path.Combine(cache,Guid.NewGuid().ToString("N"));Directory.CreateDirectory(job);
            var package=Path.Combine(job,"download.pkg");var extracted=Path.Combine(job,"extracted");Directory.CreateDirectory(extracted);
            try {
            using var ownedClient=suppliedClient is null ? new HttpClient(new HttpClientHandler{SslProtocols=System.Security.Authentication.SslProtocols.None,UseProxy=false,UseCookies=false,AllowAutoRedirect=false}){Timeout=TimeSpan.FromMinutes(30)} : null;
            var http=suppliedClient??ownedClient!;
            progress?.Report("正在下载更新包…");
            using(var response=await PythonUpdateTrust.GetAsync(http,url,cancellation).ConfigureAwait(false))
            {
                if(!response.IsSuccessStatusCode)throw new InvalidOperationException($"更新包下载失败：HTTP {(int)response.StatusCode}。");
                var length=response.Content.Headers.ContentLength;
                if(length>MaxDownload)throw new InvalidOperationException("更新包超过允许大小。");
                await using var input=await response.Content.ReadAsStreamAsync(cancellation).ConfigureAwait(false);
                await using var output=new FileStream(package,FileMode.CreateNew,FileAccess.Write,FileShare.None,131072,true);
                var buffer=new byte[131072];long total=0;var last=DateTime.UtcNow;
                while(true)
                {
                    var read=await input.ReadAsync(buffer,cancellation).ConfigureAwait(false);if(read==0)break;
                    total+=read;if(total>MaxDownload)throw new InvalidOperationException("更新包超过允许大小。");
                    await output.WriteAsync(buffer.AsMemory(0,read),cancellation).ConfigureAwait(false);
                    if(DateTime.UtcNow-last>TimeSpan.FromMilliseconds(500)){progress?.Report(length>0?$"正在下载更新包… {total*100/length}%":"正在下载更新包…");last=DateTime.UtcNow;}
                }
                if(length.HasValue&&length.Value!=total)throw new InvalidOperationException("更新包下载不完整，原客户端未修改。");
                await output.FlushAsync(cancellation).ConfigureAwait(false);output.Flush(true);
            }
            progress?.Report("正在安装资源并校验 PVF…");
            return await Task.Run(()=>ExtractAndApply(package,extracted,clientRoot,expectedPvf,originalLauncher,validateInstalled,progress,cancellation,preservedRelativePaths,cache,expectedExe,trustedCertificatePem,expectedVersion),cancellation).ConfigureAwait(false);
            } finally {
                // The journal and rollback copies have their own directory.
                try { NoLinks(job); Directory.Delete(job,true); } catch(IOException) { } catch(UnauthorizedAccessException) { }
            }
        }
    }
    private static PythonUpdateResult ExtractAndApply(string package,string extracted,string root,string expectedPvf,string launcher,
        Action validateInstalled,IProgress<string>? progress,CancellationToken cancellation,IReadOnlySet<string>? preservedRelativePaths,string cache,string expectedExe,string? pem,string? version)
    {
        // The authenticated database-published encrypted PVF hash is the sole
        // game-file version gate. Accept a raw PVF or a normal resource archive.
        // Other resources are installed without comparing their release hashes.
        var seen=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        if(Hash(package)==expectedPvf)
        {
            File.Copy(package,Path.Combine(extracted,"Script.pvf"));
            seen.Add("Script.pvf");
        }
        else
        {
            using var source=File.OpenRead(package);using var archive=ArchiveFactory.OpenArchive(source);
            var entries=archive.Entries.Take(100002).ToArray();
            if(entries.Length>100001)throw new InvalidOperationException("更新包文件过多。");
            var manifests=entries.Where(e=>!e.IsDirectory && string.Equals(e.Key,PythonUpdateTrust.ManifestName,StringComparison.OrdinalIgnoreCase)).ToArray();
            if(manifests.Length!=1 || manifests[0].Size<=0 || manifests[0].Size>PythonUpdateTrust.MaxManifestBytes)
                throw new InvalidOperationException("资源包缺少有效的唯一签名清单。");
            using var manifestStream=manifests[0].OpenEntryStream();
            using var envelope=System.Text.Json.JsonDocument.Parse(manifestStream);
            var approved=PythonUpdateTrust.Verify(envelope.RootElement,pem??"","resources");
            if(approved.GetProperty("schema").GetInt32()!=1 || approved.GetProperty("version").GetString()!=version
                || approved.GetProperty("pvf_sha256").GetString()!=expectedPvf || approved.GetProperty("exe_sha256").GetString()!=expectedExe)
                throw new InvalidOperationException("资源签名与本次发布不一致。");
            var inventory=new Dictionary<string,(long Bytes,string Hash)>(StringComparer.OrdinalIgnoreCase);
            foreach(var record in approved.GetProperty("files").EnumerateArray())
            {
                var name=PythonUpdateTrust.Relative(record.GetProperty("path").GetString()??"");
                var size=record.GetProperty("bytes").GetInt64();var hash=record.GetProperty("sha256").GetString()??"";
                if(size<0||size>MaxExpanded||hash.Length!=64||!hash.All(Uri.IsHexDigit)||!inventory.TryAdd(name,(size,hash.ToLowerInvariant())))
                    throw new InvalidOperationException("资源签名文件表无效。");
            }
            long expanded=0;
            foreach(var entry in entries)
            {
                cancellation.ThrowIfCancellationRequested();
                var key=entry.Key??"";
                if(entry.IsDirectory){PythonUpdateTrust.Relative(key.TrimEnd('/'));continue;}
                // Historical signed inventories are not the PVF version source.
                if(key.Equals(PythonUpdateTrust.ManifestName,StringComparison.OrdinalIgnoreCase))continue;
                var destination=Within(extracted,key);
                if(!seen.Add(key)||entry.Size<0||entry.Size>MaxExpanded)
                    throw new InvalidOperationException("更新包包含重名文件或大小异常的文件，原客户端未修改。");
                Directory.CreateDirectory(Path.GetDirectoryName(destination)!);long written=0;
                using(var input=entry.OpenEntryStream())using(var output=new FileStream(destination,FileMode.CreateNew,FileAccess.Write,FileShare.None))
                {
                    var buffer=new byte[131072];int count;
                    while((count=input.Read(buffer))>0)
                    {
                        cancellation.ThrowIfCancellationRequested();written+=count;expanded+=count;
                        if(written>entry.Size||expanded>MaxExpanded)throw new InvalidOperationException("更新解压大小超过限制。");
                        output.Write(buffer,0,count);
                    }
                    output.Flush(true);
                }
                if(!inventory.Remove(key,out var expected)||expected.Bytes!=written||Hash(destination)!=expected.Hash)
                    throw new InvalidOperationException("资源与签名清单不一致："+key);
                if(written!=entry.Size)throw new InvalidOperationException("更新下载或解压不完整，原客户端未修改。");
            }
            if(inventory.Count!=0)throw new InvalidOperationException("更新包缺少签名文件。");
            var pvfPath=seen.SingleOrDefault(p=>p.Equals("Script.pvf",StringComparison.OrdinalIgnoreCase));
            if(pvfPath is null||Hash(Within(extracted,pvfPath))!=expectedPvf)
                throw new InvalidOperationException("更新包中的 Script.pvf 与数据库发布 SHA256 不一致，原客户端未修改。");
        }
        var staged=new Dictionary<string,string>(StringComparer.OrdinalIgnoreCase);
        foreach(var path in seen)
        {
            var destination=Within(root,path);
            if(preservedRelativePaths?.Contains(path)==true)continue;
            if(destination.Equals(Path.GetFullPath(launcher),StringComparison.OrdinalIgnoreCase)||new[]{"launcher.json","server-cert.pem","密钥与校验.json"}.Contains(Path.GetFileName(destination),StringComparer.OrdinalIgnoreCase))
                throw new InvalidOperationException("更新包不能覆盖当前登录器或连接配置。");
            staged.Add(path,Within(extracted,path));
        }
        cancellation.ThrowIfCancellationRequested();progress?.Report("PVF 校验通过，正在安装更新文件…");
        return PythonUpdateTransaction.ApplyLocked(root,cache,staged,()=>{
            validateInstalled();if(Hash(Path.Combine(root,"Script.pvf"))!=expectedPvf)throw new InvalidOperationException("安装后的 PVF 校验失败。");
        },()=>ClientIsRunning(root));
    }
    public static ProcessStartInfo RestartPlan(string originalLauncher,string root,int processId)
    {
        originalLauncher=Path.GetFullPath(originalLauncher);root=Path.GetFullPath(root);
        if(!File.Exists(originalLauncher)||!Path.GetDirectoryName(originalLauncher)!.Equals(root,StringComparison.OrdinalIgnoreCase))throw new InvalidOperationException("无法定位原始单文件登录器，请手动重新打开。");
        var script="$ErrorActionPreference='Stop';$taskWait=$null;try{$taskWait=[Diagnostics.Process]::GetProcessById([int]$env:PY90_RESTART_PID);if(-not $taskWait.WaitForExit(60000)){exit 1}}catch [ArgumentException]{}finally{if($taskWait){$taskWait.Dispose()}};Start-Process -FilePath $env:PY90_RESTART_EXE -WorkingDirectory $env:PY90_RESTART_ROOT";
        var start=new ProcessStartInfo("powershell.exe"){UseShellExecute=false,CreateNoWindow=true,WindowStyle=ProcessWindowStyle.Hidden};
        start.ArgumentList.Add("-NoProfile");start.ArgumentList.Add("-NonInteractive");start.ArgumentList.Add("-EncodedCommand");start.ArgumentList.Add(Convert.ToBase64String(Encoding.Unicode.GetBytes(script)));
        start.Environment["PY90_RESTART_PID"]=processId.ToString();start.Environment["PY90_RESTART_EXE"]=originalLauncher;start.Environment["PY90_RESTART_ROOT"]=root;
        return start;
    }
}
#endif
