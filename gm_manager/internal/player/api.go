package player

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type Patch struct {
	FileName   string `json:"file_name"`
	SHA256     string `json:"sha256"`
	Bytes      int64  `json:"bytes"`
	Enabled    bool   `json:"enabled"`
	OnMismatch string `json:"on_mismatch"`
}
type PatchManifest struct {
	Revision int64           `json:"revision"`
	Items    []Patch         `json:"items"`
	Signed   json.RawMessage `json:"signed,omitempty"`
}
type Stage struct {
	ExeSHA256           string        `json:"exe_sha256"`
	PvfSHA256           string        `json:"pvf_sha256"`
	PvfProfile          string        `json:"pvf_profile"`
	ChannelHost         string        `json:"channel_host"`
	ChannelPort         int           `json:"channel_port"`
	LaunchMode          int           `json:"launch_mode"`
	Account             string        `json:"opaque_account"`
	Token               string        `json:"opaque_token"`
	Tail                []int         `json:"tail"`
	NativeTunnelVersion int           `json:"native_tunnel_version"`
	GamePorts           []int         `json:"game_ports"`
	Patches             PatchManifest `json:"client_patches"`
}

func (s Stage) String() string   { return "Stage (credentials redacted)" }
func (s Stage) GoString() string { return s.String() }
func (s Stage) Validate() error {
	if s.ExeSHA256 != FrozenExeSHA256 || !hashPattern.MatchString(s.PvfSHA256) || (s.PvfProfile != "keyset96" && s.PvfProfile != "utility-guarded" && s.PvfProfile != "us-zimo") {
		return errors.New("客户端或PVF版本不受支持")
	}
	ip := net.ParseIP(s.ChannelHost)
	if ip == nil || ip.To4() == nil || ip.String() != s.ChannelHost || ip.IsUnspecified() || ip.IsMulticast() || s.ChannelHost == "255.255.255.255" {
		return errors.New("频道IPv4地址无效")
	}
	if s.ChannelPort != 7001 || s.LaunchMode != 9 || !regexp.MustCompile(`^[0-9]{1,20}$`).MatchString(s.Account) || !hashPattern.MatchString(strings.ToLower(s.Token)) || !reflect.DeepEqual(s.Tail, []int{0, 0, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}) {
		return errors.New("游戏启动参数无效")
	}
	if s.NativeTunnelVersion != 0 && s.NativeTunnelVersion != 2 {
		return errors.New("游戏认证协议版本不支持")
	}
	if s.NativeTunnelVersion == 2 {
		if len(s.GamePorts) < 1 || len(s.GamePorts) > 16 {
			return errors.New("游戏端口无效")
		}
		seen := map[int]bool{}
		for _, p := range s.GamePorts {
			if p < 1024 || p > 65535 || p == 7001 || seen[p] {
				return errors.New("游戏端口重复或无效")
			}
			seen[p] = true
		}
	}
	return s.Patches.Validate()
}
func (p PatchManifest) Validate() error {
	if p.Revision < 0 || len(p.Items) > 50 || (p.Revision == 0 && len(p.Items) != 0) {
		return errors.New("补丁版本无效")
	}
	seen := map[string]bool{}
	for _, v := range p.Items {
		k := strings.ToLower(v.FileName)
		if !regexp.MustCompile(`(?i)^[a-z0-9_][a-z0-9_.-]{0,95}\.dll$`).MatchString(v.FileName) || Relative(v.FileName) != nil || !hashPattern.MatchString(v.SHA256) || v.Bytes < 256 || v.Bytes > 786432 || !v.Enabled || v.OnMismatch != "block" || seen[k] {
			return errors.New("补丁清单无效")
		}
		seen[k] = true
	}
	return nil
}
func (s Stage) SameClient(v Stage) bool {
	return s.ExeSHA256 == v.ExeSHA256 && s.PvfSHA256 == v.PvfSHA256 && s.PvfProfile == v.PvfProfile && s.ChannelHost == v.ChannelHost && s.NativeTunnelVersion == v.NativeTunnelVersion && reflect.DeepEqual(s.GamePorts, v.GamePorts) && s.Patches.Revision == v.Patches.Revision && reflect.DeepEqual(s.Patches.Items, v.Patches.Items)
}
func (s Stage) Argument() string {
	parts := []string{"9", s.ChannelHost, "7001", s.Account, s.Token}
	for _, n := range s.Tail {
		parts = append(parts, strconv.Itoa(n))
	}
	return strings.Join(parts, "?")
}

