package manager

import (
	"context"
	"database/sql"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"newadmin/internal/adminapi"
)

func directFixture(t *testing.T) (*Service, *httptest.Server, int, int) {
	t.Helper()
	sample := os.Getenv("GM_DIRECT_TEST_DATABASE")
	if sample == "" {
		t.Skip("set GM_DIRECT_TEST_DATABASE to a local verified sample")
	}
	raw, err := os.ReadFile(sample)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "isolated.db")
	if err = os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	s, err := New(Config{Listen: "127.0.0.1:0", Token: "0123456789012345678901234567890123456789", DataDirectory: dir, GameDatabase: path, DirectDatabase: true})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.Close)
	h := httptest.NewServer(s.Handler())
	t.Cleanup(h.Close)
	var account, character int
	if err = s.store.db.QueryRow("SELECT account_id,character_id FROM characters WHERE delete_flag=0 ORDER BY character_id LIMIT 1").Scan(&account, &character); err != nil {
		t.Fatal(err)
	}
	if _, err = s.store.db.Exec("UPDATE accounts SET cera=0,token_cera=0 WHERE account_id=?", account); err != nil {
		t.Fatal(err)
	}
	if _, err = s.store.db.Exec("UPDATE character_wallets SET gold=0 WHERE character_id=?", character); err != nil {
		t.Fatal(err)
	}
	return s, h, account, character
}
func number(t *testing.T, db *sql.DB, query string, args ...any) int64 {
	t.Helper()
	var n int64
	if err := db.QueryRow(query, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}
func TestDirectOfflineCurrencyIdempotencyAndRollback(t *testing.T) {
	s, h, a, c := directFixture(t)
	action := adminapi.Action{Name: "currency.credit", Scope: "single", AccountID: a, Currency: "token_cera", Amount: 27, Confirm: true}
	for i := 0; i < 2; i++ {
		status, body := call(t, h, "POST", "/manage/v1/actions", "direct-tokens", action)
		if status != 200 {
			t.Fatal(status, body)
		}
		if body["data"].(map[string]any)["database_committed"] != true {
			t.Fatal("not committed")
		}
	}
	if number(t, s.store.db, "SELECT token_cera FROM accounts WHERE account_id=?", a) != 27 {
		t.Fatal("duplicate credit")
	}
	action.Amount = 28
	if status, _ := call(t, h, "POST", "/manage/v1/actions", "direct-tokens", action); status != 409 {
		t.Fatal(status)
	}
	gold := adminapi.Action{Name: "currency.credit", Scope: "single", CharacterID: c, Currency: "gold", Amount: 17, Confirm: true}
	if status, b := call(t, h, "POST", "/manage/v1/actions", "direct-gold", gold); status != 200 {
		t.Fatal(status, b)
	}
	if number(t, s.store.db, "SELECT gold FROM character_wallets WHERE character_id=?", c) != 17 {
		t.Fatal("wallet")
	}
	if _, err := s.store.db.Exec("UPDATE accounts SET cera=99999999 WHERE account_id=?", a); err != nil {
		t.Fatal(err)
	}
	before := number(t, s.store.db, "SELECT count(*) FROM gmkit_mails")
	mail := adminapi.Action{Name: "mail.send", Scope: "single", CharacterID: c, Confirm: true, Reward: adminapi.Reward{Gold: 50, Points: 1, Items: []adminapi.Item{{ID: 1, Quantity: 2}}}}
	if status, _ := call(t, h, "POST", "/manage/v1/actions", "direct-rollback", mail); status != 409 {
		t.Fatal(status)
	}
	if number(t, s.store.db, "SELECT gold FROM character_wallets WHERE character_id=?", c) != 17 || number(t, s.store.db, "SELECT count(*) FROM gmkit_mails") != before || number(t, s.store.db, "SELECT count(*) FROM manager_requests WHERE request_id='direct-rollback'") != 0 {
		t.Fatal("partial transaction")
	}
	if _, err := s.store.db.Exec("UPDATE accounts SET cera=0 WHERE account_id=?", a); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if status, b := call(t, h, "POST", "/manage/v1/actions", "direct-mail", mail); status != 200 {
			t.Fatal(status, b)
		}
	}
	if number(t, s.store.db, "SELECT gold FROM character_wallets WHERE character_id=?", c) != 17 || number(t, s.store.db, "SELECT count(*) FROM gmkit_mails") != before+2 {
		t.Fatal("mail not atomic/idempotent")
	}
	if number(t, s.store.db, "SELECT COALESCE(SUM(gold),0) FROM gmkit_mails WHERE character_id=? AND id>(SELECT COALESCE(MAX(id),0)-2 FROM gmkit_mails)", c) != 50 {
		t.Fatal("gold must remain in pending mail until claimed")
	}
	if status, b := call(t, h, "POST", "/manage/v1/preview", "", adminapi.Action{Name: "mail.send", CharacterID: c, Scope: "single"}); status != 200 || b["data"].(map[string]any)["total"] != float64(1) {
		t.Fatal(status, b)
	}
}
func TestDirectCanonicalCodesPoliciesAndBans(t *testing.T) {
	s, h, a, _ := directFixture(t)
	req := adminapi.CodeRequest{Kind: "cdk", Name: "isolated", Quantity: 2, Reward: adminapi.Reward{Points: 2, Gold: 3, Items: []adminapi.Item{{ID: 1, Quantity: 2}}}}
	status, body := call(t, h, "POST", "/manage/v1/codes", "canonical-codes", req)
	if status != 200 {
		t.Fatal(status, body)
	}
	code := body["data"].([]any)[0].(map[string]any)["code"].(string)
	if number(t, s.store.db, "SELECT max_uses FROM gmkit_cdks WHERE code=?", code) != 1 {
		t.Fatal("code not published")
	}
	req.Reward.Tokens = 1
	if status, _ = call(t, h, "POST", "/manage/v1/codes", "bad-tokens", req); status != 400 {
		t.Fatal(status)
	}
	req.Kind = "registration"
	req.Reward = adminapi.Reward{}
	if status, b := call(t, h, "POST", "/manage/v1/codes", "registration-code", req); status != 200 {
		t.Fatal(status, b)
	}
	for _, name := range []string{"account.ban", "account.unban"} {
		if status, b := call(t, h, "POST", "/manage/v1/actions", name, adminapi.Action{Name: name, AccountID: a, Scope: "single", Confirm: true}); status != 200 {
			t.Fatal(status, b)
		}
	}
	if number(t, s.store.db, "SELECT count(*) FROM gmkit_bans WHERE account_id=?", a) != 0 {
		t.Fatal("unban")
	}
	_, body = call(t, h, "GET", "/manage/v1/config/registration", "", nil)
	entry := body["data"].(map[string]any)
	value := entry["value"].(map[string]any)
	value["open"] = false
	value["reward_enabled"] = false
	if status, b := call(t, h, "PUT", "/manage/v1/config/registration", "registration-policy", map[string]any{"revision": entry["revision"], "value": value}); status != 200 {
		t.Fatal(status, b)
	}
	var raw string
	if err := s.store.db.QueryRow("SELECT value FROM gmkit_config WHERE section='registrationPolicy'").Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var policy map[string]any
	_ = json.Unmarshal([]byte(raw), &policy)
	if policy["closed"] != true {
		t.Fatal("not canonical")
	}
}
func TestDirectBatchDeduplicatesAccountAndNeverWritesGameAPI(t *testing.T) {
	s, h, a, c := directFixture(t)
	var posts atomic.Int32
	game := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			posts.Add(1)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "session": gameSession{Complete: true, Connections: []gameConnection{{AccountID: a, CharacterID: c}}}})
	}))
	defer game.Close()
	port := game.Listener.Addr().(*net.TCPAddr).Port
	endpoint := filepath.Join(t.TempDir(), "control.json")
	raw, _ := json.Marshal(map[string]any{"api_version": 1, "host": "127.0.0.1", "port": port, "token": "isolated"})
	if err := os.WriteFile(endpoint, raw, 0600); err != nil {
		t.Fatal(err)
	}
	s.game.endpointFile = endpoint
	action := adminapi.Action{Name: "mail.send", Scope: "all", AccountID: a, Confirm: true, Reward: adminapi.Reward{Points: 5}}
	if status, b := call(t, h, "POST", "/manage/v1/actions", "batch-mail", action); status != 200 {
		t.Fatal(status, b)
	}
	if number(t, s.store.db, "SELECT cera FROM accounts WHERE account_id=?", a) != 5 {
		t.Fatal("account currency repeated across roles")
	}
	if status, b := call(t, h, "POST", "/manage/v1/actions", "online-credit", adminapi.Action{Name: "currency.credit", Scope: "online", Currency: "gold", Amount: 2, Confirm: true}); status != 200 {
		t.Fatal(status, b)
	}
	rule := jobRule{section: "welfare.weekly", revision: 1, value: map[string]any{"enabled": true, "reward": map[string]any{"points": float64(4), "items": []any{}}}}
	if _, err := s.store.db.Exec("INSERT INTO manager_job_rules(section,value_json,baseline_id,revision,updated_at) VALUES(?,?,0,1,?)", rule.section, jsonText(rule.value), gameStamp()); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if err := s.runJobs(context.Background(), time.Now()); err != nil {
			t.Fatal(err)
		}
	}
	if number(t, s.store.db, "SELECT cera FROM accounts WHERE account_id=?", a) != 9 || posts.Load() != 0 {
		t.Fatal("worker duplicate or called business write API")
	}
	if status, _ := call(t, h, "GET", "/manage/v1/receipts/online-credit", "", nil); status != 200 {
		t.Fatal(status)
	}
}
