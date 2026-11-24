//go:build windows

package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"debug/pe"
	"encoding/json"
	"fmt"
	"io"
	"newadmin/internal/downloader"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestOptionalCompiledDownloaderPackage(t *testing.T) {
	template := os.Getenv("GM_DOWNLOADER_TEMPLATE")
	if template == "" {
		t.Skip("set a freshly compiled downloader template for native verification")
	}
	output, configPath := os.Getenv("GM_DOWNLOADER_OUTPUT"), os.Getenv("GM_DOWNLOADER_CONFIG")
	for _, path := range []string{template, output, configPath} {
		full, err := filepath.Abs(path)
		if err != nil || !strings.HasPrefix(strings.ToLower(full), `e:\codex\`) {
			t.Fatal("native verification files must remain in E:\\codex")
		}
	}
	native, err := pe.Open(template)
	if err != nil {
		t.Fatal(err)
	}
	resource := native.Section(".rsrc")
	if resource == nil {
		native.Close()
		t.Fatal("native downloader is missing its Windows manifest resource")
	}
	resourceData, err := resource.Data()
	native.Close()
	if err != nil || !bytes.Contains(resourceData, []byte("Microsoft.Windows.Common-Controls")) {
		t.Fatal("native downloader is missing Common Controls v6", err)
	}
	raw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	var config downloader.Config
	if err = json.Unmarshal(raw, &config); err != nil {
		t.Fatal(err)
	}
	if err = os.MkdirAll(output, 0700); err != nil {
		t.Fatal(err)
	}
	pin, err := sha256File(template)
	if err != nil {
		t.Fatal(err)
	}
	name, err := checkedExecutableName(config.Name)
	if err != nil {
		t.Fatal(err)
	}
	target, err := buildLocalDownloader(template, pin, output, name, config)
	if err != nil {
		t.Fatal(err)
	}
	if target != filepath.Join(output, name) {
		t.Fatal("native downloader was nested in an output folder")
	}
	files, err := os.ReadDir(output)
	if err != nil || len(files) != 1 || files[0].Name() != name || files[0].IsDir() {
		t.Fatal("native generation did not produce a single EXE", err)
	}
	t.Log("native downloader:", target)
}

func TestDownloaderPackageWithoutClientSHA256(t *testing.T) {
	root := t.TempDir()
	template := filepath.Join(root, "template.exe")
	raw := []byte("MZ local downloader template fixture")
	if err := os.WriteFile(template, raw, 0600); err != nil {
		t.Fatal(err)
	}
	pin := fmt.Sprintf("%x", sha256.Sum256(raw))
	config := downloader.Config{Version: 1, Name: "客户端下载", URL: "https://example.invalid/client.zip", FileName: "client.zip", Extract: true, Launcher: "游戏/登录器.exe", AutoStart: true}
	target, err := buildLocalDownloader(template, pin, root, "下载器.exe", config)
	if err != nil {
		t.Fatal(err)
	}
	if target != filepath.Join(root, "下载器.exe") {
		t.Fatalf("downloader should be a single EXE in the selected directory: %s", target)
	}
	loaded, err := downloader.LoadExecutable(target)
	if err != nil || loaded.SHA256 != "" || loaded.URL != config.URL || !loaded.Extract || !loaded.AutoStart || loaded.Launcher != config.Launcher {
		t.Fatal("generated configuration does not match", loaded, err)
	}
	rawConfig, err := json.Marshal(loaded)
	if err != nil || bytes.Contains(rawConfig, []byte(`"sha256"`)) {
		t.Fatal("generated configuration still requires a client checksum", err)
	}
	if err := os.WriteFile(filepath.Join(root, "保留文件.txt"), []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	config.Name = "更新后的下载器"
	if _, err = buildLocalDownloader(template, pin, root, "下载器.exe", config); err != nil {
		t.Fatal(err)
	}
	loaded, err = downloader.LoadExecutable(target)
	if err != nil || loaded.Name != config.Name {
		t.Fatal("same-name downloader was not updated", err)
	}
	if data, err := os.ReadFile(filepath.Join(root, "保留文件.txt")); err != nil || string(data) != "keep" {
		t.Fatal("unrelated output file changed", err)
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if entry.IsDir() {
			t.Fatal("left an extra output folder: " + entry.Name())
		}
	}
}

func TestOfflineManagerPackageExcludesPrivateNeighbors(t *testing.T) {
	base := t.TempDir()
	if err := os.WriteFile(filepath.Join(base, "Go管理服务.exe"), []byte("MZ fixture go manager"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(base, "go-manager.json"), []byte("PRIVATE-GM-TOKEN-MUST-NOT-BE-PACKAGED"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(base, "管理配置"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(base, "管理配置", "remote-profile.json"), []byte(`{"token":"PRIVATE-GM-TOKEN-PORTABLE-PROFILE"}`), 0600); err != nil {
		t.Fatal(err)
	}
	path, err := prepareManagerPackageAt(base, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	archive, err := zip.OpenReader(path)
	if err != nil {
		t.Fatal(err)
	}
	defer archive.Close()
	allowed := map[string]bool{"Go管理服务.exe": true, "配置样例_不能直接启动.json": true, "清单.json": true, "先读.txt": true}
	if len(archive.File) != 4 {
		t.Fatal(len(archive.File))
	}
	for _, entry := range archive.File {
		if !allowed[entry.Name] {
			t.Fatal(entry.Name)
		}
		stream, err := entry.Open()
		if err != nil {
			t.Fatal(err)
		}
		raw, err := io.ReadAll(stream)
		stream.Close()
		if err != nil {
			t.Fatal(err)
		}
		if bytes.Contains(raw, []byte("PRIVATE-GM-TOKEN")) {
			t.Fatal("private token leaked")
		}
		if entry.Name == "清单.json" {
			var manifest map[string]any
			if json.Unmarshal(raw, &manifest) != nil || manifest["deployed"] != false {
				t.Fatal("package marked deployed")
			}
		}
	}
}

func TestOptionalRealLauncherAndDownloaderTemplates(t *testing.T) {
	base := os.Getenv("GM_REAL_PACKAGE_BASE")
	if base == "" {
		t.Skip("仅在明确指定本次配套模板时运行")
	}
	if !strings.EqualFold(filepath.Clean(base), `E:\codex\new-gm-go-20261031\交付`) &&
		!strings.EqualFold(filepath.Clean(base), `E:\90USADFO\新版管理系统\构建`) &&
		!strings.EqualFold(filepath.Clean(base), `E:\codex\go-launcher-20261031\构建`) {
		t.Fatal("只允许本次交付或已同步的本机项目模板")
	}
	template, err := LoadLauncherTemplate(base)
	if err != nil {
		t.Fatal(err)
	}
	options := fixturePackage(t)
	options.TemplatePath, options.TemplateSHA256 = template.Path, template.SHA256
	result, err := BuildLauncherPackage(options)
	if err != nil {
		t.Fatal(err)
	}
	config := readEmbeddedConfig(t, result.LauncherPath)
	if !bytes.Contains(config, []byte("test_server-1")) || bytes.Contains(config, []byte("PRIVATE-PVF-KEY")) {
		t.Fatal("real launcher embedded wrong configuration")
	}
	pinRaw, err := os.ReadFile(filepath.Join(base, "下载器模板.SHA256.txt"))
	if err != nil {
		t.Fatal(err)
	}
	downloadConfig := downloader.Config{Version: 1, Name: "本机模板联调", URL: "https://example.invalid/client.zip", FileName: "client.zip", SHA256: strings.Repeat("0", 64), Extract: true, Launcher: "launcher.exe", AutoStart: true}
	target, err := buildLocalDownloader(filepath.Join(base, "下载器模板.exe"), strings.TrimSpace(string(pinRaw)), t.TempDir(), "本机模板联调.exe", downloadConfig)
	if err != nil {
		t.Fatal(err)
	}
	actual, err := downloader.LoadExecutable(target)
	if err != nil || actual.Launcher != "launcher.exe" || actual.URL != downloadConfig.URL {
		t.Fatal(err, actual)
	}
	if report := os.Getenv("GM_REAL_PACKAGE_REPORT"); report != "" {
		raw, _ := json.MarshalIndent(map[string]any{"real_launcher_template_verified": true, "external_runtime_and_images_verified": true, "generated_launcher_config_read_back": true, "real_downloader_template_verified": true, "generated_downloader_config_read_back": true, "network_download_executed": false, "player_login_executed": false}, "", "  ")
		if err = os.WriteFile(report, raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
}
