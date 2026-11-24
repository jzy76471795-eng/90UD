package manager

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"

	"newadmin/internal/adminapi"
)

func fixtureDatabase(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "fixture.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	_, err = db.Exec(`CREATE TABLE accounts(account_id INTEGER PRIMARY KEY,m_id TEXT,cera INTEGER,token_cera INTEGER);
CREATE TABLE characters(character_id INTEGER PRIMARY KEY,account_id INTEGER,name TEXT,level INTEGER,job INTEGER,delete_flag INTEGER);
CREATE TABLE character_wallets(character_id INTEGER PRIMARY KEY,gold INTEGER);
INSERT INTO accounts VALUES(1,'账号甲',100,50),(2,'空账号',0,0);
INSERT INTO characters VALUES(7,1,'角色甲',90,0,0),(8,1,'角色无钱包',80,1,0),(9,1,'已删除角色',90,2,1);
INSERT INTO character_wallets VALUES(7,1234);`)
	if err != nil {
		t.Fatal(err)
	}
	return path
}
func TestReadOnlySnapshotAndUnknownArePreserved(t *testing.T) {
	path := fixtureDatabase(t)
	before, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	reader := gameReader{database: path}
	probe, probeErr := reader.open()
	if probeErr != nil {
		t.Fatal(probeErr)
	}
	if probeErr = probe.Ping(); probeErr != nil {
		t.Fatal("readonly open:", probeErr)
	}
	probe.Close()
	accounts, characters, err := reader.read(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(accounts) != 2 || len(characters) != 2 || accounts[0].Name != "账号甲" || accounts[0].CharacterCount != 2 || accounts[0].OnlineStatus != "unknown" {
		t.Fatal(accounts, characters)
	}
	if characters[0].Gold == nil || *characters[0].Gold != 1234 || characters[1].Gold != nil {
		t.Fatal(characters)
	}
	db, err := reader.open()
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.Exec("UPDATE accounts SET cera=900"); err == nil {
		t.Fatal("read connection allowed write")
	}
	db.Close()
	after, _ := os.ReadFile(path)
	if sha256.Sum256(before) != sha256.Sum256(after) {
		t.Fatal("read changed database")
	}
}
func attachGame(t *testing.T, s *Service, handler http.HandlerFunc) {
	t.Helper()
	game := httptest.NewServer(handler)
	t.Cleanup(game.Close)
	u, _ := url.Parse(game.URL)
	port, _ := strconv.Atoi(u.Port())
	raw, _ := json.Marshal(map[string]any{"api_version": 1, "host": "127.0.0.1", "port": port, "token": "fixture-game-token"})
	path := filepath.Join(t.TempDir(), "endpoint.json")
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	s.game.endpointFile = path
}
func gameSessionReply(w http.ResponseWriter) {
	_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "session": map[string]any{"all_connections_complete": true, "connections": []map[string]any{{"account_id": 1, "character_id": 7, "session_epoch": 9}}}})
}
func TestNativeCreditGuardAndDurableReplay(t *testing.T) {
	s, h := newTestService(t)
	var calls atomic.Int32
	attachGame(t, s, func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer fixture-game-token" {
			t.Error("game auth missing")
		}
		if r.Method == "GET" {
			gameSessionReply(w)
			return
		}
		var body map[string]any
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("bad body")
		}
		if body["expected_character_id"] != float64(7) || body["expected_session_epoch"] != float64(9) || body["command"] != "currency.add_gold" {
			t.Error("target guard missing", body)
		}
		calls.Add(1)
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "request_id": body["request_id"], "result": map[string]any{"character_id": 7, "amount": 10, "balance": 110}})
	})
	body := adminapi.Action{Name: "currency.credit", Currency: "gold", CharacterID: 7, AccountID: 1, Amount: 10, Confirm: true}
	status, result := call(t, h, "POST", "/manage/v1/actions", "native-1", body)
	if status != 200 {
		t.Fatal(status, result)
	}
	status, _ = call(t, h, "POST", "/manage/v1/actions", "native-1", body)
	if status != 200 || calls.Load() != 1 {
		t.Fatal("duplicate native execution", status, calls.Load())
	}
	body.AccountID = 2
	status, _ = call(t, h, "POST", "/manage/v1/actions", "native-2", body)
	if status != 409 || calls.Load() != 1 {
		t.Fatal("account mismatch sent", status)
	}
}
func TestUnknownNativeResponseSurvivesRestartWithoutResend(t *testing.T) {
	s, h := newTestService(t)
	var calls atomic.Int32
	attachGame(t, s, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			gameSessionReply(w)
			return
		}
		calls.Add(1)
		conn, _, err := w.(http.Hijacker).Hijack()
		if err == nil {
			conn.Close()
		}
	})
	body := adminapi.Action{Name: "currency.credit", Currency: "gold", CharacterID: 7, Amount: 10, Confirm: true}
	status, result := call(t, h, "POST", "/manage/v1/actions", "unknown-1", body)
	if status != 409 || result["ok"] != false {
		t.Fatal(status, result)
	}
	s2, err := New(s.config)
	if err != nil {
		t.Fatal(err)
	}
	s2.game = s.game
	defer s2.Close()
	h2 := httptest.NewServer(s2.Handler())
	defer h2.Close()
	status, result = call(t, h2, "POST", "/manage/v1/actions", "unknown-1", body)
	if status != 409 || calls.Load() != 1 {
		t.Fatal("unknown action replayed", status, result, calls.Load())
	}
}
func TestConcurrentManagementWritesDoNotLoseCodes(t *testing.T) {
	_, h := newTestService(t)
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			status, result := call(t, h, "POST", "/manage/v1/codes", "concurrent-"+strconv.Itoa(index), adminapi.CodeRequest{Kind: "cdk", Name: "并发", Quantity: 2, Reward: adminapi.Reward{Points: 1}})
			if status != 200 {
				t.Error(result)
			}
		}(i)
	}
	wg.Wait()
	_, result := call(t, h, "GET", "/manage/v1/codes?kind=cdk&page_size=200", "", nil)
	if result["data"].(map[string]any)["total"] != float64(40) {
		t.Fatal(result)
	}
}
func TestConfigurationRequiresContentAndValidRewards(t *testing.T) {
	_, h := newTestService(t)
	for i, body := range []map[string]any{{"enabled": true}, {"enabled": true, "content": ""}, {"enabled": true, "content": "ok", "interval": 1.5}} {
		status, _ := call(t, h, "PUT", "/manage/v1/config/announcement.chat", "bad-config-"+strconv.Itoa(i), map[string]any{"revision": 0, "value": body})
		if status != 400 {
			t.Fatal(status, body)
		}
	}
}
