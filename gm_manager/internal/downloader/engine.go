package downloader

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type Progress struct {
	Stage       string
	Done, Total int64
}
type Result struct{ Archive, Directory, Launcher string }
type Engine struct {
	Client     *http.Client
	Attempts   int
	RetryDelay time.Duration
}

type downloadStatusError struct{ code int }

func (e *downloadStatusError) Error() string {
	return fmt.Sprintf("下载服务器返回 HTTP %d，请检查文件直链", e.code)
}

func NoLinks(path string) error {
	full, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	for {
		info, e := os.Lstat(full)
		if e == nil && (info.Mode()&os.ModeSymlink != 0 || info.Mode()&os.ModeIrregular != 0) {
			return errors.New("目标路径不能包含链接或重解析点")
		}
		if e != nil && !os.IsNotExist(e) {
			return e
		}
		parent := filepath.Dir(full)
		if parent == full {
			break
		}
		full = parent
	}
	return nil
}
func emit(fn func(Progress), stage string, done, total int64) {
	if fn != nil {
		fn(Progress{stage, done, total})
	}
}
func Hash(ctx context.Context, path string) (string, error) {
	f, e := os.Open(path)
	if e != nil {
		return "", e
	}
	defer f.Close()
	h := sha256.New()
	b := make([]byte, 1<<20)
	for {
		if e = ctx.Err(); e != nil {
			return "", e
		}
		n, err := f.Read(b)
		h.Write(b[:n])
		if err == io.EOF {
			break
		}
		if err != nil {
			return "", err
		}
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
func (e Engine) Run(ctx context.Context, c Config, directory string, report func(Progress)) (Result, error) {
	var result Result
	if err := c.Validate(); err != nil {
		return result, err
	}
	if err := ctx.Err(); err != nil {
		return result, err
	}
	root, err := filepath.Abs(directory)
	if err != nil {
		return result, err
	}
	if err = NoLinks(root); err != nil {
		return result, err
	}
	if err = os.MkdirAll(root, 0700); err != nil {
		return result, err
	}
	release, err := lockDirectory(root)
	if err != nil {
		return result, err
	}
	defer release()
	result.Archive = filepath.Join(root, c.FileName)
	if err = NoLinks(result.Archive); err != nil {
		return result, err
	}
	if _, err = os.Stat(result.Archive); err == nil {
		if c.SHA256 != "" {
			emit(report, "校验已有文件", 0, 0)
			sum, err := Hash(ctx, result.Archive)
			if err != nil {
				return result, err
			}
			if !strings.EqualFold(sum, c.SHA256) {
				return result, errors.New("目录内有同名且内容不同的文件，请选择其他目录或移走旧文件")
			}
		} else {
			emit(report, "使用已有文件", 0, 0)
		}
	} else if !os.IsNotExist(err) {
		return result, err
	} else {
		partID := strings.ToLower(c.SHA256)
		if partID == "" {
			partID = fmt.Sprintf("%x", sha256.Sum256([]byte(c.URL+"\n"+c.FileName)))
		}
		part := filepath.Join(root, ".下载-"+partID+".part")
		if err = NoLinks(part); err != nil {
			return result, err
		}
		attempts := e.Attempts
		if attempts <= 0 {
			attempts = 5
		}
		client := e.Client
		if client == nil {
			tr := http.DefaultTransport.(*http.Transport).Clone()
			tr.Proxy = downloadProxy
			tr.ResponseHeaderTimeout = 30 * time.Second
			tr.DisableCompression = true
			client = &http.Client{Transport: tr}
			defer tr.CloseIdleConnections()
		}
		// Clone so redirect policy never mutates a caller's HTTP client.
		safeClient := *client
		attemptConfig := c
		safeClient.CheckRedirect = func(req *http.Request, via []*http.Request) error {
			if len(via) >= 10 {
				return errors.New("下载链接重定向过多")
			}
			if err := ValidateURL(req.URL.String()); err != nil {
				return err
			}
			if len(via) > 0 && via[len(via)-1].URL.Scheme == "https" && req.URL.Scheme != "https" {
				return errors.New("下载地址不能从 HTTPS 降级到 HTTP")
			}
			// Keep resolved addresses only for this run. An interrupted transfer
			// should not depend on the redirect entry point being available again.
			attemptConfig.URL = req.URL.String()
			return nil
		}
		for attempt := 1; attempt <= attempts; attempt++ {
			emit(report, "正在连接下载服务器…", 0, 0)
			err = downloadAttempt(ctx, &safeClient, attemptConfig, part, report)
			if err == nil && c.SHA256 != "" {
				emit(report, "校验 SHA256", 0, 0)
				var sum string
				sum, err = Hash(ctx, part)
				if err == nil && !strings.EqualFold(sum, c.SHA256) {
					err = errors.New("SHA256 不匹配，已丢弃损坏文件")
					if removeErr := os.Remove(part); removeErr != nil {
						return result, removeErr
					}
				}
			}
			if err == nil {
				break
			}
			if ctx.Err() != nil {
				return result, ctx.Err()
			}
			var statusError *downloadStatusError
			if errors.As(err, &statusError) {
				// Signed addresses can expire; resolve the original link again.
				attemptConfig.URL = c.URL
			}
			if attempt < attempts {
				emit(report, fmt.Sprintf("下载中断，正在重试（%d/%d）：%s", attempt+1, attempts, err), 0, 0)
				delay := e.RetryDelay
				if delay <= 0 {
					delay = time.Second
				}
				select {
				case <-ctx.Done():
					return result, ctx.Err()
				case <-time.After(delay):
				}
			}
		}
		if err != nil {
			return result, err
		}
		if _, err = os.Lstat(result.Archive); !os.IsNotExist(err) {
			return result, errors.New("目标文件已出现，未覆盖，请选择其他目录")
		}
		if err = os.Rename(part, result.Archive); err != nil {
			return result, err
		}
	}
	if c.Extract {
		result.Directory, result.Launcher, err = extract(ctx, result.Archive, root, c.Launcher, report)
		if err != nil {
			return result, err
		}
	} else {
		result.Directory = root
	}
	emit(report, "完成", 1, 1)
	return result, nil
}
func downloadAttempt(ctx context.Context, client *http.Client, c Config, part string, report func(Progress)) error {
	f, err := os.OpenFile(part, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	offset := info.Size()
	if offset > MaxDownload || (c.Size > 0 && offset > c.Size) {
		if err = f.Truncate(0); err != nil {
			return err
		}
		offset = 0
	}
	if c.Size > 0 && offset == c.Size {
		return nil
	}
	attempt, cancel := context.WithCancel(ctx)
	defer cancel()
	idle := time.AfterFunc(45*time.Second, cancel)
	defer idle.Stop()
	req, err := http.NewRequestWithContext(attempt, http.MethodGet, c.URL, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept-Encoding", "identity")
	if offset > 0 {
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-", offset))
	}
	resp, err := client.Do(req)
	if err != nil {
		var requestError *url.Error
		if errors.As(err, &requestError) {
			err = requestError.Err
		}
		return fmt.Errorf("无法连接下载服务器：%w", err)
	}
	defer resp.Body.Close()
	total := resp.ContentLength
	switch resp.StatusCode {
	case http.StatusOK:
		if err = f.Truncate(0); err != nil {
			return err
		}
		offset = 0
	case http.StatusPartialContent:
		var start, end, size int64
		if _, err = fmt.Sscanf(resp.Header.Get("Content-Range"), "bytes %d-%d/%d", &start, &end, &size); err != nil || start != offset || end < start || size <= end || size > MaxDownload || resp.ContentLength >= 0 && resp.ContentLength != end-start+1 {
			return errors.New("服务器返回的断点续传范围无效")
		}
		total = size
	case http.StatusRequestedRangeNotSatisfiable:
		size, err := strconv.ParseInt(strings.TrimPrefix(resp.Header.Get("Content-Range"), "bytes */"), 10, 64)
		if err == nil && offset == size && size > 0 {
			return nil
		}
		if err = f.Truncate(0); err != nil {
			return err
		}
		return errors.New("下载文件已变化，重新下载")
	default:
		return &downloadStatusError{code: resp.StatusCode}
	}
	if resp.Header.Get("Content-Encoding") != "" && resp.Header.Get("Content-Encoding") != "identity" {
		return errors.New("下载服务器返回不支持的内容编码")
	}
	if total > MaxDownload || c.Size > 0 && total > 0 && total != c.Size {
		return errors.New("远程文件大小与发布配置不一致")
	}
	if total <= 0 && c.Size > 0 {
		total = c.Size
	}
	if _, err = f.Seek(offset, io.SeekStart); err != nil {
		return err
	}
	done := offset
	b := make([]byte, 256<<10)
	last := time.Time{}
	for {
		if err = ctx.Err(); err != nil {
			return err
		}
		n, readErr := resp.Body.Read(b)
		if n > 0 {
			idle.Reset(45 * time.Second)
			done += int64(n)
			if done > MaxDownload || total > 0 && done > total {
				return errors.New("下载文件超出大小限制")
			}
			if _, err = f.Write(b[:n]); err != nil {
				return err
			}
			if time.Since(last) > 100*time.Millisecond {
				emit(report, "正在下载", done, total)
				last = time.Now()
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			return errors.New("下载中断，将从已下载位置重试")
		}
	}
	if total > 0 && done != total {
		return errors.New("下载未完整，将继续重试")
	}
	return f.Sync()
}
func extract(ctx context.Context, archive, root, launcher string, report func(Progress)) (directory, launch string, err error) {
	z, err := zip.OpenReader(archive)
	if err != nil {
		return "", "", errors.New("客户端不是有效 ZIP 文件")
	}
	defer z.Close()
	if len(z.File) > 100000 {
		return "", "", errors.New("ZIP 文件数量超限")
	}
	stage, err := os.MkdirTemp(root, ".解压-")
	if err != nil {
		return "", "", err
	}
	defer func() {
		if stage != "" {
			os.RemoveAll(stage)
		}
	}()
	var total uint64
	seen := map[string]bool{}
	for _, f := range z.File {
		name := strings.TrimSuffix(strings.ReplaceAll(f.Name, "\\", "/"), "/")
		rel, e := SafeRelative(name)
		if e != nil {
			return "", "", e
		}
		key := strings.ToLower(rel)
		if seen[key] {
			return "", "", errors.New("ZIP 包含重名文件")
		}
		seen[key] = true
		if f.Mode()&os.ModeSymlink != 0 || !f.FileInfo().IsDir() && !f.Mode().IsRegular() {
			return "", "", errors.New("ZIP 包含不支持的链接或特殊文件")
		}
		if f.UncompressedSize64 > uint64(MaxExtract)-total {
			return "", "", errors.New("解压大小超过 300 GB")
		}
		total += f.UncompressedSize64
	}
	var done int64
	b := make([]byte, 256<<10)
	last := time.Time{}
	for _, f := range z.File {
		if err = ctx.Err(); err != nil {
			return "", "", err
		}
		rel, _ := SafeRelative(strings.TrimSuffix(strings.ReplaceAll(f.Name, "\\", "/"), "/"))
		target := filepath.Join(stage, rel)
		if f.FileInfo().IsDir() {
			if err = os.MkdirAll(target, 0700); err != nil {
				return "", "", err
			}
			continue
		}
		if err = os.MkdirAll(filepath.Dir(target), 0700); err != nil {
			return "", "", err
		}
		input, e := f.Open()
		if e != nil {
			return "", "", e
		}
		out, e := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if e != nil {
			input.Close()
			return "", "", e
		}
		var written uint64
		for {
			if e = ctx.Err(); e != nil {
				break
			}
			var n int
			n, e = input.Read(b)
			if n > 0 {
				written += uint64(n)
				if written > f.UncompressedSize64 {
					e = errors.New("ZIP 解压大小无效")
					break
				}
				_, err = out.Write(b[:n])
				if err != nil {
					e = err
					break
				}
				done += int64(n)
				if time.Since(last) > 100*time.Millisecond {
					emit(report, "正在解压", done, int64(total))
					last = time.Now()
				}
			}
			if e != nil {
				break
			}
		}
		closeErr := out.Close()
		input.Close()
		if e != io.EOF {
			return "", "", e
		}
		if closeErr != nil {
			return "", "", closeErr
		}
		if written != f.UncompressedSize64 {
			return "", "", errors.New("ZIP 文件不完整")
		}
	}
	if launcher != "" {
		rel, _ := SafeRelative(launcher)
		info, e := os.Stat(filepath.Join(stage, rel))
		if e != nil || !info.Mode().IsRegular() {
			return "", "", errors.New("ZIP 中找不到配置的登录器，请联系管理员核对相对路径")
		}
	}
	// Always publish a new directory; never merge over a player's existing client.
	directory = filepath.Join(root, "客户端-"+strings.TrimPrefix(filepath.Base(stage), ".解压-"))
	if err = os.Rename(stage, directory); err != nil {
		return "", "", err
	}
	stage = ""
	if launcher != "" {
		rel, _ := SafeRelative(launcher)
		launch = filepath.Join(directory, rel)
	}
	return directory, launch, nil
}
