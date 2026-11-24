package launcherruntime

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"newadmin/internal/launcherbundle"
	"os"
	"path/filepath"
	"testing"
)

func fixture(t *testing.T) (string, *Manifest) {
	t.Helper()
	root := t.TempDir()
	m := &Manifest{SchemaVersion: 1, Version: "8.0.32"}
	for _, name := range []string{"dotnet.exe", "host/fxr/8.0.32/hostfxr.dll", "shared/Microsoft.NETCore.App/8.0.32/coreclr.dll", "shared/Microsoft.WindowsDesktop.App/8.0.32/PresentationFramework.dll"} {
		b := []byte(name)
		h := sha256.Sum256(b)
		m.Files = append(m.Files, File{name, int64(len(b)), hex.EncodeToString(h[:])})
	}
	b, _ := json.Marshal(m.Files)
	h := sha256.Sum256(b)
	m.Directory = "runtime/launcher-dotnet8/" + hex.EncodeToString(h[:])
	for _, row := range m.Files {
		p := filepath.Join(root, filepath.FromSlash(m.Directory), filepath.FromSlash(row.Path))
		os.MkdirAll(filepath.Dir(p), 0700)
		os.WriteFile(p, []byte(row.Path), 0600)
	}
	return root, m
}
func TestPrivateRuntimeIntegrity(t *testing.T) {
	for _, kind := range []string{"complete", "missing", "changed", "undeclared"} {
		t.Run(kind, func(t *testing.T) {
			root, m := fixture(t)
			dir := filepath.Join(root, filepath.FromSlash(m.Directory))
			p := filepath.Join(dir, "dotnet.exe")
			switch kind {
			case "missing":
				os.Remove(p)
			case "changed":
				os.WriteFile(p, []byte("xxxxxxxxxx"), 0600)
			case "undeclared":
				os.WriteFile(filepath.Join(dir, "extra.dll"), []byte("untrusted"), 0600)
			}
			_, err := Verify(root, m)
			if (err == nil) != (kind == "complete") {
				t.Fatal(kind, err)
			}
		})
	}
}
func TestRuntimeManifestRejectsInvalidPathsAndPins(t *testing.T) {
	for _, kind := range []string{"traversal", "absolute", "duplicate", "hash", "version", "directory", "size", "required"} {
		t.Run(kind, func(t *testing.T) {
			_, m := fixture(t)
			switch kind {
			case "traversal":
				m.Files[0].Path = "../secret"
			case "absolute":
				m.Files[0].Path = "C:/secret"
			case "duplicate":
				m.Files = append(m.Files, m.Files[0])
			case "hash":
				m.Files[0].SHA256 = "invalid"
			case "version":
				m.Version = "9.0.0"
			case "directory":
				m.Directory = "runtime/client"
			case "size":
				m.Files[0].Bytes = 1 << 30
			case "required":
				m.Files = m.Files[1:]
			}
			if m.Validate() == nil {
				t.Fatal("accepted invalid manifest")
			}
		})
	}
}
func TestManifestOverlayAndLegacyTemplate(t *testing.T) {
	_, m := fixture(t)
	path := filepath.Join(t.TempDir(), "template.exe")
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	f.Write([]byte("MZ bootstrap"))
	raw, _ := json.Marshal(m)
	launcherbundle.Append(f, raw, Magic)
	launcherbundle.Append(f, []byte("MZ managed"), launcherbundle.HostMagic)
	st, _ := f.Stat()
	got, err := Read(f, st.Size())
	if err != nil || got == nil || got.Directory != m.Directory {
		t.Fatal(got, err)
	}
	f.WriteAt([]byte("X"), int64(len("MZ bootstrap")))
	if _, err = Read(f, st.Size()); err == nil {
		t.Fatal("modified manifest accepted")
	}
	legacy, err := os.Create(filepath.Join(t.TempDir(), "legacy.exe"))
	if err != nil {
		t.Fatal(err)
	}
	defer legacy.Close()
	legacy.Write([]byte("MZ bootstrap"))
	launcherbundle.Append(legacy, []byte("MZ managed"), launcherbundle.HostMagic)
	st, _ = legacy.Stat()
	got, err = Read(legacy, st.Size())
	if err != nil || got != nil {
		t.Fatal("legacy template broke", err)
	}
}
