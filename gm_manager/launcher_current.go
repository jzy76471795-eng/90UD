package main

import (
	"crypto/sha256"
 "bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"newadmin/internal/launcherbundle"
	"newadmin/internal/launcherruntime"
	"os"
	"path/filepath"
)

// The GM's default player is the existing original launcher with its paired
// private runtime. The Go rewrite remains available as source only.
func LoadCurrentLauncherTemplate(gmDir string) (*LauncherTemplate, error) {
	template, err := LoadLauncherTemplate(gmDir)
	if err != nil {
		return nil, err
	}
	f, err := os.Open(template.Path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	payload, _, err := launcherbundle.Read(f, info.Size(), 512<<20, launcherbundle.HostMagic)
	if err != nil || !(len(payload)>=2&&string(payload[:2])=="MZ" || bytes.HasPrefix(payload,[]byte("PY90_HOST_AESGCM_V1\n"))&&len(payload)>len("PY90_HOST_AESGCM_V1\n")+28) {
		return nil, errors.New("当前GM使用原版玩家登录器，请使用原版模板及配套运行库")
	}
	if err = verifyLauncherProtection(template, payload); err != nil {
		return nil, err
	}
	runtime, err := launcherruntime.Read(f, info.Size())
	if err != nil {
		return nil, err
	}
	if runtime == nil {
		return nil, errors.New("原版登录器缺少配套.NET运行库清单")
	}
	if _, err = launcherruntime.Verify(filepath.Dir(template.Path), runtime); err != nil {
		return nil, fmt.Errorf("原版登录器配套运行库校验失败：%w", err)
	}
	return template, nil
}

// This build receipt stays with the GM template; it is never a player file
// validation list. Generation reuses the protected payload without unpacking it.
type launcherProtectionReceipt struct {
	Schema           int    `json:"schema_version"`
	Method           string `json:"protection_method"`
	TemplateSHA      string `json:"template_sha256"`
	HostSHA          string `json:"managed_host_sha256"`
	AssemblySHA      string `json:"managed_assembly_sha256"`
	Renamed          int    `json:"renamed_entries"`
	GameVersion      int    `json:"game_protection_version"`
	OriginalGameSHA  string `json:"original_game_sha256"`
	ProtectedGameSHA string `json:"protected_game_sha256"`
	KeyID            string `json:"key_id"`
}

func verifyLauncherProtection(template *LauncherTemplate, host []byte) error {
	path := filepath.Join(filepath.Dir(template.Path), "保护证明.json")
	if err := launcherNoLinks(path); err != nil {
		return err
	}
	b, err := readLauncherLimited(path, 16<<10)
	if err != nil {
		return fmt.Errorf("缺少登录器保护构建证明，请运行构建受保护客户端.ps1：%w", err)
	}
	var r launcherProtectionReceipt
	if err = json.Unmarshal(b, &r); err != nil {
		return fmt.Errorf("登录器保护证明无效：%w", err)
	}
	h := sha256.Sum256(host)
	validHex := func(s string, n int) bool { v, e := hex.DecodeString(s); return e == nil && len(v) == n }
	encrypted := bytes.HasPrefix(host,[]byte("PY90_HOST_AESGCM_V1\n"))
 expectedMethod := "obfuscar+client-code-aesgcm-v2"
 if encrypted { expectedMethod="obfuscar+host-aesgcm-v1+client-code-aesgcm-v2" }
 if r.Schema != 1 || r.Method != expectedMethod || r.Renamed < 1 || r.GameVersion != 2 ||
		r.TemplateSHA != template.SHA256 || r.HostSHA != hex.EncodeToString(h[:]) ||
		!validHex(r.AssemblySHA, 32) || !validHex(r.OriginalGameSHA, 32) || !validHex(r.ProtectedGameSHA, 32) || !validHex(r.KeyID, 16) {
		return errors.New("登录器模板与保护构建证明不一致，请重新构建受保护模板")
	}
	return nil
}
