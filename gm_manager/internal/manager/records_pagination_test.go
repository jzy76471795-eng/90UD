package manager

import (
	"crypto/sha256"
	"database/sql"
	"fmt"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"
)

func recordsFixture(t *testing.T) (*Service, string) {
	t.Helper()
	path := fixtureDatabase(t)
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	_, err = db.Exec(`CREATE TABLE gmkit_cera_ledger(id INTEGER PRIMARY KEY,account_id INTEGER,created_at TEXT,cera_before INTEGER,cera_after INTEGER,cera_delta INTEGER,token_before INTEGER,token_after INTEGER,token_delta INTEGER);
 CREATE TABLE manager_gold_ledger(id INTEGER PRIMARY KEY,account_id INTEGER,character_id INTEGER,created_at TEXT,delta INTEGER,balance_before INTEGER,balance_after INTEGER);
 CREATE TABLE gmkit_value_credits(request_id TEXT PRIMARY KEY,account_id INTEGER,character_id INTEGER,created_at TEXT,currency TEXT,amount INTEGER,balance_before INTEGER,balance_after INTEGER);
 CREATE TABLE manager_record_coverage(kind TEXT PRIMARY KEY,baseline_id INTEGER);
 INSERT INTO manager_record_coverage VALUES('gold',0);
 CREATE TABLE account_revive_coin_ledger(ledger_id INTEGER PRIMARY KEY,account_id INTEGER,character_id INTEGER,created_at TEXT,reason TEXT,delta INTEGER,balance_before INTEGER,balance_after INTEGER);
 CREATE TABLE gmkit_mails(id INTEGER PRIMARY KEY,character_id INTEGER,created_at TEXT,title TEXT,body TEXT,status TEXT);
 CREATE TABLE gmkit_cdks(id INTEGER PRIMARY KEY,name TEXT,code TEXT,points INTEGER,item_id INTEGER,quantity INTEGER,rewards_json TEXT);
 CREATE TABLE gmkit_redemptions(id INTEGER PRIMARY KEY,cdk_id INTEGER,account_id INTEGER,character_id INTEGER,created_at TEXT);
 CREATE TABLE character_dungeon_best_times(character_id INTEGER,dungeon_id INTEGER,difficulty INTEGER,best_time_ms INTEGER,updated_at TEXT);
 CREATE TABLE gmkit_raid_ledger(id INTEGER PRIMARY KEY,account_id INTEGER,character_id INTEGER,created_at TEXT,family TEXT,instance_id TEXT,event_name TEXT,payload_json TEXT);
 CREATE TABLE item_audit_log(audit_id INTEGER PRIMARY KEY,owner_scope TEXT,owner_id INTEGER,character_id INTEGER,created_at TEXT,action_name TEXT,item_template_id INTEGER,delta_stack_count INTEGER,payload_json TEXT);`)
	if err != nil {
		t.Fatal(err)
	}
	s, _ := newTestService(t)
	s.reader.database = path
	return s, path
}

func TestRecordsLargeLedgerAndInvalidTime(t *testing.T) {
	s, path := recordsFixture(t)
	db, _ := sql.Open("sqlite", path)
	_, err := db.Exec(`WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<100001)
 INSERT INTO gmkit_cera_ledger SELECT i,1,'2026-01-01T00:00:00Z',i-1,i,1,0,0,0 FROM n;`)
	if err != nil {
		t.Fatal(err)
	}
	db.Close()
	started := time.Now()
	page := recordsPage(t, s, "kind=cera&page=2001&page_size=50")
	if page["total"] != float64(100001) || len(page["items"].([]any)) != 1 {
		t.Fatal(page)
	}
	t.Logf("100001-row read-only filtered/count/page query: %s", time.Since(started))
	db, _ = sql.Open("sqlite", path)
	_, err = db.Exec("UPDATE gmkit_cera_ledger SET created_at='not-a-time' WHERE id=1")
	db.Close()
	if err != nil {
		t.Fatal(err)
	}
	h := httptest.NewServer(s.Handler())
	defer h.Close()
	status, result := call(t, h, "GET", "/manage/v1/records?kind=cera", "", nil)
	if status != 503 || result["error"].(map[string]any)["code"] != "record_time_invalid" {
		t.Fatal(status, result)
	}
}
func recordsPage(t *testing.T, s *Service, query string) map[string]any {
	t.Helper()
	h := httptest.NewServer(s.Handler())
	t.Cleanup(h.Close)
	status, result := call(t, h, "GET", "/manage/v1/records?"+query, "", nil)
	if status != 200 {
		t.Fatal(status, result)
	}
	return result["data"].(map[string]any)
}

