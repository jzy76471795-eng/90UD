package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"newadmin/internal/launcherbundle"
	"newadmin/internal/launcherruntime"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func publishCurrentFixture(t *testing.T, o LauncherPackageOptions) string {
	t.Helper()
	base := t.TempDir()
	dir := filepath.Join(base, "登录器模板")
	if e := os.MkdirAll(dir, 0700); e != nil {
		t.Fatal(e)
	}
	// The fixture's executable and private runtime already share one directory.
	raw, _ := json.Marshal(map[string]any{"schema_version": 2, "exe_file": filepath.ToSlash(filepath.Join("登录器模板", filepath.Base(o.TemplatePath))), "sha256": o.TemplateSHA256})
	if e := copyOutputFile(o.TemplatePath, filepath.Join(dir, filepath.Base(o.TemplatePath))); e != nil {
		t.Fatal(e)
	}
	if e := os.WriteFile(filepath.Join(dir, "清单.json"), raw, 0600); e != nil {
		t.Fatal(e)
	}
	f, _ := os.Open(o.TemplatePath)
	info, _ := f.Stat()
	host, _, hostErr := launcherbundle.Read(f, info.Size(), 512<<20, launcherbundle.HostMagic)
	if hostErr == nil {
		h := sha256.Sum256(host)
		r := launcherProtectionReceipt{Schema: 1, Method: "obfuscar+client-code-aesgcm-v2", TemplateSHA: o.TemplateSHA256, HostSHA: hex.EncodeToString(h[:]), AssemblySHA: strings.Repeat("a", 64), Renamed: 1, GameVersion: 2, OriginalGameSHA: strings.Repeat("b", 64), ProtectedGameSHA: strings.Repeat("c", 64), KeyID: strings.Repeat("d", 32)}
		proof, _ := json.Marshal(r)
		if e := os.WriteFile(filepath.Join(dir, "保护证明.json"), proof, 0600); e != nil {
			t.Fatal(e)
		}
	}
	m, e := launcherruntime.Read(f, info.Size())
	f.Close()
	if e != nil {
		t.Fatal(e)
	}
	if m != nil {
		for _, row := range m.Files {
			target := filepath.Join(dir, filepath.FromSlash(m.Directory), filepath.FromSlash(row.Path))
			os.MkdirAll(filepath.Dir(target), 0700)
			if e = copyOutputFile(filepath.Join(filepath.Dir(o.TemplatePath), filepath.FromSlash(m.Directory), filepath.FromSlash(row.Path)), target); e != nil {
				t.Fatal(e)
			}
		}
	}
	return base
}

