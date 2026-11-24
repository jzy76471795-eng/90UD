package main

import (
	"bytes"
	"encoding/json"
	"newadmin/internal/launcherbundle"
	"newadmin/internal/launcherruntime"
	"os"
	"path/filepath"
	"testing"
)

// Explicit local packaging check. No game start, API request, or server change.
func TestPVFOnlyLocalTemplatesGenerateCurrentClient(t *testing.T) {
	if os.Getenv("GM_PVF_ONLY_LOCAL_CHECK") != "1" {
		t.Skip("explicit local template check only")
	}
	player := `E:\90USADFO\客户端2\90US 登录器.exe`
	before, err := os.ReadFile(player)
	if err != nil {
		t.Fatal(err)
	}
	f, err := os.Open(player)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	raw, start, err := launcherbundle.Read(f, int64(len(before)), launcherbundle.MaxConfig, launcherbundle.ConfigMagic)
	if err != nil {
		t.Fatal(err)
	}
	var config launcherPublicConfig
	if err = json.Unmarshal(raw, &config); err != nil {
		t.Fatal(err)
	}
	current, _, err := launcherbundle.Read(f, start, 512<<20, launcherbundle.HostMagic)
	if err != nil {
		t.Fatal(err)
	}
	for _, base := range []string{`E:\90USADFO\新版管理系统\构建`, `E:\90USADFO\新版管理系统\登录器资源`} {
		t.Run(filepath.Base(base), func(t *testing.T) {
			template, err := LoadCurrentLauncherTemplate(base)
			if err != nil {
				t.Fatal(err)
			}
			input, err := os.Open(template.Path)
			if err != nil {
				t.Fatal(err)
			}
			defer input.Close()
			info, err := input.Stat()
			if err != nil {
				t.Fatal(err)
			}
			managed, _, err := launcherbundle.Read(input, info.Size(), 512<<20, launcherbundle.HostMagic)
			if err != nil || !bytes.Equal(current, managed) {
				t.Fatal("template managed payload differs from installed client", err)
			}
			runtime, err := launcherruntime.Read(input, info.Size())
			if err != nil || runtime == nil {
				t.Fatal("template runtime missing", err)
			}
			result, err := BuildLauncherPackage(LauncherPackageOptions{TemplatePath: template.Path, TemplateSHA256: template.SHA256, DisplayName: config.DisplayName, ServerID: config.ServerID, LoginURL: config.LoginURL, CertificatePEM: config.CertificatePEM, OutputDir: filepath.Join(t.TempDir(), "player")})
			if err != nil {
				t.Fatal(err)
			}
			generated, err := os.Open(result.LauncherPath)
			if err != nil {
				t.Fatal(err)
			}
			defer generated.Close()
			outputInfo, err := generated.Stat()
			if err != nil {
				t.Fatal(err)
			}
			public, end, err := launcherbundle.Read(generated, outputInfo.Size(), launcherbundle.MaxConfig, launcherbundle.ConfigMagic)
			if err != nil {
				t.Fatal(err)
			}
			var actual launcherPublicConfig
			if json.Unmarshal(public, &actual) != nil || actual != config {
				t.Fatal("public connection config changed")
			}
			payload, _, err := launcherbundle.Read(generated, end, 512<<20, launcherbundle.HostMagic)
			if err != nil || !bytes.Equal(payload, current) {
				t.Fatal("generated launcher payload changed", err)
			}
			if _, err = launcherruntime.Verify(result.Directory, runtime); err != nil {
				t.Fatal(err)
			}
		})
	}
	after, err := os.ReadFile(player)
	if err != nil || !bytes.Equal(before, after) {
		t.Fatal("packaging modified installed client", err)
	}
}