func TestRecordsFilterAndPageBeyond5000(t *testing.T) {
	s, path := recordsFixture(t)
	db, _ := sql.Open("sqlite", path)
	_, err := db.Exec(`WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<5001)
 INSERT INTO gmkit_cera_ledger SELECT i,1,CASE WHEN i=1 THEN '2024-01-01 00:00:00' ELSE '2026-11-16T00:00:00Z' END,i-1,i,1,0,0,0 FROM n;
 INSERT INTO manager_gold_ledger SELECT id,1,7,created_at,1,cera_before,cera_after FROM gmkit_cera_ledger;
 INSERT INTO account_revive_coin_ledger SELECT id,1,7,created_at,'恢复',1,cera_before,cera_after FROM gmkit_cera_ledger;`)
	db.Close()
	if err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(path)
	for _, kind := range []string{"cera", "gold", "revive"} {
		p := recordsPage(t, s, "kind="+kind+"&page=101&page_size=50")
		if p["total"] != float64(5001) || len(p["items"].([]any)) != 1 {
			t.Fatal(kind, p)
		}
		p = recordsPage(t, s, "kind="+kind+"&from=2024-01-01&to=2024-01-01&account="+url.QueryEscape("角色甲"))
		expected := float64(1)
		if kind == "cera" {
			expected = 0
		} // Currency rows intentionally have no character ownership.
		if p["total"] != expected {
			t.Fatal(kind, p)
		}
		p = recordsPage(t, s, "kind="+kind+"&from=2024-01-01&to=2024-01-01&account="+url.QueryEscape("账号甲"))
		if p["total"] != float64(1) {
			t.Fatal(kind, p)
		}
	}
	all := recordsPage(t, s, "kind=all&page_size=200")
	if all["total"] != float64(15003) {
		t.Fatal(all["total"])
	}
	if strings.Contains(all["source"].(string), "5000") {
		t.Fatal("stale source limit", all["source"])
	}
	empty := recordsPage(t, s, "kind=cera&page=1000000")
	if empty["total"] != float64(5001) || len(empty["items"].([]any)) != 0 {
		t.Fatal(empty)
	}
	after, _ := os.ReadFile(path)
	if sha256.Sum256(before) != sha256.Sum256(after) {
		t.Fatal("record query wrote the game database")
	}
}

func TestRecordsMixedSourcesUnicodeAndStablePages(t *testing.T) {
	s, path := recordsFixture(t)
	db, _ := sql.Open("sqlite", path)
	_, err := db.Exec(`INSERT INTO gmkit_cera_ledger VALUES(1,1,'2026-01-01T00:00:00.123456789Z',10,11,1,8,7,-1);
 INSERT INTO gmkit_mails VALUES(1,7,'2026-01-01 00:00:00','中文通知','保留%_与引号''','unread');
 INSERT INTO manager_gold_ledger VALUES(1,1,7,'2026-01-01T09:00:00+09:00',-5,10,5);
 INSERT INTO gmkit_value_credits VALUES('old',1,7,'2026-01-01 00:00:00','gold',5,0,5),('new',1,7,'2026-01-01 00:00:00','gold',5,5,10);
 UPDATE manager_record_coverage SET baseline_id=1;
 INSERT INTO character_dungeon_best_times VALUES(7,123,2,12345,'2026-01-01 00:00:00');
 INSERT INTO gmkit_raid_ledger VALUES(1,1,7,'2026-01-01 00:00:00','安图恩','inst','返回城镇','{}');
 INSERT INTO item_audit_log VALUES(1,'character',7,7,'2026-01-01 00:00:00','consume',100,-2,'{}');
 INSERT INTO account_revive_coin_ledger VALUES(1,1,7,'2026-01-01 00:00:00','使用',-1,3,2);
 INSERT INTO gmkit_cdks VALUES(1,'测试码','CODE',0,0,0,'[{"kind":"points","amount":4},{"kind":"gold","amount":5},{"kind":"item","itemId":100,"quantity":2}]');
 INSERT INTO gmkit_redemptions VALUES(1,1,1,7,'2026-01-01 00:00:00');`)
	if err != nil {
		t.Fatal(err)
	}
	// A legacy BLOB account name must keep the original GB18030 decoding behavior.
	if _, err = db.Exec("UPDATE accounts SET m_id=? WHERE account_id=2", []byte{0xd6, 0xd0, 0xce, 0xc4}); err != nil {
		t.Fatal(err)
	}
	if _, err = db.Exec("INSERT INTO gmkit_cera_ledger VALUES(2,2,'2026-01-01 00:00:00',0,1,1,0,0,0)"); err != nil {
		t.Fatal(err)
	}
	db.Close()
	first := recordsPage(t, s, "kind=all&page_size=200")
	expected := len(first["items"].([]any))
	if first["total"] != float64(11) || expected != 11 {
		t.Fatal(first)
	}
	var paged []any
	for page := 1; page <= 6; page++ {
		p := recordsPage(t, s, fmt.Sprintf("kind=all&page_size=2&page=%d", page))
		paged = append(paged, p["items"].([]any)...)
	}
	if fmt.Sprint(paged) != fmt.Sprint(first["items"]) {
		t.Fatal("mixed-source page order changed")
	}
	for _, query := range []string{"kind=all&keyword=" + url.QueryEscape("%_"), "kind=all&account=" + url.QueryEscape("中文"), "kind=all&keyword=" + url.QueryEscape("安图恩")} {
		p := recordsPage(t, s, query)
		if p["total"] != float64(1) {
			t.Fatal(query, p)
		}
	}
	p := recordsPage(t, s, "kind=all&keyword="+url.QueryEscape("' OR 1=1 --"))
	if p["total"] != float64(0) {
		t.Fatal(p)
	}
	p = recordsPage(t, s, "kind=cdk")
	reward := p["items"].([]any)[0].(map[string]any)["reward"].(map[string]any)
	if reward["points"] != float64(4) || reward["gold"] != float64(5) {
		t.Fatal(reward)
	}
	p = recordsPage(t, s, "kind=gold")
	if p["total"] != float64(2) {
		t.Fatal("gold coverage deduplication changed", p)
	}
	p = recordsPage(t, s, "kind=all&direction="+url.QueryEscape("支出"))
	if p["total"] != float64(4) {
		t.Fatal(p)
	}
}
