package manager

import (
	"crypto/sha256"
	"os"
	"path/filepath"
	"testing"
)

func TestManagementStoreRefusesUnrelatedGameDatabase(t *testing.T) {
	source := fixtureDatabase(t)
	raw, err := os.ReadFile(source)
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	target := filepath.Join(root, "management.db")
	if err = os.WriteFile(target, raw, 0600); err != nil {
		t.Fatal(err)
	}
	if s, err := openStore(root); err == nil {
		s.db.Close()
		t.Fatal("unrelated game database accepted as management state")
	}
	after, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if sha256.Sum256(after) != sha256.Sum256(raw) {
		t.Fatal("wrong database changed during schema check")
	}
}
