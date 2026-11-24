// Package launcherruntime verifies the private desktop runtime shared by player launchers.
package launcherruntime

import (
	"bytes"
	"crypto/sha256"
	"dnf90manager/internal/launcherbundle"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

const Magic = "PY90_EXTERNAL_RUNTIME_V1"
const MaxManifest = 2 << 20

type File struct {
	Path   string `json:"path"`
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
}
type Manifest struct {
	SchemaVersion int    `json:"schema_version"`
	Version       string `json:"version"`
	Directory     string `json:"directory"`
	Files         []File `json:"files"`
}

var safePath = regexp.MustCompile(`^[A-Za-z0-9_./-]+$`)
var version = regexp.MustCompile(`^[68]\.[0-9]+\.[0-9]+$`)
var digest = regexp.MustCompile(`^[a-f0-9]{64}$`)

func (m *Manifest) Validate() error {
	if m.SchemaVersion != 1 || !version.MatchString(m.Version) || len(m.Files) < 1 || len(m.Files) > 4096 {
		return fmt.Errorf("登陆器运行库清单无效")
	}
	seen := map[string]bool{}
	var total int64
	for _, f := range m.Files {
		lower := strings.ToLower(f.Path)
		if !safePath.MatchString(f.Path) || strings.HasPrefix(f.Path, "/") || filepath.ToSlash(filepath.Clean(f.Path)) != f.Path || strings.HasPrefix(f.Path, "../") || f.Path == ".." || seen[lower] || !digest.MatchString(f.SHA256) || f.Bytes < 0 || f.Bytes > 128<<20 {
			return fmt.Errorf("运行库文件清单无效：%s", f.Path)
		}
		for _, part := range strings.Split(f.Path, "/") {
			if strings.HasSuffix(part, ".") || part == "." || part == ".." {
				return fmt.Errorf("运行库路径无效")
			}
		}
		seen[lower] = true
		total += f.Bytes
	}
	if total > 768<<20 {
		return fmt.Errorf("运行库超过大小限制")
	}
	for _, path := range []string{"dotnet.exe", "host/fxr/" + m.Version + "/hostfxr.dll", "shared/Microsoft.NETCore.App/" + m.Version + "/coreclr.dll", "shared/Microsoft.WindowsDesktop.App/" + m.Version + "/PresentationFramework.dll"} {
		if !seen[strings.ToLower(path)] {
			return fmt.Errorf("运行库缺少必要组件：%s", path)
		}
	}
	b, _ := json.Marshal(m.Files)
	sum := sha256.Sum256(b)
	if m.Directory != "runtime/launcher-dotnet"+strings.Split(m.Version, ".")[0]+"/"+hex.EncodeToString(sum[:]) {
		return fmt.Errorf("运行库目录与清单不匹配")
	}
	return nil
}

func Decode(raw []byte) (*Manifest, error) {
	if len(raw) > MaxManifest {
		return nil, fmt.Errorf("运行库清单过大")
	}
	var m Manifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&m); err != nil {
		return nil, err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return nil, fmt.Errorf("运行库清单存在多余内容")
	}
	if err := m.Validate(); err != nil {
		return nil, err
	}
	return &m, nil
}

// Read looks immediately before the managed-host overlay. Old self-contained
// templates have no runtime footer and remain supported by the GM packager.
func Read(f *os.File, end int64) (*Manifest, error) {
	if end < launcherbundle.FooterSize {
		return nil, nil
	}
	footer := make([]byte, launcherbundle.FooterSize)
	if _, err := f.ReadAt(footer, end-int64(len(footer))); err != nil {
		return nil, err
	}
	if string(bytes.TrimRight(footer[40:], "\x00")) != launcherbundle.HostMagic {
		return nil, nil
	}
	n := binary.LittleEndian.Uint64(footer[:8])
	if n == 0 || n > 512<<20 || n > uint64(end-int64(len(footer))) {
		return nil, fmt.Errorf("登陆器主体长度无效")
	}
	before := end - int64(len(footer)) - int64(n)
	if before < int64(len(footer)) {
		return nil, nil
	}
	if _, err := f.ReadAt(footer, before-int64(len(footer))); err != nil {
		return nil, err
	}
	if string(bytes.TrimRight(footer[40:], "\x00")) != Magic {
		return nil, nil
	}
	raw, _, err := launcherbundle.Read(f, before, MaxManifest, Magic)
	if err != nil {
		return nil, err
	}
	return Decode(raw)
}

// CheckPath also rejects directory junctions and any linked ancestor.
func CheckPath(path string) error {
	full, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	for {
		info, err := os.Lstat(full)
		if err != nil {
			if !os.IsNotExist(err) {
				return err
			}
		} else if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("运行库路径不能经过符号链接或目录联接")
		}
		parent := filepath.Dir(full)
		if parent == full {
			break
		}
		full = parent
	}
	return nil
}

func Hash(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err = io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// Verify requires the exact declared inventory so hostfxr cannot select an
// undeclared newer runtime from the private directory.
func Verify(clientRoot string, m *Manifest) (string, error) {
	if err := m.Validate(); err != nil {
		return "", err
	}
	root := filepath.Join(clientRoot, filepath.FromSlash(m.Directory))
	if err := CheckPath(root); err != nil {
		return "", err
	}
	expected := map[string]File{}
	for _, row := range m.Files {
		expected[row.Path] = row
	}
	found := 0
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if d.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("运行库不能含符号链接")
		}
		if d.IsDir() {
			return nil
		}
		relative, e := filepath.Rel(root, path)
		if e != nil {
			return e
		}
		name := filepath.ToSlash(relative)
		row, ok := expected[name]
		if !ok {
			return fmt.Errorf("运行库包含未配套文件：%s", name)
		}
		info, e := d.Info()
		if e != nil {
			return e
		}
		if !info.Mode().IsRegular() || info.Size() != row.Bytes {
			return fmt.Errorf("运行库文件大小不符：%s", name)
		}
		sum, e := Hash(path)
		if e != nil {
			return e
		}
		if sum != row.SHA256 {
			return fmt.Errorf("运行库文件校验失败：%s", name)
		}
		found++
		return nil
	})
	if err != nil {
		return "", fmt.Errorf("登陆器 runtime 缺失或不完整，请补齐配套 runtime 文件夹：%w", err)
	}
	if found != len(expected) {
		return "", fmt.Errorf("登陆器 runtime 文件不齐，请补齐配套 runtime 文件夹")
	}
	return root, nil
}
