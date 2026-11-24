package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"newadmin/internal/launcherbundle"
	"newadmin/internal/launcherruntime"
	"os"
	"path/filepath"
	"testing"
)

func addRuntimeFixture(t *testing.T, o *LauncherPackageOptions) *launcherruntime.Manifest {
	t.Helper()
	m := &launcherruntime.Manifest{SchemaVersion: 1, Version: "8.0.32"}
	for _, name := range []string{"dotnet.exe", "host/fxr/8.0.32/hostfxr.dll", "shared/Microsoft.NETCore.App/8.0.32/coreclr.dll", "shared/Microsoft.WindowsDesktop.App/8.0.32/PresentationFramework.dll"} {
		b := []byte(name)
		h := sha256.Sum256(b)
		m.Files = append(m.Files, launcherruntime.File{Path: name, Bytes: int64(len(b)), SHA256: hex.EncodeToString(h[:])})
	}
	raw, _ := json.Marshal(m.Files)
	h := sha256.Sum256(raw)
	m.Directory = "runtime/launcher-dotnet8/" + hex.EncodeToString(h[:])
	for _, row := range m.Files {
		p := filepath.Join(filepath.Dir(o.TemplatePath), filepath.FromSlash(m.Directory), filepath.FromSlash(row.Path))
		os.MkdirAll(filepath.Dir(p), 0700)
		os.WriteFile(p, []byte(row.Path), 0600)
	}
	f, err := os.OpenFile(o.TemplatePath, os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ = json.Marshal(m)
	if err = launcherbundle.Append(f, raw, launcherruntime.Magic); err != nil {
		t.Fatal(err)
	}
	if err = launcherbundle.Append(f, []byte("MZ managed fixture"), launcherbundle.HostMagic); err != nil {
		t.Fatal(err)
	}
	f.Close()
	o.TemplateSHA256, err = sha256File(o.TemplatePath)
	if err != nil {
		t.Fatal(err)
	}
	return m
}
func TestLauncherExportsCompletePrivateRuntime(t *testing.T) {
	o := fixturePackage(t)
	m := addRuntimeFixture(t, &o)
	r, err := BuildLauncherPackage(o)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = launcherruntime.Verify(r.Directory, m); err != nil {
		t.Fatal(err)
	}
	if _, err = os.Stat(filepath.Join(r.Directory, "运行库清单.json")); !os.IsNotExist(err) {
		t.Fatal("internal manifest copied")
	}
}
func TestLauncherDirectRuntimeReusePreservesGameRuntimeAndArt(t *testing.T) {
	o := fixturePackage(t)
	m := addRuntimeFixture(t, &o)
	addImageFixture(t, o.TemplatePath)
	o.DirectOutput = true
	sentinel := filepath.Join(o.OutputDir, "runtime", "client", "game.bin")
	os.MkdirAll(filepath.Dir(sentinel), 0700)
	os.WriteFile(sentinel, []byte("game runtime"), 0600)
	r, err := BuildLauncherPackage(o)
	if err != nil {
		t.Fatal(err)
	}
	dotnet := filepath.Join(r.Directory, filepath.FromSlash(m.Directory), "dotnet.exe")
	before, _ := os.Stat(dotnet)
	if _, err = BuildLauncherPackage(o); err != nil {
		t.Fatal(err)
	}
	after, _ := os.Stat(dotnet)
	if !before.ModTime().Equal(after.ModTime()) {
		t.Fatal("runtime recopied instead of reused")
	}
	b, _ := os.ReadFile(sentinel)
	if string(b) != "game runtime" {
		t.Fatal("game runtime modified")
	}
	if _, err = os.Stat(filepath.Join(r.Directory, "Image", "background.png")); err != nil {
		t.Fatal("new Image directory not created", err)
	}
}
func TestLauncherRuntimeRollbackOnExecutableConflict(t *testing.T) {
	o := fixturePackage(t)
	m := addRuntimeFixture(t, &o)
	addImageFixture(t, o.TemplatePath)
	o.DirectOutput = true
	name, _ := launcherExecutableName(o.DisplayName)
	os.MkdirAll(filepath.Join(o.OutputDir, name), 0700)
	sentinel := filepath.Join(o.OutputDir, "runtime", "server", "game.bin")
	os.MkdirAll(filepath.Dir(sentinel), 0700)
	os.WriteFile(sentinel, []byte("server runtime"), 0600)
	if _, err := BuildLauncherPackage(o); err == nil {
		t.Fatal("EXE directory conflict accepted")
	}
	if _, err := os.Stat(filepath.Join(o.OutputDir, filepath.FromSlash(m.Directory))); !os.IsNotExist(err) {
		t.Fatal("runtime left after rollback", err)
	}
	if _, err := os.Stat(filepath.Join(o.OutputDir, "Image", "background.png")); !os.IsNotExist(err) {
		t.Fatal("image left after rollback", err)
	}
	b, _ := os.ReadFile(sentinel)
	if string(b) != "server runtime" {
		t.Fatal("unrelated runtime changed")
	}
}
func TestLauncherRejectsChangedSourceRuntime(t *testing.T) {
	o := fixturePackage(t)
	m := addRuntimeFixture(t, &o)
	p := filepath.Join(filepath.Dir(o.TemplatePath), filepath.FromSlash(m.Directory), "dotnet.exe")
	os.WriteFile(p, []byte("tampered runtime"), 0600)
	if _, err := BuildLauncherPackage(o); err == nil {
		t.Fatal("modified runtime published")
	}
	if _, err := os.Stat(o.OutputDir); !os.IsNotExist(err) {
		t.Fatal("failed publication created output")
	}
}