func TestCurrentTemplateRequiresMatchingProtection(t *testing.T) {
	for _, kind := range []string{"missing", "host", "template", "unprotected"} {
		t.Run(kind, func(t *testing.T) {
			o := fixturePackage(t)
			addRuntimeFixture(t, &o)
			base := publishCurrentFixture(t, o)
			path := filepath.Join(base, "登录器模板", "保护证明.json")
			if kind == "missing" {
				os.Remove(path)
			} else {
				b, _ := os.ReadFile(path)
				var r launcherProtectionReceipt
				json.Unmarshal(b, &r)
				switch kind {
				case "host":
					r.HostSHA = strings.Repeat("e", 64)
				case "template":
					r.TemplateSHA = strings.Repeat("f", 64)
				case "unprotected":
					r.Renamed = 0
				}
				b, _ = json.Marshal(r)
				os.WriteFile(path, b, 0600)
			}
			if _, e := LoadCurrentLauncherTemplate(base); e == nil {
				t.Fatal("unprotected or mismatched template accepted")
			}
		})
	}
}
func TestCurrentTemplateRejectsNativeOrUnpairedPlayer(t *testing.T) {
	o := fixturePackage(t)
	base := publishCurrentFixture(t, o)
	if _, e := LoadCurrentLauncherTemplate(base); e == nil {
		t.Fatal("native/unpaired template accepted as the current player")
	}
}
func TestCurrentTemplateAcceptsOriginalAndRejectsMissingRuntime(t *testing.T) {
	o := fixturePackage(t)
	m := addRuntimeFixture(t, &o)
	base := publishCurrentFixture(t, o)
	if _, e := LoadCurrentLauncherTemplate(base); e != nil {
		t.Fatal(e)
	}
	if e := os.Remove(filepath.Join(base, "登录器模板", filepath.FromSlash(m.Directory), "dotnet.exe")); e != nil {
		t.Fatal(e)
	}
	if _, e := LoadCurrentLauncherTemplate(base); e == nil {
		t.Fatal("incomplete original runtime accepted")
	}
}
func TestCurrentTemplateChecksManagedPayloadDigest(t *testing.T) {
	o := fixturePackage(t)
	addRuntimeFixture(t, &o)
	b, _ := os.ReadFile(o.TemplatePath)
	b[len(b)-73] ^= 1
	os.WriteFile(o.TemplatePath, b, 0600)
	o.TemplateSHA256, _ = hashLauncherFile(o.TemplatePath)
	if _, e := LoadCurrentLauncherTemplate(publishCurrentFixture(t, o)); e == nil {
		t.Fatal("corrupted managed payload accepted")
	}
}
func TestOptionalCurrentPlayerPackagePreservesExistingLauncher(t *testing.T) {
	base := os.Getenv("GM_CURRENT_LAUNCHER_BASE")
	if base == "" {
		t.Skip("explicit current player resources only")
	}
	clean := filepath.Clean(base)
	if !strings.HasPrefix(clean, `E:\codex\gm-current-launcher-20261031\`) && clean != `E:\codex\gm-complete-20261101\build` && clean != `E:\90USADFO\新版管理系统\构建` && clean != `E:\90USADFO\新版管理系统\登录器资源` {
		t.Fatal("unexpected resource base")
	}
	template, e := LoadCurrentLauncherTemplate(base)
	if e != nil {
		t.Fatal(e)
	}
	original := `E:\90USADFO\客户端\90US 登录器.exe`
	before, e := hashLauncherFile(original)
	if e != nil {
		t.Fatal(e)
	}
	file, e := os.Open(original)
	if e != nil {
		t.Fatal(e)
	}
	defer file.Close()
	stat, _ := file.Stat()
	raw, configStart, e := launcherbundle.Read(file, stat.Size(), launcherbundle.MaxConfig, launcherbundle.ConfigMagic)
	if e != nil {
		t.Fatal(e)
	}
	var cfg launcherPublicConfig
	if e = json.Unmarshal(raw, &cfg); e != nil {
		t.Fatal(e)
	}
	payload, _, e := launcherbundle.Read(file, configStart, 512<<20, launcherbundle.HostMagic)
	if e != nil {
		t.Fatal(e)
	}
	want := sha256.Sum256(payload)
	f, _ := os.Open(template.Path)
	ts, _ := f.Stat()
	b, _, e := launcherbundle.Read(f, ts.Size(), 512<<20, launcherbundle.HostMagic)
	if e != nil {
		t.Fatal(e)
	}
	m, e := launcherruntime.Read(f, ts.Size())
	f.Close()
	if e != nil || m == nil {
		t.Fatal("missing private runtime", e)
	}
	if sha256.Sum256(b) != want {
		t.Fatal("selected player differs from current running original")
	}
	result, e := BuildLauncherPackage(LauncherPackageOptions{TemplatePath: template.Path, TemplateSHA256: template.SHA256, DisplayName: cfg.DisplayName, ServerID: cfg.ServerID, LoginURL: cfg.LoginURL, CertificatePEM: cfg.CertificatePEM, OutputDir: filepath.Join(t.TempDir(), "player")})
	if e != nil {
		t.Fatal(e)
	}
	generated, e := os.Open(result.LauncherPath)
	if e != nil {
		t.Fatal(e)
	}
	defer generated.Close()
	gs, _ := generated.Stat()
	public, end, e := launcherbundle.Read(generated, gs.Size(), launcherbundle.MaxConfig, launcherbundle.ConfigMagic)
	if e != nil {
		t.Fatal(e)
	}
	var actual launcherPublicConfig
	if json.Unmarshal(public, &actual) != nil || actual != cfg {
		t.Fatal("public config changed")
	}
	generatedPayload, _, e := launcherbundle.Read(generated, end, 512<<20, launcherbundle.HostMagic)
	if e != nil || sha256.Sum256(generatedPayload) != want {
		t.Fatal("original player body changed", e)
	}
	if _, e = launcherruntime.Verify(result.Directory, m); e != nil {
		t.Fatal(e)
	}
	images, e := os.ReadFile(filepath.Join(base, "登录器模板", "Image", "清单.json"))
	if e != nil {
		t.Fatal(e)
	}
	var entries []launcherImageEntry
	if json.Unmarshal(images, &entries) != nil {
		t.Fatal("images manifest")
	}
	for _, entry := range entries {
		sum, e := hashLauncherFile(filepath.Join(result.Directory, "Image", entry.Name))
		if e != nil || sum != entry.SHA256 {
			t.Fatal("artwork changed", entry.Name, e)
		}
	}
	after, e := hashLauncherFile(original)
	if e != nil || after != before {
		t.Fatal("original player changed")
	}
	if report := os.Getenv("GM_CURRENT_LAUNCHER_REPORT"); report != "" {
		data, _ := json.MarshalIndent(map[string]any{"original_player_sha256": before, "original_player_unchanged": true, "original_managed_payload_sha256": hex.EncodeToString(want[:]), "generated_player_body_identical": true, "public_configuration_preserved": true, "runtime_files_verified": len(m.Files), "images_verified": len(entries), "player_executed": false, "server_contacted": false}, "", "  ")
		if e = os.WriteFile(report, data, 0600); e != nil {
			t.Fatal(e)
		}
	}
}

func TestOptionalCurrentTemplateResources(t *testing.T) {
	base := os.Getenv("GM_CURRENT_TEMPLATE_BASE")
	if base == "" {
		t.Skip("explicit original template resources only")
	}
	template, e := LoadCurrentLauncherTemplate(base)
	if e != nil {
		t.Fatal(e)
	}
	o := fixturePackage(t)
	o.TemplatePath, o.TemplateSHA256 = template.Path, template.SHA256
	result, e := BuildLauncherPackage(o)
	if e != nil {
		t.Fatal(e)
	}
	f, e := os.Open(template.Path)
	if e != nil {
		t.Fatal(e)
	}
	info, _ := f.Stat()
	m, e := launcherruntime.Read(f, info.Size())
	f.Close()
	if e != nil || m == nil {
		t.Fatal("private runtime missing", e)
	}
	if _, e = launcherruntime.Verify(result.Directory, m); e != nil {
		t.Fatal(e)
	}
	if report := os.Getenv("GM_CURRENT_TEMPLATE_REPORT"); report != "" {
		raw, _ := json.MarshalIndent(map[string]any{"original_template": true, "runtime_files_verified": len(m.Files), "generated_package_read_back": len(readEmbeddedConfig(t, result.LauncherPath)) > 0, "player_executed": false, "server_contacted": false}, "", "  ")
		if e = os.WriteFile(report, raw, 0600); e != nil {
			t.Fatal(e)
		}
	}
}