type Session struct {
	AccessToken string `json:"access_token"`
	TokenType   string `json:"token_type"`
	SessionID   string `json:"session_id"`
	Stage       Stage  `json:"stage3"`
}

func (s Session) String() string   { return "Session (credentials redacted)" }
func (s Session) GoString() string { return s.String() }

type API struct {
	Config   Config
	HTTP     *http.Client
	download *http.Client
}
type HTTPError struct {
	Status  int
	Message string
}

func (e *HTTPError) Error() string { return e.Message }

func NewAPI(c Config) (*API, error) {
	if e := c.Validate(); e != nil {
		return nil, e
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM([]byte(c.CertificatePEM)) {
		return nil, errors.New("公开证书不可用")
	}
	tr := &http.Transport{Proxy: nil, TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: pool}, DialContext: (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext, MaxIdleConnsPerHost: 8, ResponseHeaderTimeout: 15 * time.Second}
	c1 := &http.Client{Transport: tr, Timeout: 20 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	return &API{Config: c, HTTP: c1}, nil
}
func (a *API) Close() {
	a.HTTP.CloseIdleConnections()
	if a.download != nil {
		a.download.CloseIdleConnections()
	}
}
func (a *API) Request(ctx context.Context, method, path, token string, body, out any) error {
	if !strings.HasPrefix(path, "/launcher/v1/") {
		return errors.New("登录接口路径无效")
	}
	var reader io.Reader
	if body != nil {
		b, e := json.Marshal(body)
		if e != nil {
			return e
		}
		reader = bytes.NewReader(b)
	}
	req, e := http.NewRequestWithContext(ctx, method, strings.TrimRight(a.Config.LoginURL, "/")+path, reader)
	if e != nil {
		return e
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, e := a.HTTP.Do(req)
	if e != nil {
		return errors.New("无法连接HTTPS登录服务，请检查网络、证书和系统时间")
	}
	defer resp.Body.Close()
	raw, e := io.ReadAll(io.LimitReader(resp.Body, 2<<20+1))
	if e != nil || len(raw) > 2<<20 {
		return errors.New("登录服务响应不完整或超过限制")
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		var doc struct {
			Error struct {
				Code string `json:"code"`
			} `json:"error"`
		}
		_ = json.Unmarshal(raw, &doc)
		if doc.Error.Code == "INVALID_REQUEST" {
			return &HTTPError{resp.StatusCode, "输入格式无效，请检查账号、密码、密保或兑换码"}
		}
		if doc.Error.Code == "RECOVERY_DISABLED" {
			return &HTTPError{resp.StatusCode, "服务器已关闭找回码方式，请使用密保找回"}
		}
		messages := map[string]string{"ACCOUNT_EXISTS": "账号已存在", "AUTH_FAILED": "账号密码错误或登录已失效", "REGISTRATION_CLOSED": "服务器已关闭注册", "REGISTRATION_LIMIT": "此IP已达到注册上限", "REGISTRATION_CODE_REQUIRED": "需要有效注册码", "RECOVERY_INVALID": "找回码无效或已失效", "SECURITY_INVALID": "密保信息错误或已暂时锁定", "SECURITY_REQUIRED": "请填写密保问题和答案", "RATE_LIMITED": "操作过于频繁，请稍后再试", "REGISTRATION_BUSY": "注册繁忙，请稍后重试", "ACCOUNT_BANNED": "账号或来源IP已封禁", "CDK_UNAVAILABLE": "兑换码不存在、已作废或已用完", "CDK_ALREADY_REDEEMED": "本账号已兑换此码", "CHARACTER_FORBIDDEN": "角色不属于此账号", "CDK_REJECTED": "兑换未通过", "SERVER_NOT_READY": "服务器正在准备或维护", "NOT_FOUND": "服务端尚未提供此功能"}
		if msg := messages[doc.Error.Code]; msg != "" {
			return &HTTPError{resp.StatusCode, msg}
		}
		return &HTTPError{resp.StatusCode, fmt.Sprintf("登录服务返回HTTP %d；写入请求请先核对结果，不要盲目重试", resp.StatusCode)}
	}
	if out == nil {
		return nil
	}
	if uniqueJSON(raw) != nil || json.Unmarshal(raw, out) != nil {
		return errors.New("登录服务返回无效JSON")
	}
	return nil
}
func (a *API) Meta(ctx context.Context) (Stage, error) {
	var d struct {
		Version   int    `json:"api_version"`
		Transport string `json:"transport"`
		Ready     bool   `json:"server_ready"`
		Stage     Stage  `json:"stage3"`
	}
	e := a.Request(ctx, "GET", "/launcher/v1/meta", "", nil, &d)
	if e != nil {
		return d.Stage, e
	}
	if d.Version != 1 || d.Transport != "http-direct-tcp" || !d.Ready {
		return d.Stage, errors.New("登录服务未就绪或协议不匹配")
	}
	return d.Stage, d.Stage.Validate()
}
func (a *API) Login(ctx context.Context, account, password, device string, expected Stage) (Session, error) {
	var s Session
	body := map[string]string{"account": strings.TrimSpace(account), "password": password}
	if expected.NativeTunnelVersion == 2 {
		body["device_id"] = device
	}
	e := a.Request(ctx, "POST", "/launcher/v1/sessions", "", body, &s)
	if e != nil {
		return s, e
	}
	if s.TokenType != "Bearer" || len(s.AccessToken) > 2048 || !regexp.MustCompile(`^[A-Za-z0-9_.~+/=-]+$`).MatchString(s.AccessToken) || s.Stage.Validate() != nil || !s.Stage.SameClient(expected) || (s.Stage.NativeTunnelVersion == 2 && !regexp.MustCompile(`^[a-f0-9]{32}$`).MatchString(s.SessionID)) {
		if s.AccessToken != "" {
			_ = a.Logout(ctx, s)
		}
		return Session{}, errors.New("登录回执或资源版本不一致，请重新检查")
	}
	return s, nil
}
func (a *API) Logout(ctx context.Context, s Session) error {
	return a.Request(ctx, "DELETE", "/launcher/v1/sessions/current", s.AccessToken, nil, nil)
}
func (a *API) Key(ctx context.Context, s Session) (string, error) {
	var d struct {
		Data struct {
			Key     string `json:"key_set"`
			Hash    string `json:"file_sha256"`
			Profile string `json:"pvf_profile"`
		} `json:"data"`
	}
	if e := a.Request(ctx, "GET", "/launcher/v1/pvf/key?file_sha256="+s.Stage.PvfSHA256, s.AccessToken, nil, &d); e != nil {
		return "", e
	}
	if d.Data.Hash != s.Stage.PvfSHA256 || d.Data.Profile != "keyset96" || !regexp.MustCompile(`^[0-9a-fA-F]{96}$`).MatchString(d.Data.Key) {
		return "", errors.New("动态密钥与当前PVF不匹配")
	}
	return strings.ToLower(d.Data.Key), nil
}
func (a *API) MultiOpen(ctx context.Context) (bool, error) {
	var d struct {
		Enabled *bool `json:"enabled"`
	}
	if e := a.Request(ctx, "GET", "/launcher/v1/multi-open", "", nil, &d); e != nil {
		return false, e
	}
	if d.Enabled == nil {
		return false, errors.New("双开策略无效")
	}
	return *d.Enabled, nil
}
func (a *API) DownloadHTTP(address string) (*http.Client, error) {
	_, e := HTTPS(address, false)
	if e != nil {
		return nil, e
	}
	base, _ := url.Parse(a.Config.LoginURL)
	tr := &downloadTransport{host: base.Hostname(), pinned: a.HTTP.Transport.(*http.Transport).Clone(), public: &http.Transport{Proxy: nil, TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12}, ResponseHeaderTimeout: 20 * time.Second}}
	return &http.Client{Transport: tr, Timeout: 30 * time.Minute, CheckRedirect: func(req *http.Request, via []*http.Request) error {
		if len(via) > 5 {
			return errors.New("下载跳转过多")
		}
		_, e := HTTPS(req.URL.String(), false)
		return e
	}}, nil
}

type downloadTransport struct {
	host           string
	pinned, public *http.Transport
}

func (t *downloadTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if _, e := HTTPS(r.URL.String(), false); e != nil {
		return nil, e
	}
	if strings.EqualFold(r.URL.Hostname(), t.host) {
		return t.pinned.RoundTrip(r)
	}
	return t.public.RoundTrip(r)
}
func (t *downloadTransport) CloseIdleConnections() {
	t.pinned.CloseIdleConnections()
	t.public.CloseIdleConnections()
}
