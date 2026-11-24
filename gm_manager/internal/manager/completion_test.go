package manager

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"newadmin/internal/adminapi"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestCanonicalCDKStateAndImmediateAnnouncement(t *testing.T) {
	s, h, _, _ := directFixture(t)
	status, b := call(t, h, "POST", "/manage/v1/codes", "state-code", adminapi.CodeRequest{Kind: "cdk", Name: "state", Quantity: 1, Reward: adminapi.Reward{Points: 1}})
	if status != 200 {
		t.Fatal(b)
	}
	code := b["data"].([]any)[0].(map[string]any)["code"].(string)
	for i, enabled := range []bool{false, true} {
		if status, b = call(t, h, "POST", "/manage/v1/codes/state", []string{"disable", "enable"}[i], map[string]any{"kind": "cdk", "code": code, "enabled": enabled, "confirm": true}); status != 200 {
			t.Fatal(status, b)
		}
		want := int64(0)
		if enabled {
			want = 1
		}
		if number(t, s.store.db, "SELECT enabled FROM gmkit_cdks WHERE code=?", code) != want {
			t.Fatal("state not canonical")
		}
	}
	count := number(t, s.store.db, "SELECT count(*) FROM gmkit_announcements")
	body := map[string]any{"section": "announcement.navigation", "content": "isolated notice", "confirm": true}
	for i := 0; i < 2; i++ {
		if status, b = call(t, h, "POST", "/manage/v1/announcements", "announcement-once", body); status != 200 {
			t.Fatal(status, b)
		}
	}
	if number(t, s.store.db, "SELECT count(*) FROM gmkit_announcements") != count+1 {
		t.Fatal("duplicate announcement")
	}
	body["content"] = strings.Repeat("字", 401)
	if status, _ = call(t, h, "POST", "/manage/v1/announcements", "too-long", body); status != 400 {
		t.Fatal(status)
	}
}
func TestNumericNamesRequireExplicitTargetWhenAmbiguous(t *testing.T) {
	s, _, account, _ := directFixture(t)
	var other int
	if err := s.store.db.QueryRow("SELECT account_id FROM accounts WHERE account_id!=? LIMIT 1", account).Scan(&other); err != nil {
		t.Fatal(err)
	}
	label := strconv.Itoa(account)
	if _, err := s.store.db.Exec("UPDATE accounts SET m_id='fixture-numeric-'||account_id WHERE m_id=?", label); err != nil {
		t.Fatal(err)
	}
	if _, err := s.store.db.Exec("UPDATE accounts SET m_id=? WHERE account_id=?", label, other); err != nil {
		t.Fatal(err)
	}
	tx, err := s.store.db.BeginTx(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err = s.directTargets(context.Background(), tx, adminapi.Action{Scope: "single", Target: label}, false, nil); err == nil {
		t.Fatal("ambiguous numeric name did not fail closed")
	}
	for _, test := range []struct {
		target  string
		account int
	}{{"id:" + label, account}, {"name:" + label, other}} {
		rows, err := s.directTargets(context.Background(), tx, adminapi.Action{Scope: "single", Target: test.target}, false, nil)
		if err != nil || len(rows) != 1 || rows[0].account != test.account {
			t.Fatal("explicit target failed", err)
		}
	}
}

func TestSystemRebootRequiresTargetIdentityAndUsesOneFixedCommand(t *testing.T) {
	s, h := newTestService(t)
	hostname, _ := os.Hostname()
	body := map[string]any{"command": "reboot", "confirm": true, "hostname": hostname}
	if status, _ := call(t, h, "POST", "/manage/v1/server", "disabled-reboot", body); status != 403 {
		t.Fatal(status)
	}
	s.config.AllowSystemReboot = true
	body["hostname"] = "wrong-host"
	if status, _ := call(t, h, "POST", "/manage/v1/server", "wrong-reboot", body); status != 403 {
		t.Fatal(status)
	}
	body["hostname"] = hostname
	var count int
	s.systemRunner = func(context.Context, string) error { count++; return nil }
	if status, b := call(t, h, "POST", "/manage/v1/server", "mock-reboot", body); status != 202 {
		t.Fatal(status, b)
	}
	deadline := time.Now().Add(time.Second)
	for {
		status, b := call(t, h, "GET", "/manage/v1/receipts/mock-reboot", "", nil)
		if status != 200 {
			t.Fatal(b)
		}
		if b["data"].(map[string]any)["state"] == "confirmed" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("pending reboot")
		}
		time.Sleep(time.Millisecond)
	}
	if status, b := call(t, h, "POST", "/manage/v1/server", "mock-reboot", body); status != 200 {
		t.Fatal(status, b)
	}
	if count != 1 {
		t.Fatal("OS command repeated")
	}
}
func TestWalletLedgerCapturesIncomeAndExpenseWithoutDoubleCountingGM(t *testing.T) {
	s, h, _, character := directFixture(t)
	// The verified sample may contain more history than the API's 50-row page.
	// Isolate this assertion's ledger inside the disposable database copy.
	if _, err := s.store.db.Exec("DELETE FROM manager_gold_ledger; DELETE FROM gmkit_value_credits WHERE currency='gold'"); err != nil {
		t.Fatal(err)
	}
	before := number(t, s.store.db, "SELECT count(*) FROM manager_gold_ledger")
	if status, b := call(t, h, "POST", "/manage/v1/actions", "ledger-credit", adminapi.Action{Name: "currency.credit", Scope: "single", CharacterID: character, Currency: "gold", Amount: 9, Confirm: true}); status != 200 {
		t.Fatal(status, b)
	}
	if _, err := s.store.db.Exec("UPDATE character_wallets SET gold=gold-2 WHERE character_id=?", character); err != nil {
		t.Fatal(err)
	}
	if number(t, s.store.db, "SELECT count(*) FROM manager_gold_ledger") != before+2 {
		t.Fatal("missing wallet deltas")
	}
	status, b := call(t, h, "GET", "/manage/v1/records?kind=gold", "", nil)
	if status != 200 {
		t.Fatal(b)
	}
	items := b["data"].(map[string]any)["items"].([]any)
	foundIncome, foundExpense := false, false
	for _, v := range items {
		row := v.(map[string]any)
		if row["direction"] == "收入" {
			foundIncome = true
		}
		if row["direction"] == "支出" {
			foundExpense = true
		}
	}
	if !foundIncome || !foundExpense {
		t.Fatal("incomplete money directions")
	}
	if len(items) != int(before+2) {
		t.Fatal("GM receipt was counted twice", len(items), before+2)
	}
}
func TestPVFRegistryVersionImmutabilityAndSourceBinding(t *testing.T) {
	s, h, _, _ := directFixture(t)
	root := t.TempDir()
	s.config.ServerRoot = root
	raw := []byte("isolated server source")
	if err := os.WriteFile(filepath.Join(root, "Script.pvf"), raw, 0600); err != nil {
		t.Fatal(err)
	}
	sha := sha256.Sum256(raw)
	body := map[string]any{"pvf_version": "isolated-v1", "file_sha256": strings.Repeat("1", 64), "key_set": strings.Repeat("2", 96), "download_url": "https://example.test/update.zip", "client_download_url": "", "source_sha256": hex.EncodeToString(sha[:]), "enabled": true, "confirm": true}
	for i := 0; i < 2; i++ {
		if status, b := call(t, h, "POST", "/manage/v1/pvf/releases", "pvf-1", body); status != 200 {
			t.Fatal(status, b)
		}
	}
	if number(t, s.store.db, "SELECT count(*) FROM gmkit_pvf_releases WHERE pvf_version='isolated-v1'") != 1 {
		t.Fatal("PVF idempotency")
	}
	body["file_sha256"] = strings.Repeat("3", 64)
	if status, _ := call(t, h, "POST", "/manage/v1/pvf/releases", "pvf-conflict", body); status != 409 {
		t.Fatal(status)
	}
	body["pvf_version"] = "isolated-v2"
	body["source_sha256"] = strings.Repeat("4", 64)
	if status, _ := call(t, h, "POST", "/manage/v1/pvf/releases", "pvf-source-conflict", body); status != 409 {
		t.Fatal(status)
	}
	status, b := call(t, h, "GET", "/manage/v1/config/publishing", "", nil)
	if status != 200 {
		t.Fatal(b)
	}
	entry := b["data"].(map[string]any)
	if status, b = call(t, h, "PUT", "/manage/v1/config/publishing", "update-only", map[string]any{"revision": entry["revision"], "value": map[string]any{"update_url": "https://example.test/new.zip", "client_url": ""}}); status != 200 {
		t.Fatal(status, b)
	}
	var address string
	if err := s.store.db.QueryRow("SELECT download_url FROM gmkit_pvf_releases WHERE pvf_version='isolated-v1'").Scan(&address); err != nil || address != "https://example.test/new.zip" {
		t.Fatal("download did not reach player registry", err)
	}
}
