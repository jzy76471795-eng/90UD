package main

import (
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"math/big"
	"newadmin/internal/launcherbundle"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fixturePackage(t *testing.T) LauncherPackageOptions {
	t.Helper()
	root := t.TempDir()
	template := filepath.Join(root, "预编译 模板.exe")
	data := []byte("MZ approved frozen template; no directory copying")
	if err := os.WriteFile(template, data, 0600); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(data)
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	cert := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "localhost"}, DNSNames: []string{"localhost"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, BasicConstraintsValid: true, SignatureAlgorithm: x509.SHA256WithRSA}
	der, err := x509.CreateCertificate(rand.Reader, cert, cert, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	certPath := filepath.Join(root, "公开证书.pem")
	if err := os.WriteFile(certPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600); err != nil {
		t.Fatal(err)
	}
	// These files are deliberately beside the selected public certificate and exe.
	// A recursive directory copy would leak them into the player package.
	for _, name := range []string{"server-key.pem", "AppSettings.json"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte("SECRET-WINRM-PASSWORD PRIVATE-PVF-KEY"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	return LauncherPackageOptions{TemplatePath: template, TemplateSHA256: hex.EncodeToString(digest[:]), DisplayName: "测试服务器", ServerID: "test_server-1", LoginURL: "https://localhost:17605", CertificatePath: certPath, OutputDir: filepath.Join(root, "中文 输出", "玩家 新包")}
}

func readEmbeddedConfig(t *testing.T, path string) []byte {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	st, _ := f.Stat()
	b, _, err := launcherbundle.Read(f, st.Size(), launcherbundle.MaxConfig, launcherbundle.ConfigMagic)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestPackageOnlyOneExecutableWithEmbeddedPublicConfig(t *testing.T) {
	options := fixturePackage(t)
	result, err := BuildLauncherPackage(options)
	if err != nil {
		t.Fatal(err)
	}
	if result.Directory != options.OutputDir || result.TemplateSHA256 != options.TemplateSHA256 {
		t.Fatal("result mismatch")
	}
	if filepath.Base(result.LauncherPath) != "测试服务器.exe" {
		t.Fatalf("custom launcher name not used: %s", result.LauncherPath)
	}
	config := readEmbeddedConfig(t, result.LauncherPath)
	var doc map[string]any
	if err = json.Unmarshal(config, &doc); err != nil {
		t.Fatal(err)
	}
	cert, _ := os.ReadFile(options.CertificatePath)
	if len(doc) != 5 || doc["schema_version"] != float64(2) || doc["server_id"] != options.ServerID || doc["display_name"] != options.DisplayName || doc["login_url"] != options.LoginURL || doc["certificate_pem"] != string(cert) {
		t.Fatalf("configuration contract: %s", config)
	}
	files, err := os.ReadDir(result.Directory)
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 1 || files[0].Name() != filepath.Base(result.LauncherPath) {
		t.Fatalf("unexpected output files: %v", files)
	}
	copied, _ := os.ReadFile(result.LauncherPath)
	template, _ := os.ReadFile(options.TemplatePath)
	if !bytes.HasPrefix(copied, template) || bytes.Contains(copied, []byte("SECRET-WINRM-PASSWORD")) || bytes.Contains(copied, []byte("PRIVATE-PVF-KEY")) || bytes.Contains(config, []byte("PRIVATE KEY")) {
		t.Fatal("template damaged or private data leaked")
	}
	hash := sha256.Sum256(copied)
	if result.LauncherSHA256 != hex.EncodeToString(hash[:]) {
		t.Fatal("executable hash mismatch")
	}
	// Damage is detected, rather than falling back to previous server settings.
	copied[len(copied)-73] ^= 1
	damaged := filepath.Join(t.TempDir(), "bad.exe")
	os.WriteFile(damaged, copied, 0600)
	f, _ := os.Open(damaged)
	defer f.Close()
	if _, _, err = launcherbundle.Read(f, int64(len(copied)), launcherbundle.MaxConfig, launcherbundle.ConfigMagic); err == nil {
		t.Fatal("corrupted configuration accepted")
	}
}

func TestPackageRejectsInvalidInputWithoutPartialOutput(t *testing.T) {
	base := fixturePackage(t)
	tests := map[string]func(*LauncherPackageOptions){
		"HTTP":                   func(o *LauncherPackageOptions) { o.LoginURL = "http://localhost:17605" },
		"userinfo":               func(o *LauncherPackageOptions) { o.LoginURL = "https://u:p@localhost:17605" },
		"path":                   func(o *LauncherPackageOptions) { o.LoginURL = "https://localhost:17605/path" },
		"query":                  func(o *LauncherPackageOptions) { o.LoginURL = "https://localhost:17605?token=x" },
		"fragment":               func(o *LauncherPackageOptions) { o.LoginURL = "https://localhost:17605/#x" },
		"wrong SAN":              func(o *LauncherPackageOptions) { o.LoginURL = "https://127.0.0.1:17605" },
		"bad ID":                 func(o *LauncherPackageOptions) { o.ServerID = "../other" },
		"name too long":          func(o *LauncherPackageOptions) { o.DisplayName = strings.Repeat("名", 81) },
		"bad name":               func(o *LauncherPackageOptions) { o.DisplayName = "server\npassword" },
		"empty name":             func(o *LauncherPackageOptions) { o.DisplayName = "   " },
		"extension only":         func(o *LauncherPackageOptions) { o.DisplayName = ".exe" },
		"path escape":            func(o *LauncherPackageOptions) { o.DisplayName = "../outside" },
		"backslash":              func(o *LauncherPackageOptions) { o.DisplayName = `folder\outside` },
		"drive path":             func(o *LauncherPackageOptions) { o.DisplayName = `C:\outside` },
		"reserved name":          func(o *LauncherPackageOptions) { o.DisplayName = "CON.exe" },
		"reserved stem":          func(o *LauncherPackageOptions) { o.DisplayName = "nul.config" },
		"reserved port":          func(o *LauncherPackageOptions) { o.DisplayName = "LPT1" },
		"reserved unicode port":  func(o *LauncherPackageOptions) { o.DisplayName = "COM¹" },
		"trailing dot":           func(o *LauncherPackageOptions) { o.DisplayName = "名称." },
		"space before extension": func(o *LauncherPackageOptions) { o.DisplayName = "名称 .exe" },
		"wildcard":               func(o *LauncherPackageOptions) { o.DisplayName = "名称?" },
		"stream":                 func(o *LauncherPackageOptions) { o.DisplayName = "名称:stream" },
		"wrong pin":              func(o *LauncherPackageOptions) { o.TemplateSHA256 = strings.Repeat("a", 64) },
		"missing cert":           func(o *LauncherPackageOptions) { o.CertificatePath += ".missing" },
		"cert mixed with private key": func(o *LauncherPackageOptions) {
			b, err := os.ReadFile(o.CertificatePath)
			if err != nil {
				t.Fatal(err)
			}
			o.CertificatePath = filepath.Join(filepath.Dir(o.CertificatePath), "mixed.pem")
			b = append(b, []byte("-----BEGIN PRIVATE KEY-----\nAA==\n-----END PRIVATE KEY-----\n")...)
			if err := os.WriteFile(o.CertificatePath, b, 0600); err != nil {
				t.Fatal(err)
			}
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			options := base
			options.OutputDir = filepath.Join(filepath.Dir(base.TemplatePath), "rejected-"+strings.ReplaceAll(name, " ", "-"))
			mutate(&options)
			if _, err := BuildLauncherPackage(options); err == nil {
				t.Fatal("invalid input accepted")
			}
			if _, err := os.Lstat(options.OutputDir); !os.IsNotExist(err) {
				t.Fatal("partial output or overwritten destination")
			}
		})
	}
}

func TestLauncherCustomNamesAndOptionalExtension(t *testing.T) {
	base := fixturePackage(t)
	for _, tc := range []struct{ name, filename string }{
		{"怀旧服 登陆器", "怀旧服 登陆器.exe"},
		{"自定义名称.exe", "自定义名称.exe"},
		{"My Launcher.EXE", "My Launcher.exe"},
		{"90US.v2", "90US.v2.exe"},
		{"  我的登陆器  ", "我的登陆器.exe"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			o := base
			o.DisplayName = tc.name
			o.DirectOutput = true
			o.OutputDir = t.TempDir()
			result, err := BuildLauncherPackage(o)
			if err != nil {
				t.Fatal(err)
			}
			if result.LauncherPath != filepath.Join(o.OutputDir, tc.filename) {
				t.Fatalf("wrong generated filename: %s", result.LauncherPath)
			}
			files, err := os.ReadDir(o.OutputDir)
			if err != nil || len(files) != 1 || files[0].Name() != tc.filename {
				t.Fatalf("unexpected generated files: %v, %v", files, err)
			}
			var config map[string]any
			if err := json.Unmarshal(readEmbeddedConfig(t, result.LauncherPath), &config); err != nil || config["display_name"] != tc.name {
				t.Fatalf("embedded launcher name changed: %v, %v", config, err)
			}
		})
	}
}

func TestOutputNeverOverwritten(t *testing.T) {
	options := fixturePackage(t)
	if err := os.MkdirAll(options.OutputDir, 0700); err != nil {
		t.Fatal(err)
	}
	sentinel := filepath.Join(options.OutputDir, "retain.txt")
	if err := os.WriteFile(sentinel, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := BuildLauncherPackage(options); err == nil {
		t.Fatal("existing directory accepted")
	}

	if data, err := os.ReadFile(sentinel); err != nil || string(data) != "keep" {
		t.Fatal("old output changed")
	}
}

func TestTemplateManifestIsStrictAndContained(t *testing.T) {
	options := fixturePackage(t)
	root := filepath.Dir(options.TemplatePath)
	dir := filepath.Join(root, "登录器模板")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	doc := map[string]any{"schema_version": 2, "exe_file": filepath.Base(options.TemplatePath), "sha256": options.TemplateSHA256}
	b, _ := json.Marshal(doc)
	manifest := filepath.Join(dir, "清单.json")
	if err := os.WriteFile(manifest, b, 0600); err != nil {
		t.Fatal(err)
	}
	loaded, err := LoadLauncherTemplate(root)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Path != options.TemplatePath || loaded.SHA256 != options.TemplateSHA256 {
		t.Fatal("resolved wrong template")
	}
	for _, raw := range []string{`{"schema_version":2,"exe_file":"../escape.exe","sha256":"` + options.TemplateSHA256 + `"}`, `{"schema_version":2,"exe_file":"a.exe","sha256":"` + options.TemplateSHA256 + `","password":"forbidden"}`, `{"schema_version":2,"schema_version":2,"exe_file":"a.exe","sha256":"` + options.TemplateSHA256 + `"}`} {
		if err := os.WriteFile(manifest, []byte(raw), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := LoadLauncherTemplate(root); err == nil {
			t.Fatal("unsafe template manifest accepted")
		}
	}
}
