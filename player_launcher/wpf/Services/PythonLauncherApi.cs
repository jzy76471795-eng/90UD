using System.Net;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace DNFLogin.Services;
public sealed record PythonPvfRelease(string Version,string FileSha256,string DownloadUrl,string ClientDownloadUrl);

// This client deliberately has no dependency on the original Go/LANA authentication.
public sealed class PythonStage3
{
    public required string ExeSha256 { get; init; }
    public required string PvfSha256 { get; init; }
    public required string PvfProfile { get; init; }
    public required string ChannelHost { get; init; }
    public required string LaunchArgument { get; init; }
    public PythonPatchManifest Patches {get;init;}=new(0,[]);
    public int NativeTunnelVersion {get;init;}
    public int PartyRelayVersion {get;init;}
    public int[] GamePorts {get;init;}=[];
    public bool SameClient(PythonStage3 other) => PvfSha256 == other.PvfSha256 && PvfProfile == other.PvfProfile && ChannelHost == other.ChannelHost && NativeTunnelVersion == other.NativeTunnelVersion && PartyRelayVersion == other.PartyRelayVersion && GamePorts.SequenceEqual(other.GamePorts);
    public override string ToString() => "PythonStage3 (launch credentials redacted)";
}

public sealed class PythonSession(string accessToken, PythonStage3 stage, string sessionId="")
{
    public string AccessToken { get; } = accessToken;
    public PythonStage3 Stage { get; } = stage;
    public string SessionId {get;}=sessionId;
    public override string ToString() => "PythonSession (credentials redacted)";
}

public sealed class PythonLauncherApi : IDisposable
{
    public static readonly IReadOnlyDictionary<string,string> SecurityQuestions = new Dictionary<string,string>
    {
        ["childhood_nickname"] = "我小时候的昵称是什么？",
        ["first_school"] = "我就读的第一所学校叫什么？",
        ["memorable_city"] = "对我最有纪念意义的城市是哪里？",
        ["childhood_book"] = "我小时候最喜欢的一本书叫什么？",
        ["favorite_teacher"] = "我印象最深的一位老师叫什么？"
    };
    public const string FrozenExeSha256 = "14b85f7e4325ef2d3c7a420740a88ef16bdd1c8f5bcda97044cc630139a56634";
    private readonly HttpClient http;
    private readonly bool ownsClient;
    private readonly Uri endpoint;
    private readonly string? trustedPath,trustedPem;
    private static readonly int[] Tail = [0,0,0,0,0,2,0,0,0,0,0,0,0,0,0,0];
    public PythonLauncherApi(Uri endpoint, HttpClient? client = null, string? trustedCaFile = null, string? trustedCaPem = null)
    {
        this.endpoint = ValidateBaseUri(endpoint.ToString());
        trustedPath=trustedCaFile;trustedPem=trustedCaPem;
        ownsClient = client is null;
        http = client ?? CreateHttpClient(trustedCaFile,trustedCaPem);
    }

    public Uri Endpoint => endpoint;
    public string UpdateCertificatePem => trustedPem??(string.IsNullOrWhiteSpace(trustedPath)?"":File.ReadAllText(trustedPath));

    public HttpClient DownloadClient(Uri address)
    {
        PythonUpdateTrust.Https(address.AbsoluteUri);
        if(address.Scheme=="https"&&address.IdnHost.Equals(endpoint.IdnHost,StringComparison.OrdinalIgnoreCase))
        {
            var client=CreateHttpClient(trustedPath,trustedPem);client.Timeout=TimeSpan.FromMinutes(30);return client;
        }
        return new HttpClient(new HttpClientHandler{SslProtocols=System.Security.Authentication.SslProtocols.None,UseProxy=false,UseCookies=false,AllowAutoRedirect=false}){Timeout=TimeSpan.FromMinutes(30)};
    }

