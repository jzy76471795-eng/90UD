package main

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestFlatLauncherUsesSelectedDirectoryAndPreservesClientFiles(t *testing.T) {
	options := fixturePackage(t)
	options.DirectOutput = true
	if err := os.MkdirAll(options.OutputDir, 0700); err != nil {
		t.Fatal(err)
	}
	sentinel := filepath.Join(options.OutputDir, "DFO.exe")
	os.WriteFile(sentinel, []byte("client unchanged"), 0600)
	output, err := launcherOutputPath(options.OutputDir, "登录器")
	if err != nil || output != options.OutputDir {
		t.Fatal("extra directory added", err)
	}
	result, err := BuildLauncherPackage(options)
	if err != nil {
		t.Fatal(err)
	}
	if filepath.Dir(result.LauncherPath) != options.OutputDir {
		t.Fatal("launcher nested in another directory")
	}
	files, _ := os.ReadDir(options.OutputDir)
	if len(files) != 2 {
		t.Fatal("extra folder or sidecar left in selected directory")
	}
	if data, _ := os.ReadFile(sentinel); string(data) != "client unchanged" {
		t.Fatal("unrelated client file changed")
	}
	old, _ := os.ReadFile(result.LauncherPath)
	options.ServerID = "new-server"
	second, err := BuildLauncherPackage(options)
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(second.BackupDirectory, filepath.Base(result.LauncherPath)))
	if err != nil || string(data) != string(old) {
		t.Fatal("old launcher backup missing")
	}
	updated, _ := os.ReadFile(second.LauncherPath)
	options.DisplayName = "另一个登陆器"
	third, err := BuildLauncherPackage(options)
	if err != nil || third.BackupDirectory != "" {
		t.Fatalf("different names should coexist: %v, %v", third, err)
	}
	if data, err := os.ReadFile(second.LauncherPath); err != nil || string(data) != string(updated) {
		t.Fatal("generating a different name replaced the previous launcher")
	}
}

func TestFlatOutputRejectsDirectoryCollisionBeforeChangingAnything(t *testing.T) {
	root := t.TempDir()
	stage := t.TempDir()
	os.WriteFile(filepath.Join(root, "first"), []byte("old"), 0600)
	os.Mkdir(filepath.Join(root, "second"), 0700)
	for _, name := range []string{"first", "second"} {
		os.WriteFile(filepath.Join(stage, name), []byte("new"), 0600)
	}
	if _, err := commitOutputFiles(stage, root, []string{"first", "second"}, t.TempDir()); err == nil {
		t.Fatal("directory collision accepted")
	}
	if data, _ := os.ReadFile(filepath.Join(root, "first")); string(data) != "old" {
		t.Fatal("preflight partially changed outputs")
	}
}

func TestFlatOutputRollsBackWhenLaterFileIsLocked(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("Windows file sharing")
	}
	root := t.TempDir()
	stage := t.TempDir()
	for _, name := range []string{"first", "second"} {
		os.WriteFile(filepath.Join(root, name), []byte("old-"+name), 0600)
		os.WriteFile(filepath.Join(stage, name), []byte("new-"+name), 0600)
	}
	locked, err := os.Open(filepath.Join(root, "second"))
	if err != nil {
		t.Fatal(err)
	}
	defer locked.Close()
	backup, err := commitOutputFiles(stage, root, []string{"first", "second"}, t.TempDir())
	if err == nil || backup == "" {
		t.Fatal("locked output succeeded or missing rollback backup")
	}
	for _, name := range []string{"first", "second"} {
		if data, _ := os.ReadFile(filepath.Join(root, name)); string(data) != "old-"+name {
			t.Fatal("partial output was not rolled back")
		}
	}
}
