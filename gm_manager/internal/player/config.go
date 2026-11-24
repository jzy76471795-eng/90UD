package player

import (
	"bytes"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/url"
	"newadmin/internal/launcherbundle"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const Version = "1.0.0-go"
const FrozenExeSHA256 = "14b85f7e4325ef2d3c7a420740a88ef16bdd1c8f5bcda97044cc630139a56634"

var hashPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)
var serverPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`)

type Config struct {
	SchemaVersion  int    `json:"schema_version"`
	ServerID       string `json:"server_id"`
	DisplayName    string `json:"display_name"`
	LoginURL       string `json:"login_url"`
	CertificatePEM string `json:"certificate_pem"`
}

func StrictJSON(raw []byte, dst any) error {
	if err := uniqueJSON(raw); err != nil {
		return err
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(dst); err != nil {
		return errors.New("配置或清单字段无效")
	}
	if d.Decode(new(any)) != io.EOF {
		return errors.New("JSON存在多余内容")
	}
	return nil
}

// Reject duplicate keys at every level before decoding security-sensitive data.
func uniqueJSON(raw []byte) error {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	var value func(int) error
	value = func(depth int) error {
		if depth > 20 {
			return errors.New("JSON嵌套过深")
		}
		t, e := d.Token()
		if e != nil {
			return e
		}
		if delim, ok := t.(json.Delim); ok {
			if delim == '{' {
				seen := map[string]bool{}
				for d.More() {
					k, e := d.Token()
					if e != nil {
						return e
					}
					s, ok := k.(string)
					if !ok || seen[s] {
						return errors.New("JSON字段重复")
					}
					seen[s] = true
					if e = value(depth + 1); e != nil {
						return e
					}
				}
			} else if delim == '[' {
				for d.More() {
					if e = value(depth + 1); e != nil {
						return e
					}
				}
			} else {
				return errors.New("JSON格式无效")
			}
			_, e = d.Token()
			return e
		}
		return nil
	}
	if e := value(0); e != nil {
		return e
	}
	if _, e := d.Token(); e != io.EOF {
		return errors.New("JSON尾部无效")
	}
	return nil
}
func HTTPS(value string, base bool) (*url.URL, error) {
	if value == "" || len(value) > 4096 || strings.ContainsAny(value, "\\\r\n\t ") {
		return nil, errors.New("地址必须使用HTTPS")
	}
	u, e := url.Parse(value)
	if e != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" || u.Opaque != "" {
		return nil, errors.New("HTTPS地址无效")
	}
	if base && (u.RawQuery != "" || u.ForceQuery || (u.Path != "" && u.Path != "/")) {
		return nil, errors.New("登录地址只能包含HTTPS主机和端口")
	}
	return u, nil
}
func (c Config) Validate() error {
	if c.SchemaVersion != 2 || !serverPattern.MatchString(c.ServerID) || strings.TrimSpace(c.DisplayName) == "" || len([]rune(c.DisplayName)) > 80 {
		return errors.New("请通过GM后台生成此Go登录器的服务器配置")
	}
	u, e := HTTPS(c.LoginURL, true)
	if e != nil {
		return e
	}
	b, rest := pem.Decode([]byte(c.CertificatePEM))
	if b == nil || b.Type != "CERTIFICATE" || len(bytes.TrimSpace(rest)) != 0 {
		return errors.New("内置公开证书无效")
	}
	cert, e := x509.ParseCertificate(b.Bytes)
	if e != nil {
		return errors.New("证书格式无效")
	}
	if e = cert.VerifyHostname(u.Hostname()); e != nil {
		return errors.New("证书与登录服务器不匹配")
	}
	key, ok := cert.PublicKey.(*rsa.PublicKey)
	if !ok || key.N.BitLen() < 2048 || time.Now().Before(cert.NotBefore) || !time.Now().Before(cert.NotAfter) {
		return errors.New("公开证书无效、过期或密钥强度不足")
	}
	return nil
}
func LoadConfig(path string) (Config, error) {
	var c Config
	f, e := os.Open(path)
	if e != nil {
		return c, e
	}
	defer f.Close()
	s, e := f.Stat()
	if e != nil {
		return c, e
	}
	raw, _, e := launcherbundle.Read(f, s.Size(), launcherbundle.MaxConfig, launcherbundle.ConfigMagic)
	if e != nil {
		return c, errors.New("尚无内置服务器配置，请在GM后台生成Go登录器")
	}
	if e = StrictJSON(raw, &c); e != nil {
		return c, e
	}
	return c, c.Validate()
}
func (c Config) DataDirectory() string {
	sum := sha256.Sum256([]byte(c.ServerID + "|" + c.LoginURL))
	// The operator's workstation uses E:\codex. Players use a portable directory beside the launcher.
	base := filepath.Join(filepath.Dir(executablePath()), "data", "go-launcher")
	if s, e := os.Stat(`E:\codex`); e == nil && s.IsDir() {
		base = `E:\codex\登录器Go数据`
	}
	return filepath.Join(base, hex.EncodeToString(sum[:]))
}
func executablePath() string {
	p, e := os.Executable()
	if e != nil {
		return "."
	}
	return p
}
func FileHash(path string) (string, error) {
	f, e := os.Open(path)
	if e != nil {
		return "", e
	}
	defer f.Close()
	h := sha256.New()
	if _, e = io.Copy(h, f); e != nil {
		return "", e
	}
	return fmt.Sprintf("%x", h.Sum(nil)), nil
}
func NoLinks(path string) error {
	p, e := filepath.Abs(path)
	if e != nil {
		return e
	}
	for {
		if s, e := os.Lstat(p); e == nil {
			if s.Mode()&os.ModeSymlink != 0 {
				return errors.New("路径包含链接或目录联接")
			}
		} else if !os.IsNotExist(e) {
			return e
		}
		n := filepath.Dir(p)
		if n == p {
			return nil
		}
		p = n
	}
}
func Relative(name string) error {
	if name == "" || len(name) > 230 || strings.ContainsAny(name, "\\:\x00\r\n\t<>\"|?*") || strings.HasPrefix(name, "/") {
		return errors.New("更新文件路径无效")
	}
	reserved := regexp.MustCompile(`(?i)^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\.|$)`)
	for _, s := range strings.Split(name, "/") {
		if s == "" || s == "." || s == ".." || strings.HasSuffix(s, ".") || strings.HasSuffix(s, " ") || reserved.MatchString(s) {
			return errors.New("更新文件名无效")
		}
		for _, r := range s {
			if r < 32 || r == 127 {
				return errors.New("更新文件名含控制字符")
			}
		}
	}
	return nil
}
func Within(root, name string) (string, error) {
	if e := Relative(name); e != nil {
		return "", e
	}
	root, e := filepath.Abs(root)
	if e != nil {
		return "", e
	}
	p := filepath.Join(root, filepath.FromSlash(name))
	if e = NoLinks(p); e != nil {
		return "", e
	}
	return p, nil
}
func Durable(path string, b []byte) error {
	if e := NoLinks(path); e != nil {
		return e
	}
	f, e := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return e
	}
	if _, e = f.Write(b); e == nil {
		e = f.Sync()
	}
	closeErr := f.Close()
	if e == nil {
		e = closeErr
	}
	return e
}