    public static Uri ValidateBaseUri(string text)
    {
        if (!Uri.TryCreate(text, UriKind.Absolute, out var uri) || uri.Scheme != "https" || string.IsNullOrEmpty(uri.Host) || !string.IsNullOrEmpty(uri.UserInfo) || uri.AbsolutePath != "/" || uri.Query != "" || uri.Fragment != "")
            throw new InvalidOperationException("Python 登录地址必须为 HTTPS 主机与端口；不允许明文 HTTP。");
        return uri;
    }

    private void RecordConnectionFailure(string detail)
    {
        // Record only connection metadata, never HTTP bodies, tokens or account data.
        try
        {
#if PYTHON90
            var directory = LauncherPackage.ConfigurationDirectory;
#else
            var directory = Path.Combine(AppContext.BaseDirectory, "data");
#endif
            ClientDiagnostics.Append(directory, "connection.log", $"{endpoint.GetLeftPart(UriPartial.Authority)} direct=true {detail}");
        }
        catch { /* Diagnostics must not change authentication behavior. */ }
    }

    private HttpClient CreateHttpClient(string? caPath,string? caPem)
    {
        // Game authentication connects directly. A desktop/VPN proxy must not
        // intercept or become a prerequisite for this pinned HTTPS endpoint.
        var handler = new HttpClientHandler { SslProtocols=System.Security.Authentication.SslProtocols.None, AllowAutoRedirect = false, UseCookies = false, UseProxy = false };
        if (!string.IsNullOrWhiteSpace(caPem) || !string.IsNullOrWhiteSpace(caPath))
        {
            var roots = new X509Certificate2Collection();
            if (!string.IsNullOrWhiteSpace(caPem)) roots.ImportFromPem(caPem);
            else roots.ImportFromPemFile(Path.GetFullPath(caPath!));
            if (roots.Count == 0) throw new InvalidOperationException("可信 CA 文件不包含证书。");
            handler.ServerCertificateCustomValidationCallback = (_, certificate, _, errors) => {
                if (certificate is null || (errors & (SslPolicyErrors.RemoteCertificateNameMismatch | SslPolicyErrors.RemoteCertificateNotAvailable)) != 0)
                {
                    RecordConnectionFailure("TLS policy=" + errors);
                    return false;
                }
                using var chain = new X509Chain();
                chain.ChainPolicy.TrustMode = X509ChainTrustMode.CustomRootTrust;
                chain.ChainPolicy.CustomTrustStore.AddRange(roots);
                chain.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
                chain.ChainPolicy.ApplicationPolicy.Add(new System.Security.Cryptography.Oid("1.3.6.1.5.5.7.3.1"));
                var valid = chain.Build(certificate);
                if (!valid) RecordConnectionFailure("TLS chain=" + string.Join(",", chain.ChainStatus.Select(status => status.Status)));
                return valid;
            };
        }
        return new HttpClient(handler) {Timeout = TimeSpan.FromSeconds(15), MaxResponseContentBufferSize = 2 * 1024 * 1024};
    }

