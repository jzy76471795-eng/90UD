// Package downloader implements the portable player's download workflow.
package downloader

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"strings"
	"unicode"

	"newadmin/internal/launcherbundle"
)

const Magic = "PY90_DOWNLOADER_CONFIG_V1"
const MaxDownload = int64(200) << 30
const MaxExtract = int64(300) << 30

type Config struct {
	Version   int    `json:"version"`
	Name      string `json:"name"`
	URL       string `json:"url"`
	FileName  string `json:"file_name"`
	SHA256    string `json:"sha256,omitempty"`
	Size      int64  `json:"size,omitempty"`
	Extract   bool   `json:"extract_zip"`
	Launcher  string `json:"launcher,omitempty"`
	AutoStart bool   `json:"auto_start"`
}

func ValidateURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u == nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" || strings.ContainsAny(raw, "\r\n\x00") {
		return errors.New("下载地址必须为完整的 HTTPS 文件直链，不能包含登录密码或片段")
	}
	return nil
}

// SafeRelative applies Windows rules even when a package is produced elsewhere.
func SafeRelative(value string) (string, error) {
	v := strings.ReplaceAll(value, "\\", "/")
	if v == "" || strings.HasPrefix(v, "/") || path.Clean(v) != v || strings.ContainsAny(v, ":\x00") {
		return "", errors.New("路径必须是目录内的相对路径，不能包含 ..、盘符或特殊流")
	}
	for _, part := range strings.Split(v, "/") {
		if part == "" || part == "." || part == ".." || strings.TrimRight(part, " .") != part || strings.ContainsAny(part, "<>\"|?*") {
			return "", errors.New("路径包含无效文件名")
		}
		for _, r := range part {
			if unicode.IsControl(r) {
				return "", errors.New("路径包含控制字符")
			}
		}
		base := strings.ToUpper(strings.SplitN(part, ".", 2)[0])
		runes := []rune(base)
		if base == "CON" || base == "PRN" || base == "AUX" || base == "NUL" || base == "CONIN$" || base == "CONOUT$" || (len(runes) == 4 && (strings.HasPrefix(base, "COM") || strings.HasPrefix(base, "LPT")) && strings.ContainsRune("123456789¹²³", runes[3])) {
			return "", errors.New("路径包含 Windows 保留名称")
		}
	}
	return filepath.FromSlash(v), nil
}

func (c Config) Validate() error {
	if c.Version != 1 {
		return errors.New("下载器配置版本无效")
	}
	if strings.TrimSpace(c.Name) == "" || len([]rune(c.Name)) > 64 {
		return errors.New("显示名称需要 1 至 64 个字符")
	}
	for _, r := range c.Name {
		if unicode.IsControl(r) {
			return errors.New("显示名称包含控制字符")
		}
	}
	if err := ValidateURL(c.URL); err != nil {
		return err
	}
	name, err := SafeRelative(c.FileName)
	if err != nil || filepath.Base(name) != name {
		return errors.New("下载文件名必须是单个有效文件名")
	}
	if strings.HasPrefix(c.FileName, ".") || strings.EqualFold(c.FileName, "客户端") {
		return errors.New("下载文件名不能使用内部工作目录名称")
	}
	if c.SHA256 != "" {
		if b, err := hex.DecodeString(c.SHA256); err != nil || len(b) != 32 {
			return errors.New("客户端摘要格式无效")
		}
	}
	if c.Size < 0 || c.Size > MaxDownload {
		return errors.New("文件大小超出范围（最大 200 GB）")
	}
	if c.Extract && !strings.EqualFold(filepath.Ext(c.FileName), ".zip") {
		return errors.New("自动解压支持 ZIP；RAR、7Z 等格式请选择仅下载")
	}
	if c.Launcher != "" {
		if _, err := SafeRelative(c.Launcher); err != nil || !strings.EqualFold(filepath.Ext(c.Launcher), ".exe") {
			return errors.New("登录器路径须是 ZIP 内的相对 EXE 路径")
		}
		if !c.Extract {
			return errors.New("启动登录器需要先启用 ZIP 自动解压")
		}
	}
	if c.AutoStart && c.Launcher == "" {
		return errors.New("自动启动需要填写 ZIP 内的登录器路径")
	}
	return nil
}

func LoadExecutable(filename string) (Config, error) {
	f, err := os.Open(filename)
	if err != nil {
		return Config{}, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return Config{}, err
	}
	b, _, err := launcherbundle.Read(f, info.Size(), 64<<10, Magic)
	if err != nil {
		return Config{}, errors.New("下载器缺少有效配置，请在 GM 后台重新生成")
	}
	var c Config
	if err := json.Unmarshal(b, &c); err != nil {
		return c, fmt.Errorf("下载器配置损坏：%w", err)
	}
	return c, c.Validate()
}
