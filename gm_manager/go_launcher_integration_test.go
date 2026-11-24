//go:build windows

package main

import (
	"bytes"
	"debug/buildinfo"
	"debug/pe"
	"encoding/json"
	"newadmin/internal/launcherbundle"
	"newadmin/internal/launcherruntime"
	"newadmin/internal/player"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func TestGoPlayerPvfKeyMatchesGMGeneratedResource(t *testing.T) {
	dir := t.TempDir()
	source := filepath.Join(dir, "before.pvf")
	target := filepath.Join(dir, "after.pvf")
	if e := os.WriteFile(source, fixturePVF("utility-guarded"), 0600); e != nil {
		t.Fatal(e)
	}
	if _, _, e := protectPVFFile(source, target, "", fixtureKeyset); e != nil {
		t.Fatal(e)
	}
	keys, e := player.VerifyPvfKey(target, fixtureKeyset)
	if e != nil || len(keys) != 48 {
		t.Fatal("Go player did not accept GM protected PVF", e)
	}
	clear(keys)
	if _, e = player.VerifyPvfKey(target, strings.Repeat("f", 96)); e == nil {
		t.Fatal("Go player accepted wrong dynamic key")
	}
}
func assertNativeGoPlayer(t *testing.T, path string) {
	t.Helper()
	info, e := buildinfo.ReadFile(path)
	if e != nil || !strings.HasPrefix(info.GoVersion, "go") {
		t.Fatal("player is not a native Go build", e)
	}
	f, e := pe.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	defer f.Close()
	header, ok := f.OptionalHeader.(*pe.OptionalHeader32)
	if !ok || f.Machine != pe.IMAGE_FILE_MACHINE_I386 || header.DataDirectory[14].VirtualAddress != 0 {
		t.Fatal("player must be native Go x86 without CLR")
	}
	s, e := os.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	stat, _ := s.Stat()
	m, e := launcherruntime.Read(s, stat.Size())
	if e != nil || m != nil {
		t.Fatal("Go player still requests managed runtime", e)
	}
	b := make([]byte, 72)
	s.ReadAt(b, stat.Size()-72)
	if bytes.Contains(b, []byte(launcherbundle.HostMagic)) {
		t.Fatal("managed host overlay remained")
	}
}
func TestGoTemplatePolicyRejectsLegacyOrArbitraryEXE(t *testing.T) {
	path := filepath.Join(t.TempDir(), "legacy.exe")
	if e := os.WriteFile(path, []byte("MZ legacy placeholder"), 0600); e != nil {
		t.Fatal(e)
	}
	if verifyGoPlayerTemplate(path) == nil {
		t.Fatal("accepted a non-Go player template")
	}
}
func TestOptionalPreserveExistingPublicServerConfiguration(t *testing.T) {
	source := os.Getenv("GO_PLAYER_EXISTING_CONFIG_SOURCE")
	if source == "" {
		t.Skip("explicitly selected old player public config only")
	}
	if !strings.EqualFold(filepath.Clean(source), `E:\90USADFO\客户端\90US 登录器.exe`) {
		t.Fatal("unexpected player config source")
	}
	base := os.Getenv("GO_PLAYER_GM_BASE")
	if base != `E:\codex\go-launcher-20261031\构建` {
		t.Fatal("unexpected candidate base")
	}
	out := os.Getenv("GO_PLAYER_EXISTING_PACKAGE_OUTPUT")
	if !strings.HasPrefix(filepath.Clean(out), `E:\codex\go-launcher-20261031\玩家登录器_`) {
		t.Fatal("unexpected player output")
	}
	before, e := hashLauncherFile(source)
	if e != nil {
		t.Fatal(e)
	}
	cfg, e := player.LoadConfig(source)
	if e != nil {
		t.Fatal("existing public server configuration", e)
	}
	template, e := LoadLauncherTemplate(base)
	if e != nil {
		t.Fatal(e)
	}
	result, e := BuildLauncherPackage(LauncherPackageOptions{TemplatePath: template.Path, TemplateSHA256: template.SHA256, DisplayName: cfg.DisplayName, ServerID: cfg.ServerID, LoginURL: cfg.LoginURL, CertificatePEM: cfg.CertificatePEM, OutputDir: out, RequireGo: true})
	if e != nil {
		t.Fatal(e)
	}
	actual, e := player.LoadConfig(result.LauncherPath)
	if e != nil || actual != cfg {
		t.Fatal("public server configuration changed", e)
	}
	after, e := hashLauncherFile(source)
	if e != nil || before != after {
		t.Fatal("old player was modified during generation")
	}
	cmd := exec.Command(result.LauncherPath, "-inspect")
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	if _, e = cmd.Output(); e != nil {
		t.Fatal("configured Go player's offline self-check failed", e)
	}
	if report := os.Getenv("GO_PLAYER_EXISTING_CONFIG_REPORT"); report != "" {
		raw, _ := json.MarshalIndent(map[string]any{"old_player_hash_unchanged_during_generation": true, "server_id_url_certificate_display_name_preserved": true, "configuration_read_from": "existing public executable overlay only", "generated_go_player": result.LauncherPath, "new_sha256": result.LauncherSHA256, "old_sha256": before, "server_contacted": false, "game_started": false}, "", "  ")
		if e = os.WriteFile(report, raw, 0600); e != nil {
			t.Fatal(e)
		}
	}
}
func TestOptionalGMGeneratesRunnableGoPlayer(t *testing.T) {
	base := os.Getenv("GO_PLAYER_GM_BASE")
	if base == "" {
		t.Skip("explicit native Go GM build only")
	}
	if !strings.EqualFold(filepath.Clean(base), `E:\codex\go-launcher-20261031\构建`) && !strings.EqualFold(filepath.Clean(base), `E:\90USADFO\新版管理系统\构建`) {
		t.Fatal("unexpected Go GM candidate")
	}
	template, e := LoadLauncherTemplate(base)
	if e != nil {
		t.Fatal(e)
	}
	assertNativeGoPlayer(t, template.Path)
	options := fixturePackage(t)
	options.TemplatePath, options.TemplateSHA256 = template.Path, template.SHA256
	options.RequireGo = true
	result, e := BuildLauncherPackage(options)
	if e != nil {
		t.Fatal(e)
	}
	cfg, e := player.LoadConfig(result.LauncherPath)
	if e != nil || cfg.ServerID != "test_server-1" {
		t.Fatal("Go runtime cannot read GM configuration", e)
	}
	cmd := exec.Command(result.LauncherPath, "-inspect")
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	raw, e := cmd.Output()
	if e != nil {
		t.Fatal("generated Go EXE configuration self-check failed", e)
	}
	var report struct {
		Configured bool   `json:"configured"`
		Dotnet     bool   `json:"dotnet_required"`
		Contacted  bool   `json:"server_contacted"`
		Arch       string `json:"architecture"`
	}
	if json.Unmarshal(raw, &report) != nil || !report.Configured || report.Dotnet || report.Contacted || report.Arch != "386" {
		t.Fatal("Go EXE reported invalid configuration scope")
	}
	files, e := os.ReadDir(result.Directory)
	if e != nil {
		t.Fatal(e)
	}
	if len(files) != 1 || !strings.HasSuffix(strings.ToLower(files[0].Name()), ".exe") {
		t.Fatal("Go GM package should contain only the configured player EXE")
	}
	if target := os.Getenv("GO_PLAYER_GM_REPORT"); target != "" {
		data, _ := json.MarshalIndent(map[string]any{"template_is_native_go_x86": true, "clr_present": false, "dotnet_runtime_required": false, "old_injector_required": false, "old_key_dll_required": false, "gm_public_configuration_read_back_by_actual_exe": true, "player_package_files": len(files), "production_server_contacted": false, "game_started": false}, "", "  ")
		if e = os.WriteFile(target, data, 0600); e != nil {
			t.Fatal(e)
		}
	}
}