    private static string Text(JsonElement doc, string name)
    {
        if (!doc.TryGetProperty(name, out var val) || val.ValueKind != JsonValueKind.String || val.GetString() is not string value || value.Length == 0)
            throw new InvalidOperationException("Python 服务端响应字段不完整：" + name);
        return value;
    }
    private static bool Number(JsonElement doc, string name, int value) => doc.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.Number && p.TryGetInt32(out var n) && n == value;
    private static bool IsHex(string value, int length) => value.Length == length && value.All(Uri.IsHexDigit);
    public static PythonStage3 ValidateStage(JsonElement stage)
    {
        if (stage.ValueKind != JsonValueKind.Object) throw new InvalidOperationException("缺少 Python Stage3 启动协议。");
        // Kept for older API callers only; EXE metadata does not gate this client.
        var exe = stage.TryGetProperty("exe_sha256",out var legacyExe)&&legacyExe.ValueKind==JsonValueKind.String
            ? (legacyExe.GetString()??"").ToLowerInvariant() : "";
        var pvf = Text(stage, "pvf_sha256").ToLowerInvariant();
        var profile = Text(stage, "pvf_profile");
        var host = Text(stage, "channel_host");
        var account = Text(stage, "opaque_account");
        var token = Text(stage, "opaque_token");
        if (!IsHex(pvf, 64) || profile is not ("keyset96" or "utility-guarded" or "us-zimo"))
            throw new InvalidOperationException("PVF 加密版本不受支持。");
        if (!IPAddress.TryParse(host, out var ip) || ip.AddressFamily != System.Net.Sockets.AddressFamily.InterNetwork || ip.ToString() != host || host == "0.0.0.0" || host == "255.255.255.255" || ip.GetAddressBytes()[0] >= 224)
            throw new InvalidOperationException("Python 频道 IPv4 地址无效。");
        if (!Number(stage, "channel_port", 7001) || !Number(stage, "launch_mode", 9) || !Regex.IsMatch(account, "^[0-9]{1,20}$") || !IsHex(token, 64))
            throw new InvalidOperationException("Python 频道启动参数无效。");
        if (!stage.TryGetProperty("tail", out var tail) || tail.ValueKind != JsonValueKind.Array || tail.GetArrayLength() != Tail.Length || tail.EnumerateArray().Where((v,i) => v.ValueKind != JsonValueKind.Number || !v.TryGetInt32(out var n) || n != Tail[i]).Any())
            throw new InvalidOperationException("Python Stage3 参数尾部不一致。");
        var tunnel=stage.TryGetProperty("native_tunnel_version",out var revision)?revision.GetInt32():0;
        var relay=stage.TryGetProperty("party_relay_version",out var relayRevision)?relayRevision.GetInt32():0;
        if(relay is not (0 or 1)||relay==1&&tunnel!=2)throw new InvalidOperationException("组队中继协议不匹配，请更新配套登录器。");
        var ports=stage.TryGetProperty("game_ports",out var portArray)?portArray.EnumerateArray().Select(p=>p.GetInt32()).ToArray():[];
        if(tunnel is not (0 or 2))throw new InvalidOperationException("原生认证协议不匹配，请更新配套服务端。");
        if(tunnel==2&&(ports.Length is <1 or >16||ports.Any(p=>p<1024||p>65535||p==7001)||ports.Distinct().Count()!=ports.Length))throw new InvalidOperationException("游戏端口配置无效。");
        return new PythonStage3 {Patches=new PythonPatchManifest(0,[]),NativeTunnelVersion=tunnel,PartyRelayVersion=relay,GamePorts=ports,ExeSha256=exe,PvfSha256=pvf,PvfProfile=profile,ChannelHost=host,LaunchArgument=$"9?{host}?7001?{account}?{token}?{string.Join("?",Tail)}"};
    }

