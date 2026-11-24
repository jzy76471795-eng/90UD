package main

import (
	"bytes"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"debug/buildinfo"
	"debug/pe"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"newadmin/internal/launcherbundle"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

type LauncherPackageOptions struct {
	TemplatePath, TemplateSHA256, DisplayName, ServerID  string
	LoginURL, CertificatePath, CertificatePEM, OutputDir string
	DirectOutput                                         bool
	RequireGo                                            bool
}

type LauncherPackageResult struct {
	Directory, LauncherPath, TemplateSHA256, LauncherSHA256 string
	BackupDirectory                                         string
}

type LauncherTemplate struct {
	SchemaVersion         int
	Path, SHA256, ExeFile string
}

type launcherPublicConfig struct {
	SchemaVersion  int    `json:"schema_version"`
	ServerID       string `json:"server_id"`
	DisplayName    string `json:"display_name"`
	LoginURL       string `json:"login_url"`
	CertificatePEM string `json:"certificate_pem"`
}

var launcherServerID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`)

func normalizeLauncherHost(value string) (string, net.IP, error) {
	host := strings.TrimSpace(value)
	if ip := net.ParseIP(host); ip != nil {
		return ip.String(), ip, nil
	}
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	if host == "" || len(host) > 253 {
		return "", nil, errors.New("请填写一个服务器 IP 或 ASCII/punycode 域名")
	}
	labels := strings.Split(host, ".")
	numeric := len(labels) == 4
	for _, label := range labels {
		if label == "" || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
			return "", nil, errors.New("服务器域名格式无效")
		}
		for _, ch := range label {
			if !(ch >= 'a' && ch <= 'z' || ch >= '0' && ch <= '9' || ch == '-') {
				return "", nil, errors.New("服务器地址只能包含 IP 或 ASCII/punycode 域名，不含协议、端口、路径或通配符")
			}
			if ch < '0' || ch > '9' {
				numeric = false
			}
		}
	}
	if numeric {
		return "", nil, errors.New("IPv4 地址无效")
	}
	return host, nil, nil
}

func validateLauncherURL(value string) (string, string, error) {
	if value == "" || strings.TrimSpace(value) != value || strings.ContainsAny(value, "\\\r\n\t") {
		return "", "", errors.New("请填写 HTTPS 主机与端口，不含路径、参数或账号")
	}
	u, err := url.Parse(value)
	if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.Opaque != "" || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.RawFragment != "" || (u.Path != "" && u.Path != "/") || u.RawPath != "" {
		return "", "", errors.New("登录地址必须为 HTTPS 主机与端口，不含路径、参数或账号")
	}
	host, ip, err := normalizeLauncherHost(u.Hostname())
	if err != nil {
		return "", "", err
	}
	port := u.Port()
	if strings.HasSuffix(u.Host, ":") {
		return "", "", errors.New("HTTPS 端口无效")
	}
	if port != "" {
		n, err := strconv.Atoi(port)
		if err != nil || n < 1 || n > 65535 {
			return "", "", errors.New("HTTPS 端口必须为 1 至 65535")
		}
	}
	authority := host
	if ip != nil && strings.Contains(host, ":") {
		authority = "[" + host + "]"
	}
	if port != "" {
		authority = net.JoinHostPort(host, port)
	}
	return "https://" + authority, host, nil
}

func readLauncherCertificate(path, host string) ([]byte, error) {
	b, err := readLauncherLimited(path, 128*1024)
	if err != nil {
		return nil, fmt.Errorf("读取公开证书失败: %w", err)
	}
	return validateLauncherCertificate(b, host)
}

func validateLauncherCertificate(b []byte, host string) ([]byte, error) {
	if len(b) > 128*1024 {
		return nil, errors.New("公开证书过大")
	}
	trimmed := bytes.TrimSpace(b)
	if !bytes.HasPrefix(trimmed, []byte("-----BEGIN CERTIFICATE-----")) {
		return nil, errors.New("请选择只含单个 CERTIFICATE 的公开 PEM 证书")
	}
	block, rest := pem.Decode(trimmed)
	if block == nil || block.Type != "CERTIFICATE" || len(block.Headers) != 0 || len(bytes.TrimSpace(rest)) != 0 {
		return nil, errors.New("公开证书必须只有一个 CERTIFICATE；禁止包含私钥或其他内容")
	}
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("证书格式无效: %w", err)
	}
	key, ok := cert.PublicKey.(*rsa.PublicKey)
	if !ok || key.N.BitLen() < 2048 {
		return nil, errors.New("公开证书必须使用至少 2048 位 RSA 公钥")
	}
	switch cert.SignatureAlgorithm {
	case x509.SHA256WithRSA, x509.SHA384WithRSA, x509.SHA512WithRSA:
	default:
		return nil, errors.New("公开证书必须使用 RSA SHA256/SHA384/SHA512 签名")
	}
	now := time.Now().UTC()
	if now.Before(cert.NotBefore) || !now.Before(cert.NotAfter) {
		return nil, errors.New("公开证书尚未生效或已经过期")
	}
	serverAuth := false
	for _, usage := range cert.ExtKeyUsage {
		if usage == x509.ExtKeyUsageServerAuth {
			serverAuth = true
		}
	}
	if !serverAuth {
		return nil, errors.New("公开证书缺少 ServerAuth 用途")
	}
	match := false
	if ip := net.ParseIP(host); ip != nil {
		for _, san := range cert.IPAddresses {
			if san.Equal(ip) {
				match = true
			}
		}
	} else {
		for _, san := range cert.DNSNames {
			if !strings.Contains(san, "*") && strings.EqualFold(san, host) {
				match = true
			}
		}
	}
	if !match {
		return nil, errors.New("公开证书 SAN 与登录地址主机不一致；不接受通配符或仅 CommonName")
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw}), nil
}

func launcherExecutableName(displayName string) (string, error) {
	if !utf8.ValidString(displayName) || utf8.RuneCountInString(displayName) > 80 {
		return "", errors.New("登陆器名称必须为 1 至 80 个字符")
	}
	for _, r := range displayName {
		if unicode.IsControl(r) {
			return "", errors.New("登陆器名称不能含控制字符")
		}
	}
	name := strings.TrimSpace(displayName)
	if strings.HasSuffix(strings.ToLower(name), ".exe") {
		name = name[:len(name)-4]
	}
	if name == "" {
		return "", errors.New("请填写登陆器名称，不能只有 .exe")
	}
	if strings.ContainsAny(name, `<>:"/\|?*`) || strings.HasSuffix(name, ".") || strings.HasSuffix(name, " ") {
		return "", errors.New(`登陆器名称不能包含 \ / : * ? " < > |，且不能以空格或句点结尾`)
	}
	device := strings.ToUpper(strings.TrimSpace(strings.SplitN(name, ".", 2)[0]))
	switch device {
	case "CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$":
		return "", errors.New("登陆器名称不能使用 Windows 保留名称")
	}
	for _, prefix := range []string{"COM", "LPT"} {
		if number, ok := strings.CutPrefix(device, prefix); ok && len([]rune(number)) == 1 && strings.ContainsAny(number, "123456789¹²³") {
			return "", errors.New("登陆器名称不能使用 Windows 保留名称")
		}
	}
	return name + ".exe", nil
}

