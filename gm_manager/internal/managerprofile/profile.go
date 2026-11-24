// Package managerprofile stores administrator connection settings with the GM tool.
// It intentionally has no Windows-account encryption or machine identity dependency.
package managerprofile

import (
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

const FileName = "remote-profile.json"

var ErrLegacyProfile = errors.New("这是旧 Windows 账户加密配置，请导入便携连接配置")

type Profile struct {
	Version     int    `json:"version"`
	Address     string `json:"address"`
	Certificate string `json:"certificate,omitempty"`
	Token       string `json:"token"`
}

func DataDirectory(executable, override string) string {
	base := filepath.Dir(executable)
	if override = strings.TrimSpace(override); override != "" {
		if filepath.IsAbs(override) {
			return filepath.Clean(override)
		}
		return filepath.Join(base, override)
	}
	return filepath.Join(base, "管理配置")
}

func Load(directory string) (Profile, error) {
	var p Profile
	raw, err := os.ReadFile(filepath.Join(directory, FileName))
	if err != nil {
		return p, err
	}
	var legacy struct {
		ProtectedToken string `json:"protected_token"`
	}
	if json.Unmarshal(raw, &p) != nil || json.Unmarshal(raw, &legacy) != nil {
		return p, errors.New("连接配置格式无效")
	}
	if p.Token == "" && legacy.ProtectedToken != "" {
		return p, ErrLegacyProfile
	}
	if strings.TrimSpace(p.Address) == "" || len(p.Token) < 32 {
		return p, errors.New("连接配置缺少有效地址或管理令牌")
	}
	if p.Certificate != "" && !filepath.IsAbs(p.Certificate) {
		clean := filepath.Clean(p.Certificate)
		if clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
			return p, errors.New("信任证书必须位于管理配置目录内")
		}
		p.Certificate = filepath.Join(directory, clean)
	}
	return p, nil
}

func Save(directory string, p Profile) error {
	if strings.TrimSpace(p.Address) == "" || len(p.Token) < 32 {
		return errors.New("连接配置缺少有效地址或管理令牌")
	}
	var certificate []byte
	if p.Certificate != "" {
		path := p.Certificate
		if !filepath.IsAbs(path) {
			path = filepath.Join(directory, path)
		}
		var err error
		certificate, err = os.ReadFile(path)
		if err != nil {
			return errors.New("无法读取信任证书，连接配置未保存")
		}
		if !x509.NewCertPool().AppendCertsFromPEM(certificate) {
			return errors.New("信任证书格式无效，连接配置未保存")
		}
		sum := sha256.Sum256(certificate)
		p.Certificate = "server-cert-" + hex.EncodeToString(sum[:8]) + ".pem"
	}
	if err := os.MkdirAll(directory, 0700); err != nil {
		return err
	}
	if len(certificate) != 0 {
		path := filepath.Join(directory, p.Certificate)
		if prior, err := os.ReadFile(path); err == nil {
			if string(prior) != string(certificate) {
				return errors.New("同名信任证书内容不一致，连接配置未保存")
			}
		} else if !os.IsNotExist(err) {
			return err
		} else if err := atomicWrite(path, certificate); err != nil {
			return err
		}
	}
	p.Version = 1
	raw, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		return errors.New("无法编码连接配置")
	}
	return atomicWrite(filepath.Join(directory, FileName), raw)
}

func atomicWrite(path string, raw []byte) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".gm-profile-*")
	if err != nil {
		return err
	}
	temporary := f.Name()
	defer os.Remove(temporary)
	if err = f.Chmod(0600); err == nil {
		_, err = f.Write(raw)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err = os.Rename(temporary, path); err != nil {
		return fmt.Errorf("无法提交连接配置：%w", err)
	}
	return nil
}