    private async Task<JsonElement> RequestAsync(HttpMethod method, string path, object? body=null, string? token=null)
    {
        using var request = new HttpRequestMessage(method, new Uri(endpoint, path));
        if (token is not null) request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        if (body is not null) request.Content = new StringContent(JsonSerializer.Serialize(body),Encoding.UTF8,"application/json");
        try
        {
            using var response = await http.SendAsync(request).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                var message = $"登录服务返回 HTTP {(int)response.StatusCode}。";
                try
                {
                    using var error = JsonDocument.Parse(await response.Content.ReadAsStringAsync().ConfigureAwait(false));
                    var detail = error.RootElement.GetProperty("error");
                    var code = detail.GetProperty("code").GetString();
                    var reason = detail.TryGetProperty("reason",out var r) && r.ValueKind == JsonValueKind.String ? r.GetString() : null;
                    var serverMessage = detail.TryGetProperty("message",out var m) && m.ValueKind == JsonValueKind.String ? m.GetString() : null;
                    RecordConnectionFailure($"HTTP status={(int)response.StatusCode} code={PythonSessionErrors.SafeCode(code)} reason={PythonSessionErrors.SafeCode(reason)}");
                    message = code switch
                    {
                        "ACCOUNT_EXISTS" => "账号已存在，请更换账号。",
                        "AUTH_FAILED" => path.StartsWith("/launcher/v1/cdk/",StringComparison.Ordinal) ? "登录已失效，请先进行账号登录，再进行兑换。" : "账号或密码错误，请重新登录。",
                        "INVALID_REQUEST" => "输入格式不正确，请检查账号、密码或兑换码。",
                        "REGISTRATION_CLOSED" => "服务器已关闭注册。",
                        "REGISTRATION_LIMIT" => "此 IP 已达到注册上限。",
                        "REGISTRATION_CODE_REQUIRED" => "需要有效且未使用的注册码。",
                        "RECOVERY_INVALID" => "找回码无效或已失效，请检查保存的找回码。",
                        "SECURITY_REQUIRED" => "请选择密保问题，并填写 2–100 个字符的密保答案。",
                        "SECURITY_INVALID" => "账号、密保问题或答案不正确，或暂时被锁定。请核对后稍后重试。",
                        "REGISTRATION_BUSY" => "注册服务正忙，请稍后重试。",
                        "RATE_LIMITED" => "操作过于频繁，请稍后再试。",
                        "ACCOUNT_BANNED" => "账号或来源 IP 已被封禁。",
                        "CDK_UNAVAILABLE" => "兑换码不存在、已作废或已用完。",
                        "CDK_ALREADY_REDEEMED" => "本账号已兑换过此兑换码。",
                        "CHARACTER_FORBIDDEN" => "所选角色不属于当前账号。",
                        "CDK_REJECTED" => "兑换未通过，请检查兑换码和角色。",
                        "SERVER_NOT_READY" => "服务器正在准备，请稍后再试。",
                        "NOT_FOUND" => "服务器尚未提供此功能，请联系管理员更新服务端。",
                        "DOOR_ROOM_NOT_CLEAR" => "请先清理当前房间的怪物。",
                        "DOOR_TARGET_CHANGED" => "房间或队伍已变化，请松开方向键后重试。",
                        "DOOR_PARTY_NOT_READY" => "队伍房间尚未同步完成，请稍后再试。",
                        "DOOR_NOT_READY" or "DOOR_SESSION_NOT_READY" => "请等待副本加载完成并保持存活。",
                        "DOOR_COOLDOWN" => "过门请求过快，请松开方向键后重试。",
                        "NO_ADJACENT_ROOM" => "这个方向没有相邻房间。",
                        "DOOR_UNSUPPORTED" => "当前副本请使用原来的过门方式。",
                        "DOOR_MOVE_REJECTED" => "过门未完成，请松开方向键后重试。",
                        _ => message
                    };
                    message = PythonSessionErrors.Describe(code,reason,serverMessage,message);
                }
                catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException) { }
                throw new InvalidOperationException(message);
            }
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync().ConfigureAwait(false));
            return doc.RootElement.Clone();
        }
        catch (HttpRequestException ex)
        {
            RecordConnectionFailure("HttpRequestFailure=" + ex.GetType().Name);
            var reason = "HTTPS 连接中断，请检查网络、可信 CA、系统时间及登录服务状态。";
            for (Exception? cause=ex; cause is not null; cause=cause.InnerException)
            {
                if (cause is System.Security.Authentication.AuthenticationException) { reason="HTTPS 证书校验失败，请检查可信 CA、证书有效期、系统时间及地址是否匹配。"; break; }
                if (cause is System.Net.Sockets.SocketException socket) reason=socket.SocketErrorCode is System.Net.Sockets.SocketError.HostNotFound or System.Net.Sockets.SocketError.NoData ? "无法解析服务器地址，请检查域名和网络。" : "无法连接登录服务，请检查网络或联系管理员。";
            }
            throw new InvalidOperationException(reason);
        }
        catch (TaskCanceledException) { RecordConnectionFailure("Timeout"); throw new InvalidOperationException("HTTPS 登录服务连接超时。"); }
        catch (JsonException) { throw new InvalidOperationException("Python 登录服务返回了无效 JSON。"); }
    }

    public Task<JsonElement> PatchFileAsync(string hash)=>RequestAsync(HttpMethod.Get,"/launcher/v1/patches/"+hash);

    public async Task<Uri?> RechargeUrlAsync()
    {
        var doc=await RequestAsync(HttpMethod.Get,"/launcher/v1/recharge").ConfigureAwait(false);
        if(!doc.TryGetProperty("enabled",out var enabled)||enabled.ValueKind!=JsonValueKind.True)return null;
        var text=Text(doc,"url");
        if(text.Length>2048||text.IndexOfAny(['\r','\n','\0'])>=0||!Uri.TryCreate(text,UriKind.Absolute,out var address)||
            (address.Scheme!="https"&&address.Scheme!="http")||address.Host.Length==0||address.UserInfo.Length!=0)
            throw new InvalidOperationException("服务器返回的充值网址无效。");
        return address;
    }

    public async Task<PythonStage3> MetaAsync()
    {
        var doc = await RequestAsync(HttpMethod.Get, "/launcher/v1/meta").ConfigureAwait(false);
        if (!Number(doc,"api_version",1) || Text(doc,"transport") != "http-direct-tcp") throw new InvalidOperationException("Python 登录接口版本不一致。");
        if (!doc.TryGetProperty("server_ready",out var ready) || ready.ValueKind != JsonValueKind.True) throw new InvalidOperationException("服务器正在准备或维护中。");
        if (!doc.TryGetProperty("stage3",out var stage)) throw new InvalidOperationException("缺少 Stage3 启动协议。");
        return ValidateStage(stage);
    }


    public async Task<PythonSession> LoginAsync(string account, string password, PythonStage3 expected)
    {
        if (string.IsNullOrWhiteSpace(account) || string.IsNullOrEmpty(password)) throw new InvalidOperationException("请填写账号和密码。");
        var doc = await RequestAsync(HttpMethod.Post,"/launcher/v1/sessions",expected.NativeTunnelVersion==2?(object)new {account=account.Trim(),password,device_id=PythonDeviceIdentity.Read()}:new {account=account.Trim(),password}).ConfigureAwait(false);
        string? token = null;
        try
        {
            token = Text(doc,"access_token");
            if (!Regex.IsMatch(token,"^[A-Za-z0-9_.~+/=-]{1,2048}$") || Text(doc,"token_type") != "Bearer") throw new InvalidOperationException("Python 登录票据无效。");
            var stage = ValidateStage(doc.GetProperty("stage3"));
            if (!stage.SameClient(expected)) throw new InvalidOperationException("登录期间客户端发布信息发生变化，请重新登录。");
            var sid=Text(doc,"session_id");
            if(stage.NativeTunnelVersion==2&&!Regex.IsMatch(sid,"^[a-f0-9]{32}$"))throw new InvalidOperationException("原生登录会话编号无效。");
            return new PythonSession(token,stage,sid);
        }
        catch
        {
            if (token is not null) try {await RequestAsync(HttpMethod.Delete,"/launcher/v1/sessions/current",token:token).ConfigureAwait(false);} catch { }
            throw;
        }
    }

    public async Task<(string Account,string RecoveryCode)> RegisterAsync(string account, string password, string registrationCode = "", string securityQuestion = "", string securityAnswer = "")
    {
        if (string.IsNullOrWhiteSpace(account) || account.Trim().Length > 64 || account.IndexOfAny(['?', '\0', '\r', '\n']) >= 0)
            throw new InvalidOperationException("账号需为 1–64 个字符，不能包含问号或换行。");
        if (password.Length is < 8 or > 128 || password.IndexOfAny(['\0', '\r', '\n']) >= 0)
            throw new InvalidOperationException("密码需为 8–128 个字符，不能包含换行。");
        ValidateSecurity(securityQuestion, securityAnswer);
        var doc = await RequestAsync(HttpMethod.Post, "/launcher/v1/accounts", new { account = account.Trim(), password, registration_code = registrationCode.Trim(), security_question = securityQuestion, security_answer = securityAnswer });
        if (!doc.TryGetProperty("registered", out var ok) || ok.ValueKind != JsonValueKind.True) throw new InvalidOperationException("注册结果未确认，请尝试使用该账号登录核对。");
        return (Text(doc, "account"), Text(doc, "recovery_code"));
    }

    public async Task<(string Account,string RecoveryCode)> RecoverAsync(string recoveryCode, string newPassword)
    {
        if (newPassword.Length is < 8 or > 128 || newPassword.IndexOfAny(['\0','\r','\n']) >= 0) throw new InvalidOperationException("新密码需为 8–128 个字符，不能包含换行。");
        var doc = await RequestAsync(HttpMethod.Post, "/launcher/v1/accounts/recovery", new { recovery_code = recoveryCode.Trim(), new_password = newPassword });
        return (Text(doc, "account"), Text(doc, "recovery_code"));
    }

    private static void ValidateSecurity(string question, string answer)
    {
        if (!SecurityQuestions.ContainsKey(question) || answer.Trim().Length is < 2 or > 100 || answer.Any(char.IsControl))
            throw new InvalidOperationException("请选择密保问题，并填写 2–100 个字符的密保答案。");
    }

    public async Task<(string Account,string RecoveryCode)> RecoverBySecurityAsync(string account, string question, string answer, string newPassword)
    {
        if (string.IsNullOrWhiteSpace(account) || account.Trim().Length > 64 || account.IndexOfAny(['?', '\0', '\r', '\n']) >= 0)
            throw new InvalidOperationException("请填写注册时使用的账号。");
        ValidateSecurity(question, answer);
        if (newPassword.Length is < 8 or > 128 || newPassword.IndexOfAny(['\0','\r','\n']) >= 0) throw new InvalidOperationException("新密码需为 8–128 个字符，不能包含换行。");
        var doc = await RequestAsync(HttpMethod.Post, "/launcher/v1/accounts/security-recovery", new { account = account.Trim(), security_question = question, security_answer = answer, new_password = newPassword });
        return (Text(doc, "account"), Text(doc, "recovery_code"));
    }

    internal Task<JsonElement> AdventureOperationAsync(PythonSession session, object body) =>
        RequestAsync(HttpMethod.Post, "/launcher/v1/adventure/operation", body, session.AccessToken);

    internal Task<JsonElement> AdventureCollectionAsync(PythonSession session, object body) =>
        RequestAsync(HttpMethod.Post, "/launcher/v1/adventure/collection", body, session.AccessToken);

    public Task<JsonElement> NoticesAsync() => RequestAsync(HttpMethod.Get, "/launcher/v1/notices");
    public Task<JsonElement> AssistAsync(PythonSession session, object body) =>
        RequestAsync(HttpMethod.Post, "/launcher/v1/assist", body, session.AccessToken);
    public Task<JsonElement> AnnouncementsAsync(string token,long since) => RequestAsync(HttpMethod.Get, "/launcher/v1/announcements?since="+since.ToString(System.Globalization.CultureInfo.InvariantCulture), token:token);
    public Task<JsonElement> AcknowledgeAnnouncementAsync(string token,long id) => RequestAsync(HttpMethod.Post, "/launcher/v1/announcements/receipt", new { id }, token);
    public Task<JsonElement> RegistrationPolicyAsync() => RequestAsync(HttpMethod.Get, "/launcher/v1/registration");
    public async Task<PythonPvfRelease> PvfLatestAsync()
    {
        var result=await RequestAsync(HttpMethod.Get,"/launcher/v1/pvf/latest").ConfigureAwait(false);
        if(result.ValueKind!=JsonValueKind.Object||!result.TryGetProperty("enabled",out var enabled)||enabled.ValueKind!=JsonValueKind.True||!result.TryGetProperty("data",out var data)||data.ValueKind!=JsonValueKind.Object)throw new InvalidOperationException("服务器尚未启用客户端更新版本。");
        var version=Text(data,"pvf_version");var hash=Text(data,"file_sha256").ToLowerInvariant();
        if(!IsHex(hash,64))throw new InvalidOperationException("数据库中的 PVF 指纹无效。");
        string Url(string name){if(!data.TryGetProperty(name,out var field)||field.ValueKind!=JsonValueKind.String)throw new InvalidOperationException("数据库中的下载地址字段不完整。");var value=field.GetString()??"";if(value!=""&&(!Uri.TryCreate(value,UriKind.Absolute,out var uri)||uri.Scheme!="https"||uri.UserInfo.Length!=0||uri.Fragment.Length!=0||value.Any(char.IsControl)))throw new InvalidOperationException("数据库中的下载地址无效。");return value;}
        return new PythonPvfRelease(version,hash,Url("download_url"),Url("client_download_url"));
    }
    public Task<JsonElement> CdkCharactersAsync(string token) => RequestAsync(HttpMethod.Get, "/launcher/v1/cdk/characters", token: token);
    public Task<JsonElement> CdkHistoryAsync(string token) => RequestAsync(HttpMethod.Get, "/launcher/v1/cdk/redemptions", token: token);
    public Task<JsonElement> RedeemAsync(string token, string code, long characterId, string requestId) =>
        RequestAsync(HttpMethod.Post, "/launcher/v1/cdk/redemptions", new { code = code.Trim(), character_id = characterId, request_id = requestId }, token);

    public async Task<string> KeyAsync(PythonSession session)
    {
        if (session.Stage.PvfProfile != "keyset96") throw new InvalidOperationException("此 PVF 配置不需要动态密钥。");
        var doc = await RequestAsync(HttpMethod.Get,"/launcher/v1/pvf/key?file_sha256="+session.Stage.PvfSha256,token:session.AccessToken).ConfigureAwait(false);
        if (!doc.TryGetProperty("data",out var data)) throw new InvalidOperationException("缺少 PVF 密钥响应。");
        var key = Text(data,"key_set");
        if (Text(data,"file_sha256").ToLowerInvariant() != session.Stage.PvfSha256 || Text(data,"pvf_profile") != "keyset96" || !IsHex(key,96))
            throw new InvalidOperationException("PVF 密钥与当前资源不匹配；游戏尚未启动。");
        return key.ToLowerInvariant();
    }
    public async Task<PythonRelayAuthorization?> PartyRelayAsync(PythonSession session)
    {
        if(session.Stage.PartyRelayVersion==0)return null;
        var result=await RequestAsync(HttpMethod.Post,"/launcher/v1/party-relay/grants",new {request_id=Guid.NewGuid().ToString("N")},session.AccessToken).ConfigureAwait(false);
        if(!Number(result,"protocol",1))throw new InvalidOperationException("组队启动协议不匹配。");
        var relay=ValidateBaseUri(Text(result,"relay_url"));
        if(!relay.IdnHost.Equals(endpoint.IdnHost,StringComparison.OrdinalIgnoreCase))throw new InvalidOperationException("组队中继服务器身份不匹配。");
        var grant=result.GetProperty("grant");
        if(grant.ValueKind!=JsonValueKind.Object||!IsHex(Text(grant,"boot"),32)||!IsHex(Text(grant,"secret"),64)||!IsHex(Text(grant,"launch_token"),64)||
           !grant.GetProperty("member_id").TryGetUInt32(out var member)||member is 0 or >65535||!grant.GetProperty("session_id").TryGetUInt64(out var id)||id==0||
           !grant.GetProperty("expires_ms").TryGetInt64(out var expires)||expires<=0||!grant.GetProperty("mtu").TryGetInt32(out var mtu)||mtu is <512 or >1400)
            throw new InvalidOperationException("组队启动凭据无效。");
        return new PythonRelayAuthorization(relay.GetLeftPart(UriPartial.Authority),grant.Clone(),UpdateCertificatePem);
    }
    public async Task LogoutAsync(PythonSession session) => await RequestAsync(HttpMethod.Delete,"/launcher/v1/sessions/current",token:session.AccessToken).ConfigureAwait(false);
    public void Dispose() { if (ownsClient) http.Dispose(); }
}
