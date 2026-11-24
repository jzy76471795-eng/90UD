package manager

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"newadmin/internal/adminapi"
)

func cleanupFixture(t *testing.T) (*Service, *httptest.Server) {
	t.Helper()
	s, h, account, character := directFixture(t)
	_, err := s.store.db.Exec(`DELETE FROM manager_gold_ledger;
		DELETE FROM gmkit_cera_ledger;
		DELETE FROM account_revive_coin_ledger;
		INSERT INTO manager_gold_ledger(account_id,character_id,balance_before,balance_after,delta,created_at)
		VALUES(?,?,0,1,1,'2025-12-31T15:59:59.999999999Z'),(?,?,1,2,1,'2026-01-01T00:00:00+08:00');
		INSERT INTO gmkit_cera_ledger(account_id,cera_before,cera_after,cera_delta,token_before,token_after,token_delta,happy_before,happy_after,happy_delta,created_at)
		VALUES(?,0,1,1,0,2,2,0,3,3,'2025-12-30 00:00:00'),(?,1,2,1,2,3,1,3,4,1,'2026-01-01 00:00:00');
		INSERT INTO account_revive_coin_ledger(account_id,character_id,delta,balance_before,balance_after,reason,created_at)
		VALUES(?,?,1,0,1,'test','2025-12-30 00:00:00'),(?,?,1,1,2,'test','2026-01-01 00:00:00')`,
		account, character, account, character, account, account, account, character, account, character)
	if err != nil {
		t.Fatal(err)
	}
	return s, h
}

func cleanupPlan(t *testing.T, h *httptest.Server, kinds string) adminapi.RecordCleanupPlan {
	t.Helper()
	status, body := call(t, h, "GET", "/manage/v1/records/cleanup?before=2026-01-01&"+kinds, "", nil)
	if status != 200 {
		t.Fatal(status, body)
	}
	raw, err := json.Marshal(body["data"])
	if err != nil {
		t.Fatal(err)
	}
	var plan adminapi.RecordCleanupPlan
	if err := json.Unmarshal(raw, &plan); err != nil {
		t.Fatal(err)
	}
	return plan
}

func TestRecordCleanupRetainsRecentAndNonLedgerData(t *testing.T) {
	s, h := cleanupFixture(t)
	accountBalance := number(t, s.store.db, "SELECT sum(cera+token_cera) FROM accounts")
	walletBalance := number(t, s.store.db, "SELECT sum(gold) FROM character_wallets")
	mail := number(t, s.store.db, "SELECT count(*) FROM gmkit_mails")
	cdk := number(t, s.store.db, "SELECT count(*) FROM gmkit_redemptions")
	grants := number(t, s.store.db, "SELECT count(*) FROM gmkit_value_credits")
	plan := cleanupPlan(t, h, "kind=gold&kind=currency&kind=revive")
	for _, target := range plan.Targets {
		if target.Count != 1 {
			t.Fatal(plan)
		}
	}
	request := adminapi.RecordCleanupRequest{Plan: plan, BackupSHA256: strings.Repeat("a", 64), Confirm: true}
	// A late, backdated insert is outside the confirmed range of row identifiers.
	_, err := s.store.db.Exec(`INSERT INTO manager_gold_ledger(account_id,character_id,balance_before,balance_after,delta,created_at)
		SELECT account_id,character_id,balance_before,balance_after,delta,'2025-12-30T00:00:00Z'
		FROM manager_gold_ledger ORDER BY id DESC LIMIT 1`)
	if err != nil {
		t.Fatal(err)
	}
	for range 2 {
		status, body := call(t, h, "POST", "/manage/v1/records/cleanup", "cleanup-1", request)
		if status != 200 || len(body["data"].(map[string]any)["deleted"].([]any)) != 3 {
			t.Fatal(status, body)
		}
	}
	if rows := number(t, s.store.db, "SELECT count(*) FROM manager_gold_ledger"); rows != 2 {
		t.Fatal("gold rows", rows)
	}
	if rows := number(t, s.store.db, "SELECT count(*) FROM gmkit_cera_ledger"); rows != 1 {
		t.Fatal("currency rows", rows)
	}
	if rows := number(t, s.store.db, "SELECT count(*) FROM account_revive_coin_ledger"); rows != 1 {
		t.Fatal("revive rows", rows)
	}
	for _, check := range []struct {
		query string
		want  int64
	}{
		{"SELECT sum(cera+token_cera) FROM accounts", accountBalance},
		{"SELECT sum(gold) FROM character_wallets", walletBalance},
		{"SELECT count(*) FROM gmkit_mails", mail},
		{"SELECT count(*) FROM gmkit_redemptions", cdk},
		{"SELECT count(*) FROM gmkit_value_credits", grants},
		{"SELECT count(*) FROM manager_audit WHERE request_id='cleanup-1' AND action='records.cleanup'", 1},
	} {
		if got := number(t, s.store.db, check.query); got != check.want {
			t.Fatal(check.query, got, check.want)
		}
	}
	if status, body := call(t, h, "GET", "/manage/v1/receipts/cleanup-1", "", nil); status != 200 {
		t.Fatal(status, body)
	}
}

func TestRecordCleanupRejectsScopeChangesAndRollsBack(t *testing.T) {
	s, h := cleanupFixture(t)
	for _, query := range []string{"before=2026-01-01&kind=raid", "before=9999-12-31&kind=gold", "before=2026-01-01&kind=gold&kind=gold"} {
		if status, body := call(t, h, "GET", "/manage/v1/records/cleanup?"+query, "", nil); status != 400 {
			t.Fatal(status, body)
		}
	}
	plan := cleanupPlan(t, h, "kind=gold&kind=revive")
	request := adminapi.RecordCleanupRequest{Plan: plan, BackupSHA256: strings.Repeat("a", 64), Confirm: true}
	request.Plan.Before = "2026-01-02"
	if status, body := call(t, h, "POST", "/manage/v1/records/cleanup", "fake-plan", request); status != 400 {
		t.Fatal(status, body)
	}
	request.Plan = plan
	_, err := s.store.db.Exec(`DELETE FROM account_revive_coin_ledger WHERE ledger_id=(SELECT MIN(ledger_id) FROM account_revive_coin_ledger)`)
	if err != nil {
		t.Fatal(err)
	}
	if status, body := call(t, h, "POST", "/manage/v1/records/cleanup", "stale-plan", request); status != 409 {
		t.Fatal(status, body)
	}
	if number(t, s.store.db, "SELECT count(*) FROM manager_gold_ledger") != 2 {
		t.Fatal("earlier delete was not rolled back")
	}
	if _, err := s.store.db.Exec(`CREATE TABLE cleanup_fk_guard(id INTEGER PRIMARY KEY REFERENCES manager_gold_ledger(id) ON DELETE CASCADE)`); err != nil {
		t.Fatal(err)
	}
	if status, body := call(t, h, "GET", "/manage/v1/records/cleanup?before=2026-01-01&kind=gold", "", nil); status != 409 {
		t.Fatal(status, body)
	}
}
