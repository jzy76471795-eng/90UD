package manager

import (
	"net/http"
	"testing"
)

func TestDeleteCodePreservesUsedCodesAndReplays(t *testing.T) {
	s, h := newTestService(t)
	s.config.DirectDatabase = true
	_, err := s.store.db.Exec(`
CREATE TABLE gmkit_cdks(id INTEGER PRIMARY KEY, code TEXT UNIQUE, used INTEGER NOT NULL);
CREATE TABLE gmkit_redemptions(cdk_id INTEGER NOT NULL);
CREATE TABLE gmkit_registration_codes(code TEXT PRIMARY KEY,state TEXT NOT NULL);
CREATE TABLE gmkit_audit(action TEXT,summary TEXT,actor TEXT,request_id TEXT,outcome TEXT,created_at TEXT);
INSERT INTO gmkit_cdks VALUES (1,'unused-cdk',0),(2,'used-cdk',0),(3,'counted-cdk',1);
INSERT INTO gmkit_redemptions VALUES (2);
INSERT INTO gmkit_registration_codes VALUES ('unused-registration','unused'),('revoked-registration','revoked'),('used-registration','used');
INSERT INTO manager_codes VALUES ('unused-cdk','test','cdk','{}','active','test');`)
	if err != nil {
		t.Fatal(err)
	}
	const path = "/manage/v1/codes/delete"
	deleteCode := func(id, kind, code string, want int) {
		t.Helper()
		status, body := call(t, h, http.MethodPost, path, id, map[string]string{"kind": kind, "code": code})
		if status != want {
			t.Fatalf("%s %s: status %d, want %d: %v", kind, code, status, want, body)
		}
	}
	deleteCode("delete-cdk", "cdk", "unused-cdk", http.StatusOK)
	deleteCode("delete-cdk", "cdk", "unused-cdk", http.StatusOK)
	deleteCode("different-request", "cdk", "unused-cdk", http.StatusNotFound)
	deleteCode("delete-registration", "registration", "unused-registration", http.StatusOK)
	deleteCode("delete-revoked", "registration", "revoked-registration", http.StatusOK)
	deleteCode("protected-cdk", "cdk", "used-cdk", http.StatusConflict)
	deleteCode("protected-counted-cdk", "cdk", "counted-cdk", http.StatusConflict)
	deleteCode("protected-registration", "registration", "used-registration", http.StatusConflict)
	deleteCode("invalid-kind", "other", "unused-cdk", http.StatusBadRequest)
	if number(t, s.store.db, "SELECT count(*) FROM gmkit_cdks") != 2 ||
		number(t, s.store.db, "SELECT count(*) FROM gmkit_redemptions WHERE cdk_id=2") != 1 ||
		number(t, s.store.db, "SELECT count(*) FROM gmkit_registration_codes WHERE state='used'") != 1 ||
		number(t, s.store.db, "SELECT count(*) FROM gmkit_registration_codes") != 1 ||
		number(t, s.store.db, "SELECT count(*) FROM manager_codes WHERE code='unused-cdk' AND state='deleted'") != 1 ||
		number(t, s.store.db, "SELECT count(*) FROM gmkit_audit WHERE action='codes.delete'") != 3 {
		t.Fatal("deletion did not preserve usage history and audit exactly once")
	}
}
