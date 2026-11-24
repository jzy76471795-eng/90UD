using System.IO;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace DNFLogin.Services;

public static class PythonUpdateTrust
{
    public const string ManifestName=".90us-update.json";
    public const int MaxManifestBytes=16*1024*1024;
    public static string Hash(string path){using var f=File.OpenRead(path);return Convert.ToHexString(CompatHash.Stream(f)).ToLowerInvariant();}
    public static void Fields(JsonElement value,params string[] fields)
    {
        if(value.ValueKind!=JsonValueKind.Object)throw new InvalidOperationException("更新清单格式无效。");
        var names=value.EnumerateObject().Select(p=>p.Name).ToArray();
        if(names.Length!=fields.Length||!names.ToHashSet(StringComparer.Ordinal).SetEquals(fields))throw new InvalidOperationException("更新清单包含重复、缺失或未知字段。");
    }
    public static string Relative(string value)
    {
        if(string.IsNullOrEmpty(value)||value.Length>230||value.Contains('\\')||value.Any(c=>c<32||c==127)||Path.IsPathRooted(value))throw new InvalidOperationException("更新路径无效。");
        var parts=value.Split('/');
        foreach(var p in parts)
            if(p.Length==0||p is "." or ".."||p.EndsWith('.')||p.EndsWith(' ')||p.IndexOfAny([':', '<', '>', '"', '|', '?', '*'])>=0||Regex.IsMatch(p,@"\A(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\.|\z)",RegexOptions.IgnoreCase))
                throw new InvalidOperationException("更新路径包含无效文件名。");
        return value;
    }
    public static string Within(string root,string relative)
    {
        Relative(relative);root=Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar)+Path.DirectorySeparatorChar;
        var target=Path.GetFullPath(Path.Combine(root,relative.Replace('/',Path.DirectorySeparatorChar)));
        if(!target.StartsWith(root,StringComparison.OrdinalIgnoreCase))throw new InvalidOperationException("更新路径越界。");
        return target;
    }
    public static void NoLinks(string path)
    {
        for(var p=Path.GetFullPath(path);!string.IsNullOrEmpty(p);p=Path.GetDirectoryName(p))
        {
            try{if((File.GetAttributes(p)&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException("更新路径包含链接，未写入文件。");}
            catch(FileNotFoundException){}catch(DirectoryNotFoundException){}
        }
    }
    public static JsonElement Verify(JsonElement envelope,string pem,string kind)
    {
        Fields(envelope,"payload","signature");
        var payload=envelope.GetProperty("payload").GetString()??"";
        var signature=envelope.GetProperty("signature").GetString()??"";
        if(payload.Length>MaxManifestBytes*4/3+4||signature.Length>1400||string.IsNullOrWhiteSpace(pem))throw new InvalidOperationException("缺少有效的更新签名。");
        try
        {
            var bytes=Convert.FromBase64String(payload);var sig=Convert.FromBase64String(signature);
            using var cert=X509Certificate2.CreateFromPem(pem);using var key=cert.GetRSAPublicKey();
            var digest=SHA256.HashData(bytes);
            var message=Encoding.ASCII.GetBytes("90US-UPDATE-V1\0"+kind+"\0").Concat(digest).ToArray();
            if(key is null||key.KeySize<2048||!key.VerifyData(message,sig,HashAlgorithmName.SHA256,RSASignaturePadding.Pss))throw new InvalidOperationException("更新签名校验失败，原客户端未修改。");
            using var document=JsonDocument.Parse(bytes,new JsonDocumentOptions{MaxDepth=16});
            return document.RootElement.Clone();
        }
        catch(Exception e) when(e is FormatException or CryptographicException or JsonException){throw new InvalidOperationException("更新签名或清单格式无效，原客户端未修改。");}
    }
    public static Uri Https(string text)
    {
        if(string.IsNullOrWhiteSpace(text)||text.Length>4096||text.Any(c=>char.IsControl(c)||char.IsWhiteSpace(c))||!Uri.TryCreate(text,UriKind.Absolute,out var uri)||uri.Scheme!="https"||uri.Host.Length==0||uri.UserInfo.Length!=0||uri.Fragment.Length!=0)
            throw new InvalidOperationException("更新下载地址必须为 HTTPS，不能包含账号密码或片段。");
        return uri;
    }
    public static async Task<HttpResponseMessage> GetAsync(HttpClient client,Uri url,CancellationToken cancellation)
    {
        url=Https(url.AbsoluteUri);
        for(int i=0;i<=5;i++)
        {
            using var request=new HttpRequestMessage(HttpMethod.Get,url);
            var response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead,cancellation).ConfigureAwait(false);
            if(response.RequestMessage?.RequestUri is Uri final&&final!=url){response.Dispose();throw new InvalidOperationException("下载连接发生了未经检查的自动跳转。");}
            if(response.StatusCode is HttpStatusCode.MovedPermanently or HttpStatusCode.Redirect or HttpStatusCode.SeeOther or HttpStatusCode.TemporaryRedirect or HttpStatusCode.PermanentRedirect)
            {
                var location=response.Headers.Location;response.Dispose();
                if(i==5||location is null)throw new InvalidOperationException("更新下载跳转次数过多或地址缺失。");
                url=Https(new Uri(url,location).AbsoluteUri);continue;
            }
            return response;
        }
        throw new InvalidOperationException("更新下载跳转无效。");
    }
}
