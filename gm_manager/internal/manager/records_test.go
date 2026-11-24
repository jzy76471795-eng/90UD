package manager

import (
	"database/sql"
	"net/http/httptest"
	"testing"
)

func TestNativeRecordsPreserveScopeAndTime(t *testing.T) {
	path := fixtureDatabase(t)
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec(`CREATE TABLE gmkit_cera_ledger(id INTEGER PRIMARY KEY,account_id INTEGER,created_at TEXT,cera_before INTEGER,cera_after INTEGER,cera_delta INTEGER,token_before INTEGER,token_after INTEGER,token_delta INTEGER);
INSERT INTO gmkit_cera_ledger VALUES(1,1,'2026-10-31 00:00:00',100,110,10,50,50,0),(2,1,'2026-10-30 15:59:59',110,100,-10,50,49,-1);
CREATE TABLE gmkit_identity(account_id INTEGER,remote_ip TEXT,machine_id TEXT);
INSERT INTO gmkit_identity VALUES(1,'192.0.2.1','fixture-device');
CREATE TABLE gmkit_ban_rules(kind TEXT,value TEXT,revoked_at TEXT,expires_at TEXT);
INSERT INTO gmkit_ban_rules VALUES('machine','fixture-device',NULL,NULL);`)
	db.Close()
	if err != nil {
		t.Fatal(err)
	}
	s, _ := newTestService(t)
	s.reader.database = path
	h := httptest.NewServer(s.Handler())
	defer h.Close()
	status, result := call(t, h, "GET", "/manage/v1/records?kind=cera&from=2026-10-31&to=2026-10-31", "", nil)
	if status != 200 {
		t.Fatal(result)
	}
	page := result["data"].(map[string]any)
	if page["total"] != float64(1) {
		t.Fatal(page)
	}
	record := page["items"].([]any)[0].(map[string]any)
	if record["character"] != "" || record["direction"] != "收入" {
		t.Fatal(record)
	}
	status, result = call(t, h, "GET", "/manage/v1/accounts?scope=banned", "", nil)
	if status != 200 || result["data"].(map[string]any)["total"] != float64(1) {
		t.Fatal(status, result)
	}
	status, _ = call(t, h, "GET", "/manage/v1/records?kind=chat", "", nil)
	if status != 501 {
		t.Fatal("uncollected chat became empty success", status)
	}
}