// BuildLauncherPackage embeds public configuration into one pinned executable.
// No local application settings, PVF key, certificate private key or credentials
// are read or included in the player package.
func BuildLauncherPackage(o LauncherPackageOptions) (*LauncherPackageResult, error) {
	if o.RequireGo {
		if err := verifyGoPlayerTemplate(o.TemplatePath); err != nil {
			return nil, err
		}
	}
	if !launcherServerID.MatchString(o.ServerID) {
		return nil, errors.New("服务器标识只允许 1 至 64 位英文字母、数字、下划线或连字符，首位须为字母或数字")
	}
	exeName, err := launcherExecutableName(o.DisplayName)
	if err != nil {
		return nil, err
	}
	loginURL, host, err := validateLauncherURL(o.LoginURL)
	if err != nil {
		return nil, err
	}
	pin, err := launcherPin(o.TemplateSHA256)
	if err != nil {
		return nil, err
	}
	var cert []byte
	if o.CertificatePEM != "" {
		cert, err = validateLauncherCertificate([]byte(o.CertificatePEM), host)
	} else {
		cert, err = readLauncherCertificate(o.CertificatePath, host)
	}
	if err != nil {
		return nil, err
	}
	var destination, stage string
	if o.DirectOutput {
		destination, stage, err = flatOutputStage(o.OutputDir, ".launcher-stage-")
	} else {
		destination, stage, err = launcherStage(o.OutputDir)
	}
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(stage)
	imageNames, err := stageLauncherImages(o.TemplatePath, stage)
	if err != nil {
		return nil, err
	}
	if err := copyPinnedLauncher(o.TemplatePath, filepath.Join(stage, exeName), pin); err != nil {
		return nil, err
	}
	runtimeManifest, err := stageLauncherRuntime(o.TemplatePath, filepath.Join(stage, exeName), stage)
	if err != nil {
		return nil, err
	}
	config, err := launcherJSON(launcherPublicConfig{2, o.ServerID, o.DisplayName, loginURL, string(cert)})
	if err != nil {
		return nil, err
	}
	executable := filepath.Join(stage, exeName)
	stream, err := os.OpenFile(executable, os.O_WRONLY|os.O_APPEND, 0)
	if err != nil {
		return nil, err
	}
	err = launcherbundle.Append(stream, config, launcherbundle.ConfigMagic)
	if err == nil {
		err = stream.Sync()
	}
	closeErr := stream.Close()
	if err != nil {
		return nil, err
	}
	if closeErr != nil {
		return nil, closeErr
	}
	sum, err := hashLauncherFile(executable)
	if err != nil {
		return nil, err
	}
	backup := ""
	if o.DirectOutput {
		rollbackRuntime, runtimeErr := installLauncherRuntime(stage, destination, runtimeManifest)
		if runtimeErr != nil {
			return nil, runtimeErr
		}
		rollbackImages, imageErr := installLauncherImages(stage, destination, imageNames)
		if imageErr != nil {
			rollbackRuntime()
			return nil, imageErr
		}
		backup, err = commitFlatOutput(stage, destination, []string{exeName})
		if err != nil {
			rollbackImages()
			rollbackRuntime()
		}
	} else {
		err = commitLauncherDirectory(stage, destination)
	}
	if err != nil {
		return nil, fmt.Errorf("保存登录器失败: %w", err)
	}
	return &LauncherPackageResult{Directory: destination, LauncherPath: filepath.Join(destination, exeName), TemplateSHA256: pin, LauncherSHA256: sum, BackupDirectory: backup}, nil
}
func verifyGoPlayerTemplate(path string) error {
	info, e := buildinfo.ReadFile(path)
	if e != nil || info.Path != "newadmin/cmd/player-launcher" {
		return errors.New("此检查要求配套Go重写模板；GM默认使用原版玩家登录器模板")
	}
	f, e := pe.Open(path)
	if e != nil {
		return e
	}
	defer f.Close()
	h, ok := f.OptionalHeader.(*pe.OptionalHeader32)
	if !ok || f.Machine != pe.IMAGE_FILE_MACHINE_I386 || h.DataDirectory[14].VirtualAddress != 0 {
		return errors.New("玩家模板必须为原生Go x86程序")
	}
	return nil
}

