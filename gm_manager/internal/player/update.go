package player

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"github.com/bodgit/sevenzip"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
)

const maxDownload int64 = 32 << 30
const maxExpanded int64 = 64 << 30

func shaText(s string) string { return fmt.Sprintf("%x", sha256.Sum256([]byte(s))) }
func VerifySigned(raw json.RawMessage, certificate, kind string, out any) error {
	var env struct {
		Payload   string `json:"payload"`
		Signature string `json:"signature"`
	}
	if len(raw) > 24<<20 || StrictJSON(raw, &env) != nil {
		return errors.New("更新签名封装无效")
	}
	payload, e := base64.StdEncoding.DecodeString(env.Payload)
	if e != nil || len(payload) > 16<<20 {
		return errors.New("签名清单过大或无效")
	}
	sig, e := base64.StdEncoding.DecodeString(env.Signature)
	if e != nil {
		return errors.New("签名编码无效")
	}
	b, _ := pem.Decode([]byte(certificate))
	if b == nil {
		return errors.New("缺少签名公开证书")
	}
	cert, e := x509.ParseCertificate(b.Bytes)
	if e != nil {
		return errors.New("签名证书无效")
	}
	key, ok := cert.PublicKey.(*rsa.PublicKey)
	if !ok || key.N.BitLen() < 2048 {
		return errors.New("签名公钥不受支持")
	}
	sum := sha256.Sum256(payload)
	message := append([]byte("90US-UPDATE-V1\x00"+kind+"\x00"), sum[:]...)
	digest := sha256.Sum256(message)
	if rsa.VerifyPSS(key, crypto.SHA256, digest[:], sig, &rsa.PSSOptions{SaltLength: rsa.PSSSaltLengthEqualsHash}) != nil {
		return errors.New("更新签名校验失败，原客户端未修改")
	}
	return StrictJSON(payload, out)
}

type ResourceRelease struct {
	Version     string `json:"pvf_version"`
	SHA256      string `json:"file_sha256"`
	DownloadURL string `json:"download_url"`
	ClientURL   string `json:"client_download_url"`
}
type resourceFile struct {
	Path   string `json:"path"`
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
}
type resourceManifest struct {
	Schema    int            `json:"schema"`
	Version   string         `json:"version"`
	PvfSHA256 string         `json:"pvf_sha256"`
	ExeSHA256 string         `json:"exe_sha256"`
	Files     []resourceFile `json:"files"`
}
type archiveEntry struct {
	Name      string
	Size      uint64
	Directory bool
	Mode      os.FileMode
	Open      func() (io.ReadCloser, error)
}

