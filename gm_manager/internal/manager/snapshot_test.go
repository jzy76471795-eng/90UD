package manager

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"testing"
)

func TestMemoryBackupIsConsistentAndCreatesNoRemoteFile(t *testing.T) {
	s, _, account, _ := directFixture(t)
	before, _ := os.ReadDir(filepath.Dir(s.config.GameDatabase))
	raw, err := s.snapshotBytes(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	after, _ := os.ReadDir(filepath.Dir(s.config.GameDatabase))
	if len(before) != len(after) {
		t.Fatal("created remote backup file")
	}
	path := filepath.Join(t.TempDir(), "downloaded.db")
	if err = os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var integrity string
	if err = db.QueryRow("PRAGMA integrity_check").Scan(&integrity); err != nil || integrity != "ok" {
		t.Fatal("bad serialized backup", err, integrity)
	}
	if number(t, db, "SELECT token_cera FROM accounts WHERE account_id=?", account) != 0 {
		t.Fatal("wrong backup state")
	}
	if _, err = s.store.db.Exec("UPDATE accounts SET token_cera=3 WHERE account_id=?", account); err != nil {
		t.Fatal(err)
	}
	if number(t, db, "SELECT token_cera FROM accounts WHERE account_id=?", account) != 0 {
		t.Fatal("backup was not independent")
	}
}