func LoadLauncherTemplate(gmDir string) (*LauncherTemplate, error) {
	root, err := filepath.Abs(gmDir)
	if err != nil {
		return nil, err
	}
	manifest := filepath.Join(root, "登录器模板", "清单.json")
	if err := launcherNoLinks(manifest); err != nil {
		return nil, err
	}
	b, err := readLauncherLimited(manifest, 64<<10)
	if err != nil {
		return nil, fmt.Errorf("读取固定登录器模板清单失败: %w", err)
	}
	fields, err := launcherStrictObject(b)
	if err != nil {
		return nil, err
	}
	if len(fields) != 3 {
		return nil, errors.New("模板清单只能包含 schema_version、exe_file、sha256")
	}
	var version int
	var name, pin string
	if err := json.Unmarshal(fields["schema_version"], &version); err != nil || version != 2 {
		return nil, errors.New("登录器模板清单版本无效")
	}
	if err := json.Unmarshal(fields["exe_file"], &name); err != nil {
		return nil, errors.New("模板 exe_file 无效")
	}
	if err := json.Unmarshal(fields["sha256"], &pin); err != nil {
		return nil, errors.New("模板 sha256 无效")
	}
	name = strings.ReplaceAll(name, "\\", "/")
	if name == "" || strings.Contains(name, ":") || strings.HasPrefix(name, "/") || filepath.IsAbs(name) || filepath.VolumeName(name) != "" {
		return nil, errors.New("模板必须是 GM 目录内的相对 EXE 路径")
	}
	for _, part := range strings.Split(name, "/") {
		if part == "" || part == "." || part == ".." {
			return nil, errors.New("模板路径不能含空段、. 或 ..")
		}
	}
	if !strings.EqualFold(filepath.Ext(name), ".exe") {
		return nil, errors.New("模板必须为 EXE 文件")
	}
	path := filepath.Join(root, filepath.FromSlash(name))
	rel, err := filepath.Rel(root, path)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return nil, errors.New("模板路径越出 GM 目录")
	}
	if err := launcherNoLinks(path); err != nil {
		return nil, err
	}
	pin, err = launcherPin(pin)
	if err != nil {
		return nil, err
	}
	actual, err := hashLauncherFile(path)
	if err != nil {
		return nil, err
	}
	if actual != pin {
		return nil, errors.New("登录器模板 SHA256 与固定清单不一致")
	}
	return &LauncherTemplate{1, path, pin, name}, nil
}

