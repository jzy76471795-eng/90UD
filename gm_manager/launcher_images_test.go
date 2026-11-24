package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func addImageFixture(t *testing.T, template string) string {
	t.Helper()
	root := filepath.Join(filepath.Dir(template), "Image")
	os.Mkdir(root, 0700)
	file := filepath.Join(root, "background.png")
	os.WriteFile(file, []byte("verified image fixture"), 0600)
	sum, _ := sha256File(file)
	raw, _ := json.Marshal([]launcherImageEntry{{"background.png", sum}})
	os.WriteFile(filepath.Join(root, "清单.json"), raw, 0600)
	return root
}
func TestLauncherCopiesExternalImagesAlongsideExecutable(t *testing.T) {
	o := fixturePackage(t)
	addImageFixture(t, o.TemplatePath)
	r, err := BuildLauncherPackage(o)
	if err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(filepath.Join(r.Directory, "Image", "background.png"))
	if err != nil || string(b) != "verified image fixture" {
		t.Fatal("external image missing", err)
	}
	if _, err = os.Stat(filepath.Join(r.Directory, "Image", "清单.json")); !os.IsNotExist(err) {
		t.Fatal("internal manifest was distributed")
	}
}
func TestLauncherImageDirectOutputPreservesCustomAndRollsBackOnExeConflict(t *testing.T) {
	o := fixturePackage(t)
	addImageFixture(t, o.TemplatePath)
	o.DirectOutput = true
	os.MkdirAll(filepath.Join(o.OutputDir, "Image"), 0700)
	custom := filepath.Join(o.OutputDir, "Image", "background.png")
	os.WriteFile(custom, []byte("operator customization"), 0600)
	if _, err := BuildLauncherPackage(o); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(custom)
	if string(b) != "operator customization" {
		t.Fatal("overwrote custom art")
	}
	// A directory with the EXE name blocks publishing after image installation.
	o.OutputDir = filepath.Join(t.TempDir(), "blocked")
	name, _ := launcherExecutableName(o.DisplayName)
	os.MkdirAll(filepath.Join(o.OutputDir, name), 0700)
	if _, err := BuildLauncherPackage(o); err == nil {
		t.Fatal("invalid target accepted")
	}
	if _, err := os.Stat(filepath.Join(o.OutputDir, "Image", "background.png")); !os.IsNotExist(err) {
		t.Fatal("failed EXE left new images")
	}
}
func TestLauncherImageManifestCannotCopySecretsOrChangedFiles(t *testing.T) {
	o := fixturePackage(t)
	root := addImageFixture(t, o.TemplatePath)
	os.WriteFile(filepath.Join(root, "background.png"), []byte("changed"), 0600)
	if _, err := BuildLauncherPackage(o); err == nil {
		t.Fatal("changed image accepted")
	}
	raw, _ := json.Marshal([]launcherImageEntry{{"../server-key.pem", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}})
	os.WriteFile(filepath.Join(root, "清单.json"), raw, 0600)
	if _, err := BuildLauncherPackage(o); err == nil {
		t.Fatal("path traversal accepted")
	}
	if _, err := os.Stat(o.OutputDir); !os.IsNotExist(err) {
		t.Fatal("failed generation committed output")
	}
}
