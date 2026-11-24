#if PYTHON90
using System.IO;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace DNFLogin.Services;

public sealed record LauncherPackageManifest(string ServerId, string DisplayName, Uri LoginUrl, string CertificatePath)
{
    public string CertificatePem { get; init; } = "";
    public string CaFile => CertificatePem.Length > 0 ? "" : "server-cert.pem";
}

// Only public distribution settings belong here. Reading never writes settings or connects to a server.
public static class LauncherPackage
{
    public static LauncherPackageManifest? Current { get; private set; }
    private static string Identity => Convert.ToHexString(SHA256.HashData(
        Encoding.UTF8.GetBytes(Current!.ServerId.ToLowerInvariant()))).ToLowerInvariant();

    // Portable output: configuration, logs and extracted components stay beside
    // this executable. No new AppData files are created on the system drive.
    public static string PortableRoot => Path.GetFullPath(Environment.GetEnvironmentVariable("PY90_PORTABLE_ROOT") ?? AppContext.BaseDirectory);
    public static string ConfigurationDirectory => Path.Combine(PythonLauncherPaths.DataDirectory ?? Path.Combine(PortableRoot,"data"), Current is null ? "default" : Identity);
    public static string RuntimeDirectory => Path.Combine(ConfigurationDirectory, "runtime");
    public static string MutexName => Current is null ? @"Local\DNFLoginPy90_SingleInstance" : @"Local\DNFLoginPy90_" + Identity + "_SingleInstance";
    public static string ShowSignalName => Current is null ? @"Local\DNFLoginPy90_ShowSignal" : @"Local\DNFLoginPy90_" + Identity + "_" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Current.LoginUrl.AbsoluteUri))) + "_ShowSignal";

    public static void Initialize(string baseDirectory) => Current = ReadExecutable(Environment.ProcessPath
        ?? throw new InvalidOperationException("无法确定登录器位置。"));

    // Payload and certificate are loaded directly into memory, never sidecar files.
    public static LauncherPackageManifest ReadExecutable(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        if (stream.Length < 72) throw new InvalidOperationException("登录器缺少内置配置，请由 GM 重新生成。");
        stream.Seek(-72, SeekOrigin.End);
        var footer = new byte[72]; stream.ReadExactly(footer);
        var magic = new byte[32]; Encoding.ASCII.GetBytes("PY90_LAUNCHER_CONFIG_V2").CopyTo(magic, 0);
        var length = System.Buffers.Binary.BinaryPrimitives.ReadUInt64LittleEndian(footer);
        if (!footer.AsSpan(40).SequenceEqual(magic) || length == 0 || length > 256 * 1024
            || length > (ulong)(stream.Length - 72))
            throw new InvalidOperationException("登录器内置配置无效，请由 GM 重新生成。");
        stream.Seek(-72 - (long)length, SeekOrigin.End);
        var payload = new byte[(int)length]; stream.ReadExactly(payload);
        if (!CryptographicOperations.FixedTimeEquals(SHA256.HashData(payload), footer.AsSpan(8, 32)))
            throw new InvalidOperationException("登录器内置配置校验失败。");
        using var document = JsonDocument.Parse(payload);
        var json = document.RootElement;
        var expected = new HashSet<string>(StringComparer.Ordinal) { "schema_version", "server_id", "display_name", "login_url", "certificate_pem" };
        var found = new HashSet<string>(StringComparer.Ordinal);
        if (json.ValueKind != JsonValueKind.Object) throw new InvalidOperationException("内置配置必须为对象。");
        foreach (var field in json.EnumerateObject())
            if (!expected.Contains(field.Name) || !found.Add(field.Name)) throw new InvalidOperationException("内置配置有重复或未知字段。");
        if (!found.SetEquals(expected) || !json.GetProperty("schema_version").TryGetInt32(out var version) || version != 2)
            throw new InvalidOperationException("内置配置版本无效。");
        string Text(string name) => json.GetProperty(name).GetString() ?? throw new InvalidOperationException("内置配置字段为空。");
        var id = Text("server_id");
        var display = Text("display_name");
        var address = Text("login_url");
        if (!Regex.IsMatch(id, @"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\z") || string.IsNullOrWhiteSpace(display)
            || display.EnumerateRunes().Count() > 80 || display.EnumerateRunes().Any(Rune.IsControl))
            throw new InvalidOperationException("内置服务器标识或名称无效。");
        if (address != address.Trim() || address.Contains('\\') || address.EnumerateRunes().Any(Rune.IsControl)
            || !Uri.TryCreate(address, UriKind.Absolute, out var url) || url.Scheme != Uri.UriSchemeHttps
            || string.IsNullOrEmpty(url.Host) || url.Port < 1 || url.UserInfo.Length != 0 || url.AbsolutePath != "/"
            || url.Query.Length != 0 || url.Fragment.Length != 0)
            throw new InvalidOperationException("内置 HTTPS 地址无效。");
        var pem = Text("certificate_pem");
        ValidateCertificateText(pem, url.IdnHost.Trim('[', ']'));
        return new LauncherPackageManifest(id, display, url, "") { CertificatePem = pem };
    }

    public static LauncherPackageManifest? Read(string baseDirectory)
    {
        var root = Path.GetFullPath(baseDirectory);
        var manifest = Path.Combine(root, "launcher.json");
        try
        {
            if (!Directory.Exists(root)) throw new InvalidOperationException("登录器所在目录不存在。");
            FileAttributes attributes;
            try { attributes = File.GetAttributes(manifest); }
            catch (FileNotFoundException) { return null; }
            if ((attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != 0)
                throw new InvalidOperationException("launcher.json 必须是普通文件，不能使用目录或链接。");
            using var document = JsonDocument.Parse(ReadText(manifest, 16 * 1024));
            var json = document.RootElement;
            if (json.ValueKind != JsonValueKind.Object) throw new InvalidOperationException("launcher.json 必须为 JSON 对象。");
            var expected = new HashSet<string>(StringComparer.Ordinal) { "schema_version", "server_id", "display_name", "login_url", "ca_file" };
            var found = new HashSet<string>(StringComparer.Ordinal);
            foreach (var field in json.EnumerateObject())
                if (!expected.Contains(field.Name) || !found.Add(field.Name))
                    throw new InvalidOperationException("launcher.json 存在未知或重复字段。");
            if (!found.SetEquals(expected) || json.GetProperty("schema_version").ValueKind != JsonValueKind.Number
                || !json.GetProperty("schema_version").TryGetInt32(out var version) || version != 1)
                throw new InvalidOperationException("launcher.json 字段不完整或 schema_version 不受支持。");
            string Text(string name)
            {
                var value = json.GetProperty(name);
                if (value.ValueKind != JsonValueKind.String || value.GetString() is not string text
                    || string.IsNullOrWhiteSpace(text) || text.EnumerateRunes().Any(Rune.IsControl))
                    throw new InvalidOperationException("launcher.json 文本字段为空、类型错误或包含控制字符。");
                return text;
            }
            var id = Text("server_id");
            if (!Regex.IsMatch(id, @"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\z"))
                throw new InvalidOperationException("server_id 必须为 1–64 位安全字母、数字、下划线或连字符。");
            var display = Text("display_name");
            if (display.EnumerateRunes().Count() > 80) throw new InvalidOperationException("display_name 不能超过 80 个字符。");
            var urlText = Text("login_url");
            if (urlText.Length > 4096 || urlText != urlText.Trim()
                || !Uri.TryCreate(urlText, UriKind.Absolute, out var url) || url.Scheme != Uri.UriSchemeHttps
                || string.IsNullOrEmpty(url.Host) || url.Port < 1 || url.Port > 65535 || url.UserInfo.Length != 0
                || url.AbsolutePath != "/" || url.Query.Length != 0 || url.Fragment.Length != 0)
                throw new InvalidOperationException("login_url 必须为 HTTPS 主机与端口，不允许账号、路径、查询或片段。");
            if (Text("ca_file") != "server-cert.pem")
                throw new InvalidOperationException("ca_file 必须为包内相对文件名 server-cert.pem，不能指定其他路径。");
            var certificatePath = Path.GetFullPath(Path.Combine(root, "server-cert.pem"));
            ValidateCertificate(certificatePath, url.IdnHost.Trim('[', ']'));
            return new LauncherPackageManifest(id, display, url, certificatePath);
        }
        catch (InvalidOperationException error)
        {
            throw new InvalidOperationException("登录器配置无法使用：" + manifest + "\n" + error.Message);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException
            or CryptographicException or ArgumentException)
        {
            throw new InvalidOperationException("登录器配置无法读取或校验：" + manifest
                + "\n请检查 launcher.json 格式、包内 server-cert.pem 和文件读取权限；未使用旧配置连接。");
        }
    }

    private static string ReadText(string path, int maximum)
    {
        if ((File.GetAttributes(path) & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != 0)
            throw new InvalidOperationException("配置与证书必须为包内普通文件，不能使用链接。");
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        var bytes = new byte[maximum + 1];
        var count = 0;
        while (count < bytes.Length)
        {
            var length = stream.Read(bytes, count, bytes.Length - count);
            if (length == 0) break;
            count += length;
        }
        if (count > maximum) throw new InvalidOperationException("配置或证书文件过大。");
        var start = count >= 3 && bytes[0] == 0xef && bytes[1] == 0xbb && bytes[2] == 0xbf ? 3 : 0;
        return new UTF8Encoding(false, true).GetString(bytes, start, count - start);
    }

    private static void ValidateCertificate(string path, string host)
    {
        if (!File.Exists(path)) throw new InvalidOperationException("缺少包内 server-cert.pem 公开证书。");
        ValidateCertificateText(ReadText(path, 128 * 1024), host);
    }

    private static void ValidateCertificateText(string pem, string host)
    {
        if (Encoding.UTF8.GetByteCount(pem) > 128 * 1024) throw new InvalidOperationException("公开证书过大。");
        var text = pem.Trim();
        if (!PemEncoding.TryFind(text, out var fields) || text[fields.Label] != "CERTIFICATE"
            || fields.Location.Start.GetOffset(text.Length) != 0 || fields.Location.End.GetOffset(text.Length) != text.Length)
            throw new InvalidOperationException("server-cert.pem 必须只含一张公开证书，不能包含私钥或其他内容。");
        using var certificate = X509Certificate2.CreateFromPem(text);
        using var rsa = certificate.GetRSAPublicKey();
        if (rsa is null || rsa.KeySize < 2048 || certificate.SignatureAlgorithm.Value is not
            ("1.2.840.113549.1.1.11" or "1.2.840.113549.1.1.12" or "1.2.840.113549.1.1.13"))
            throw new InvalidOperationException("公开证书必须使用 RSA 2048 位以上密钥及 RSA SHA256/384/512 签名。");
        var now = DateTime.UtcNow;
        if (now < certificate.NotBefore.ToUniversalTime() || now > certificate.NotAfter.ToUniversalTime())
            throw new InvalidOperationException("server-cert.pem 尚未生效或已经过期。");
        if (!certificate.Extensions.OfType<X509EnhancedKeyUsageExtension>().Any(extension =>
                extension.EnhancedKeyUsages.Cast<Oid>().Any(oid => oid.Value == "1.3.6.1.5.5.7.3.1")))
            throw new InvalidOperationException("server-cert.pem 缺少服务器身份验证用途（ServerAuth）。");
        if (!certificate.MatchesHostname(host, allowWildcards: false, allowCommonName: false))
            throw new InvalidOperationException("server-cert.pem 的 SAN 主机名或 IP 与 login_url 不匹配。");
    }

    public static void ApplyConnection(AppSettings settings)
    {
        if (Current is not { } package) return;
        settings.LoginMode = "Remote";
        settings.RemoteIp = package.LoginUrl.GetLeftPart(UriPartial.Authority);
        settings.RemotePort = package.LoginUrl.Port.ToString(System.Globalization.CultureInfo.InvariantCulture);
        settings.RemoteCAFile = package.CaFile;
        var profile = settings.Profiles?.Find(value => value.Id == "90");
        if (profile is not null)
        {
            profile.LoginMode = settings.LoginMode;
            profile.RemoteIp = settings.RemoteIp;
            profile.RemotePort = settings.RemotePort;
            profile.RemoteCAFile = settings.RemoteCAFile;
        }
    }

    public static string ResolveCaFile(string configured) => Current?.CertificatePath ?? configured;
}
#endif