func launcherPin(value string) (string, error) {
	if len(value) != 64 {
		return "", errors.New("模板 SHA256 必须为 64 位十六进制")
	}
	b, err := hex.DecodeString(value)
	if err != nil || len(b) != 32 {
		return "", errors.New("模板 SHA256 无效")
	}
	return strings.ToLower(value), nil
}

func launcherStage(output string) (string, string, error) {
	if strings.TrimSpace(output) == "" {
		return "", "", errors.New("请指定一个尚不存在的输出目录")
	}
	destination, err := filepath.Abs(output)
	if err != nil {
		return "", "", err
	}
	if _, err := os.Lstat(destination); err == nil {
		return "", "", errors.New("输出目录已存在，拒绝覆盖；请使用新目录")
	} else if !os.IsNotExist(err) {
		return "", "", err
	}
	parent := filepath.Dir(destination)
	if err := os.MkdirAll(parent, 0700); err != nil {
		return "", "", err
	}
	if err := launcherNoLinks(parent); err != nil {
		return "", "", err
	}
	stage, err := os.MkdirTemp(parent, ".launcher-stage-")
	if err != nil {
		return "", "", err
	}
	if err := protectLauncherPath(stage, true); err != nil {
		os.RemoveAll(stage)
		return "", "", fmt.Errorf("限制输出目录权限失败: %w", err)
	}
	return destination, stage, nil
}

func launcherNoLinks(path string) error {
	path, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	for {
		info, err := os.Lstat(path)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return errors.New("路径不能经过符号链接或目录联接")
		}
		parent := filepath.Dir(path)
		if parent == path {
			break
		}
		path = parent
	}
	return nil
}

func writeLauncherFile(path string, b []byte) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	if err := protectLauncherPath(path, false); err != nil {
		return err
	}
	if _, err := f.Write(b); err != nil {
		return err
	}
	if err := f.Sync(); err != nil {
		return err
	}
	return f.Close()
}

func readLauncherLimited(path string, limit int64) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() > limit {
		return nil, errors.New("文件不是普通文件或超过大小限制")
	}
	b, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, errors.New("文件超过大小限制")
	}
	return b, nil
}

func launcherJSON(v any) ([]byte, error) {
	b, err := json.MarshalIndent(v, "", "  ")
	return append(b, '\n'), err
}

func hashLauncherFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", errors.New("模板或输出必须为普通文件")
	}
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func copyPinnedLauncher(src, dst, pin string) error {
	if err := launcherNoLinks(src); err != nil {
		return fmt.Errorf("模板路径无效: %w", err)
	}
	f, err := os.Open(src)
	if err != nil {
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return errors.New("模板必须是普通 EXE 文件")
	}
	var signature [2]byte
	if _, err := io.ReadFull(f, signature[:]); err != nil || string(signature[:]) != "MZ" {
		return errors.New("登录器模板不是 MZ EXE 文件")
	}
	if _, err := f.Seek(0, 0); err != nil {
		return err
	}
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	defer out.Close()
	if err := protectLauncherPath(dst, false); err != nil {
		return err
	}
	h := sha256.New()
	if _, err := io.Copy(io.MultiWriter(out, h), f); err != nil {
		return err
	}
	if hex.EncodeToString(h.Sum(nil)) != pin {
		return errors.New("登录器模板 SHA256 不匹配，拒绝生成")
	}
	if err := out.Sync(); err != nil {
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	copied, err := hashLauncherFile(dst)
	if err != nil {
		return err
	}
	if copied != pin {
		return errors.New("登录器副本 SHA256 自检失败")
	}
	return nil
}

func launcherStrictObject(b []byte) (map[string]json.RawMessage, error) {
	d := json.NewDecoder(bytes.NewReader(b))
	token, err := d.Token()
	if err != nil || token != json.Delim('{') {
		return nil, errors.New("模板清单必须为 JSON 对象")
	}
	fields := make(map[string]json.RawMessage)
	for d.More() {
		token, err := d.Token()
		if err != nil {
			return nil, err
		}
		key, ok := token.(string)
		if !ok {
			return nil, errors.New("模板字段无效")
		}
		if _, exists := fields[key]; exists {
			return nil, errors.New("模板清单包含重复字段")
		}
		var raw json.RawMessage
		if err := d.Decode(&raw); err != nil {
			return nil, err
		}
		fields[key] = raw
	}
	if _, err := d.Token(); err != nil {
		return nil, err
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return nil, errors.New("模板清单含尾随内容")
	}
	return fields, nil
}
