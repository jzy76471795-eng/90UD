package manager

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"newadmin/internal/adminapi"
)

func TestRechargeSavedRedirectChangesWithoutRestart(t *testing.T) {
	s, h := newTestService(t)
	l := &launcherService{manager: s}
	visit := func(method string, tlsOn bool) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, "https://127.0.0.1/recharge?mid=ignored", nil)
		if tlsOn {
			r.TLS = &tls.ConnectionState{}
		} else {
			r.TLS = nil
		}
		w := httptest.NewRecorder()
		l.ServeHTTP(w, r)
		return w
	}
	if w := visit("GET", true); w.Code != 503 || w.Header().Get("Location") != "" {
		t.Fatal(w.Code, w.Header())
	}
	for i, link := range []string{"https://pay.example.test/first?x=1", "http://pay.example.test/second"} {
		status, body := call(t, h, "PUT", "/manage/v1/config/recharge", fmt.Sprintf("recharge-%d", i), map[string]any{"revision": i, "value": map[string]any{"enabled": true, "url": link}})
		if status != 200 {
			t.Fatal(status, body)
		}
		w := visit("GET", true)
		if w.Code != 302 || w.Header().Get("Location") != link || w.Header().Get("Cache-Control") != "no-store" {
			t.Fatal(w.Code, w.Header())
		}
	}
	status, _ := call(t, h, "PUT", "/manage/v1/config/recharge", "recharge-stale", map[string]any{"revision": 1, "value": map[string]any{"enabled": false, "url": ""}})
	if status != 409 {
		t.Fatal("stale recharge edit accepted", status)
	}
	status, body := call(t, h, "PUT", "/manage/v1/config/recharge", "recharge-off", map[string]any{"revision": 2, "value": map[string]any{"enabled": false, "url": "https://pay.example.test/retained"}})
	if status != 200 {
		t.Fatal(status, body)
	}
	if w := visit("HEAD", true); w.Code != 503 || w.Body.Len() != 0 || w.Header().Get("Location") != "" {
		t.Fatal(w.Code, w.Body.String())
	}
	if w := visit("GET", false); w.Code != 426 {
		t.Fatal(w.Code)
	}
	if w := visit("POST", true); w.Code != 405 {
		t.Fatal(w.Code)
	}
}
func TestRechargeRejectsInvalidAndIncompleteSettings(t *testing.T) {
	_, h := newTestService(t)
	values := []map[string]any{
		{"enabled": true, "url": ""}, {"enabled": true, "url": "javascript:alert(1)"}, {"enabled": true, "url": "https://user:pass@example.test/"},
		{"enabled": true, "url": "https://example.test/\r\nLocation:elsewhere"}, {"enabled": true, "url": " https://example.test/"},
		{"enabled": "yes", "url": "https://example.test/"}, {"enabled": false}, {"url": "https://example.test/"}, {"enabled": false, "url": "", "unknown": true},
	}
	for i, value := range values {
		if status, body := call(t, h, "PUT", "/manage/v1/config/recharge", fmt.Sprintf("bad-recharge-%d", i), map[string]any{"revision": 0, "value": value}); status != 400 {
			t.Fatal(value, status, body)
		}
	}
}
func nativeRequest(t *testing.T, s *Service, remote, token string, value any) (int, map[string]any) {
	t.Helper()
	raw, _ := json.Marshal(value)
	r := httptest.NewRequest("POST", "http://127.0.0.1/manage/v1/native/coupon", bytes.NewReader(raw))
	r.RemoteAddr = remote
	r.Header.Set("Authorization", "Bearer "+token)
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	s.Handler().ServeHTTP(w, r)
	var doc map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &doc); err != nil {
		t.Fatal(err, w.Body.String())
	}
	return w.Code, doc
}
func couponData(t *testing.T, s *Service, aid, cid int, code string) map[string]any {
	t.Helper()
	status, doc := nativeRequest(t, s, "127.0.0.1:45231", s.config.Token, map[string]any{"account_id": aid, "character_id": cid, "code": code})
	if status != 200 || doc["ok"] != true {
		t.Fatal(status, doc)
	}
	return doc["data"].(map[string]any)
}
func TestNativeCouponAwardReplayLegacyAndOwnership(t *testing.T) {
	l, aid, cid := launcherFixture(t)
	s := l.manager
	for code, points := range map[string]int{"ABCDEF1234567890": 7, strings.Repeat("A", 32): 9} {
		_, err := s.store.db.Exec("INSERT INTO gmkit_cdks(code,name,points,item_id,item_name,quantity,max_uses,used,enabled,created_at,rewards_json) VALUES(?, 'test',?,0,'',0,1,0,1,'test',?)", code, points, fmt.Sprintf(`[{"kind":"points","amount":%d},{"kind":"gold","amount":11}]`, points))
		if err != nil {
			t.Fatal(err)
		}
	}
	first := couponData(t, s, aid, cid, "ABCD-EF12-3456-7890")
	again := couponData(t, s, aid, cid, "abcdef1234567890")
	a, _ := json.Marshal(first)
	b, _ := json.Marshal(again)
	if !bytes.Equal(a, b) {
		t.Fatal("retry changed receipt", first, again)
	}
	if number(t, s.store.db, "SELECT cera FROM accounts WHERE account_id=?", aid) != 7 || number(t, s.store.db, "SELECT used FROM gmkit_cdks WHERE code='ABCDEF1234567890'") != 1 {
		t.Fatal("duplicate or missing award")
	}
	if number(t, s.store.db, "SELECT COUNT(*) FROM gmkit_redemptions WHERE account_id=?", aid) < 1 {
		t.Fatal("missing redemption")
	}
	if number(t, s.store.db, "SELECT COUNT(*) FROM manager_native_coupon_attempts WHERE account_id=?", aid) != 1 {
		t.Fatal("retry consumed daily allowance")
	}
	// Reusing the same 16-digit code in the launcher remains the same account-wide redemption.
	body, _ := json.Marshal(map[string]any{"code": "ABCDEF1234567890", "character_id": cid, "request_id": strings.Repeat("b", 32)})
	r := httptest.NewRequest("POST", "http://localhost/", bytes.NewReader(body))
	_, err := l.redeemWithRefresh(context.Background(), r, aid, false)
	if nativeCouponFailure(err) != 1 {
		t.Fatal("launcher/native duplicate did not share protection", err)
	}
	body, _ = json.Marshal(map[string]any{"code": strings.Repeat("A", 32), "character_id": cid, "request_id": strings.Repeat("c", 32)})
	r = httptest.NewRequest("POST", "http://localhost/", bytes.NewReader(body))
	if _, err = l.redeemWithRefresh(context.Background(), r, aid, false); err != nil {
		t.Fatal("legacy code broken", err)
	}
	if number(t, s.store.db, "SELECT cera FROM accounts WHERE account_id=?", aid) != 16 {
		t.Fatal("legacy credit wrong")
	}
	status, _ := nativeRequest(t, s, "127.0.0.1:2", s.config.Token, map[string]any{"account_id": aid + 100000, "character_id": cid, "code": "ABCDEF1234567890"})
	if status != 403 {
		t.Fatal("foreign owner accepted", status)
	}
	status, _ = nativeRequest(t, s, "198.51.100.1:2", s.config.Token, map[string]any{"account_id": aid, "character_id": cid, "code": "ABCDEF1234567890"})
	if status != 403 {
		t.Fatal(status)
	}
	status, _ = nativeRequest(t, s, "127.0.0.1:2", "player-token", map[string]any{"account_id": aid, "character_id": cid, "code": "ABCDEF1234567890"})
	if status != 401 {
		t.Fatal(status)
	}
}
func TestNativeCouponConcurrentDeliveryAndLimits(t *testing.T) {
	l, aid, cid := launcherFixture(t)
	s := l.manager
	_, err := s.store.db.Exec(`INSERT INTO gmkit_cdks(code,name,points,item_id,item_name,quantity,max_uses,used,enabled,created_at,rewards_json) VALUES('1234567890ABCDEF','concurrent',13,0,'',0,1,0,1,'test','[{"kind":"points","amount":13}]')`)
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); couponData(t, s, aid, cid, "1234567890ABCDEF") }()
	}
	wg.Wait()
	if number(t, s.store.db, "SELECT cera FROM accounts WHERE account_id=?", aid) != 13 {
		t.Fatal("concurrent duplicate")
	}
	for i := 1; i < 50; i++ {
		couponData(t, s, aid, cid, fmt.Sprintf("%016X", i))
	}
	limited := couponData(t, s, aid, cid, "FFFFFFFFFFFFFFFF")
	if limited["coupon_error"] != float64(2) {
		t.Fatal(limited)
	}
	if result := couponData(t, s, aid, cid, "1234567890ABCDEF"); result["result"] == nil {
		t.Fatal("durable receipt blocked after allowance", result)
	}
}
func TestGeneratedCDKFitsNativeWindow(t *testing.T) {
	s, h, _, _ := directFixture(t)
	status, doc := call(t, h, "POST", "/manage/v1/codes", "native-generation", adminapi.CodeRequest{Kind: "cdk", Name: "native", Quantity: 3, Reward: adminapi.Reward{Points: 1}})
	if status != 200 {
		t.Fatal(status, doc)
	}
	for _, v := range doc["data"].([]any) {
		code := v.(map[string]any)["code"].(string)
		if len(code) != 16 || !nativeCouponPattern.MatchString(code) {
			t.Fatal(code)
		}
		if number(t, s.store.db, "SELECT COUNT(*) FROM gmkit_cdks WHERE code=?", code) != 1 {
			t.Fatal("not published")
		}
	}
}