func archiveFiles(path string) ([]archiveEntry, func() error, error) {
	f, e := os.Open(path)
	if e != nil {
		return nil, nil, e
	}
	magic := make([]byte, 6)
	_, _ = f.Read(magic)
	f.Close()
	var rows []archiveEntry
	if bytes.HasPrefix(magic, []byte{'P', 'K'}) {
		r, e := zip.OpenReader(path)
		if e != nil {
			return nil, nil, e
		}
		if len(r.File) > 100001 {
			r.Close()
			return nil, nil, errors.New("更新包文件过多")
		}
		for _, v := range r.File {
			rows = append(rows, archiveEntry{v.Name, v.UncompressedSize64, v.FileInfo().IsDir(), v.Mode(), v.Open})
		}
		return rows, r.Close, nil
	}
	if bytes.Equal(magic, []byte{0x37, 0x7a, 0xbc, 0xaf, 0x27, 0x1c}) {
		r, e := sevenzip.OpenReader(path)
		if e != nil {
			return nil, nil, e
		}
		if len(r.File) > 100001 {
			r.Close()
			return nil, nil, errors.New("更新包文件过多")
		}
		for _, v := range r.File {
			rows = append(rows, archiveEntry{v.Name, v.UncompressedSize, v.FileInfo().IsDir(), v.Mode(), v.Open})
		}
		return rows, r.Close, nil
	}
	return nil, nil, errors.New("更新包必须是ZIP或7z格式")
}
func (a *API) ApplyPatches(ctx context.Context, root string, stage Stage, progress func(string)) error {
	var signed struct {
		Revision int64   `json:"revision"`
		Items    []Patch `json:"items"`
	}
	if e := VerifySigned(stage.Patches.Signed, a.Config.CertificatePEM, "patches", &signed); e != nil {
		return e
	}
	if signed.Revision != stage.Patches.Revision || !reflect.DeepEqual(signed.Items, stage.Patches.Items) {
		return errors.New("补丁签名与当前发布不一致")
	}
	matches := func(p Patch) bool {
		path, e := Within(root, "us_extend_dll/"+p.FileName)
		if e != nil {
			return false
		}
		s, e := os.Stat(path)
		if e != nil || s.Size() != p.Bytes {
			return false
		}
		h, e := FileHash(path)
		return e == nil && h == p.SHA256
	}
	pending := []Patch{}
	for _, p := range stage.Patches.Items {
		if !matches(p) {
			pending = append(pending, p)
		}
	}
	if len(pending) == 0 {
		return nil
	}
	if ClientRunning(root) {
		return errors.New("请先退出游戏再更新补丁")
	}
	cache := filepath.Join(a.Config.DataDirectory(), "updates")
	job := filepath.Join(cache, "patch-"+ID())
	if e := NoLinks(job); e != nil {
		return e
	}
	if e := os.MkdirAll(job, 0700); e != nil {
		return e
	}
	files := map[string]string{}
	for _, p := range pending {
		progress("正在下载补丁：" + p.FileName)
		var d struct {
			Content string `json:"content_base64"`
			SHA256  string `json:"sha256"`
			Bytes   int64  `json:"bytes"`
		}
		if e := a.Request(ctx, "GET", "/launcher/v1/patches/"+p.SHA256, "", nil, &d); e != nil {
			return e
		}
		data, e := base64.StdEncoding.DecodeString(d.Content)
		if e != nil || int64(len(data)) != p.Bytes || fmt.Sprintf("%x", sha256.Sum256(data)) != p.SHA256 || d.Bytes != p.Bytes || d.SHA256 != p.SHA256 {
			return errors.New("补丁内容与签名摘要不一致")
		}
		path := filepath.Join(job, p.FileName)
		if e = Durable(path, data); e != nil {
			return e
		}
		files["us_extend_dll/"+p.FileName] = path
	}
	tx := Transaction{Root: root, Cache: cache, Running: func() bool { return ClientRunning(root) }, Check: ctx.Err}
	return tx.Apply(files, func() error {
		for _, p := range stage.Patches.Items {
			if !matches(p) {
				return errors.New("补丁安装后校验失败")
			}
		}
		return nil
	})
}
func (a *API) EnsureResources(ctx context.Context, root, launcher string, stage Stage, progress func(string)) error {
	root, e := filepath.Abs(root)
	if e != nil {
		return e
	}
	cache := filepath.Join(a.Config.DataDirectory(), "updates")
	tx := Transaction{Root: root, Cache: cache, Running: func() bool { return ClientRunning(root) }}
	if e = tx.Recover(); e != nil {
		return e
	}
	if e = a.ApplyPatches(ctx, root, stage, progress); e != nil {
		return e
	}
	exe := filepath.Join(root, "DFO.exe")
	pvf := filepath.Join(root, "Script.pvf")
	exeHash, _ := FileHash(exe)
	pvfHash, _ := FileHash(pvf)
	if exeHash == stage.ExeSHA256 && pvfHash == stage.PvfSHA256 {
		_, e = ValidateClient(root, stage)
		return e
	}
	if ClientRunning(root) {
		return errors.New("请先退出游戏再更新资源")
	}
	var d struct {
		Enabled bool            `json:"enabled"`
		Data    ResourceRelease `json:"data"`
	}
	if e = a.Request(ctx, "GET", "/launcher/v1/pvf/latest", "", nil, &d); e != nil {
		return e
	}
	r := d.Data
	if !d.Enabled || r.Version == "" || r.SHA256 != stage.PvfSHA256 {
		return errors.New("资源发布尚未就绪或版本发生变化")
	}
	address := r.DownloadURL
	if exeHash != stage.ExeSHA256 || address == "" {
		address = r.ClientURL
	}
	if address == "" {
		return errors.New("GM尚未设置配套更新包直链")
	}
	httpClient, e := a.DownloadHTTP(address)
	if e != nil {
		return e
	}
	defer httpClient.CloseIdleConnections()
	job := filepath.Join(cache, "resources-"+ID())
	if e = NoLinks(job); e != nil {
		return e
	}
	if e = os.MkdirAll(job, 0700); e != nil {
		return e
	}
	packagePath := filepath.Join(job, "download.pkg")
	progress("正在下载客户端更新包…")
	req, e := http.NewRequestWithContext(ctx, "GET", address, nil)
	if e != nil {
		return e
	}
	resp, e := httpClient.Do(req)
	if e != nil {
		return errors.New("更新下载连接失败")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 || resp.ContentLength > maxDownload {
		return errors.New("更新下载返回状态或大小无效")
	}
	out, e := os.OpenFile(packagePath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return e
	}
	n, e := io.Copy(out, io.LimitReader(resp.Body, maxDownload+1))
	if e == nil {
		e = out.Sync()
	}
	ce := out.Close()
	if e == nil {
		e = ce
	}
	if e != nil || n > maxDownload || (resp.ContentLength >= 0 && n != resp.ContentLength) {
		return errors.New("更新包下载不完整，原客户端未修改")
	}
	progress("正在验证数字签名及全部文件…")
	return a.InstallArchive(ctx, packagePath, job, root, launcher, r, stage, progress)
}
func (a *API) InstallArchive(ctx context.Context, path, job, root, launcher string, release ResourceRelease, stage Stage, progress func(string)) error {
	rows, close, e := archiveFiles(path)
	if e != nil {
		return e
	}
	defer close()
	var env []byte
	count := 0
	for _, v := range rows {
		if v.Name == ".90us-update.json" && !v.Directory {
			count++
			if v.Size > 24<<20 {
				return errors.New("签名清单过大")
			}
			r, e := v.Open()
			if e != nil {
				return e
			}
			env, e = io.ReadAll(io.LimitReader(r, 24<<20+1))
			r.Close()
			if e != nil || len(env) > 24<<20 {
				return errors.New("签名清单读取失败")
			}
		}
	}
	if count != 1 {
		return errors.New("更新包缺少唯一签名清单")
	}
	var m resourceManifest
	if e = VerifySigned(env, a.Config.CertificatePEM, "resources", &m); e != nil {
		return e
	}
	if m.Schema != 1 || m.Version != release.Version || m.PvfSHA256 != stage.PvfSHA256 || m.ExeSHA256 != stage.ExeSHA256 || len(m.Files) < 1 || len(m.Files) > 100000 {
		return errors.New("资源签名与当前服务器发布不一致")
	}
	approved := map[string]resourceFile{}
	var total int64
	for _, v := range m.Files {
		k := strings.ToLower(v.Path)
		if Relative(v.Path) != nil || v.Bytes < 0 || v.Bytes > maxExpanded || !hashPattern.MatchString(v.SHA256) || k == ".90us-update.json" {
			return errors.New("资源签名文件表无效")
		}
		if _, exists := approved[k]; exists {
			return errors.New("资源签名文件重名")
		}
		approved[k] = v
		total += v.Bytes
		if total > maxExpanded {
			return errors.New("解压总量超限")
		}
	}
	extracted := filepath.Join(job, "extracted")
	files := map[string]string{}
	seen := map[string]bool{}
	preserve := map[string]bool{}
	for _, p := range stage.Patches.Items {
		preserve[strings.ToLower("us_extend_dll/"+p.FileName)] = true
	}
	for _, v := range rows {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if v.Directory {
			if Relative(strings.TrimSuffix(v.Name, "/")) != nil {
				return errors.New("压缩包目录无效")
			}
			continue
		}
		if v.Name == ".90us-update.json" {
			continue
		}
		k := strings.ToLower(v.Name)
		row, ok := approved[k]
		if !ok || seen[k] || v.Name != row.Path || v.Size != uint64(row.Bytes) || v.Mode&os.ModeSymlink != 0 {
			return errors.New("更新含未批准、重复或链接文件")
		}
		seen[k] = true
		target, e := Within(extracted, v.Name)
		if e != nil {
			return e
		}
		if e = os.MkdirAll(filepath.Dir(target), 0700); e != nil {
			return e
		}
		input, e := v.Open()
		if e != nil {
			return e
		}
		output, e := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if e != nil {
			input.Close()
			return e
		}
		n, e := io.Copy(output, io.LimitReader(contextReader{ctx: ctx, input: input}, row.Bytes+1))
		input.Close()
		if e == nil {
			e = output.Sync()
		}
		ce := output.Close()
		if e == nil {
			e = ce
		}
		h, he := FileHash(target)
		if e != nil || he != nil || n != row.Bytes || h != row.SHA256 {
			return errors.New("资源文件校验失败，原客户端未修改")
		}
		original, e := Within(root, v.Name)
		if e != nil {
			return e
		}
		base := strings.ToLower(filepath.Base(v.Name))
		if strings.EqualFold(original, launcher) || base == "launcher.json" || base == "server-cert.pem" || base == "密钥与校验.json" {
			return errors.New("资源包不能覆盖登录器或信任配置")
		}
		if !preserve[k] {
			files[v.Name] = target
		}
	}
	if len(seen) != len(approved) {
		return errors.New("更新包缺少签名清单中的文件")
	}
	pvf, ok := approved["script.pvf"]
	if !ok || pvf.SHA256 != stage.PvfSHA256 {
		return errors.New("更新包缺少配套PVF")
	}
	exePath := filepath.Join(extracted, "DFO.exe")
	if _, e = os.Stat(exePath); os.IsNotExist(e) {
		exePath = filepath.Join(root, "DFO.exe")
	}
	h, e := FileHash(exePath)
	if e != nil || h != stage.ExeSHA256 {
		return errors.New("更新包缺少配套DFO.exe")
	}
	progress("签名与文件校验通过，正在安装…")
	if ctx.Err() != nil {
		return ctx.Err()
	}
	tx := Transaction{Root: root, Cache: filepath.Join(a.Config.DataDirectory(), "updates"), Running: func() bool { return ClientRunning(root) }, Check: ctx.Err}
	return tx.Apply(files, func() error { _, e := ValidateClient(root, stage); return e })
}

type contextReader struct {
	ctx   context.Context
	input io.Reader
}

func (r contextReader) Read(b []byte) (int, error) {
	if e := r.ctx.Err(); e != nil {
		return 0, e
	}
	return r.input.Read(b)
}
