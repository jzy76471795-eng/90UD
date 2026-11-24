package manager

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"newadmin/internal/adminapi"
)

func newTestService(t *testing.T) (*Service, *httptest.Server) {
	t.Helper()
	s, err := New(Config{Listen: "127.0.0.1:0", Token: "0123456789012345678901234567890123456789", DataDirectory: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	h := httptest.NewServer(s.Handler())
	t.Cleanup(h.Close)
	return s, h
}
func call(t *testing.T, h *httptest.Server, method, path, id string, body any) (int, map[string]any) {
	t.Helper()
	var b bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&b).Encode(body); err != nil {
			t.Fatal(err)
		}
	}
	r, _ := http.NewRequest(method, h.URL+path, &b)
	r.Header.Set("Authorization", "Bearer 0123456789012345678901234567890123456789")
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("Idempotency-Key", id)
	resp, err := h.Client().Do(r)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var result map[string]any
	if err = json.NewDecoder(resp.Body).Decode(&result); err != nil {
		t.Fatal(err)
	}
	return resp.StatusCode, result
}
func TestAuthenticationAndUnknownAreExplicit(t *testing.T) {
	_, h := newTestService(t)
	resp, err := h.Client().Get(h.URL + "/manage/v1/status")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 401 {
		t.Fatal(resp.StatusCode)
	}
	status, body := call(t, h, "GET", "/manage/v1/status", "", nil)
	if status != 200 {
		t.Fatal(body)
	}
	d := body["data"].(map[string]any)
	if d["online_accounts"] != nil || d["database_state"] != "unconfigured" || d["language"] != "go" {
		t.Fatal(d)
	}
}
func TestGenerationDurableIdempotentAndNotRedeemed(t *testing.T) {
	s, h := newTestService(t)
	request := adminapi.CodeRequest{Kind: "cdk", Name: "测试奖励", Quantity: 3, Reward: adminapi.Reward{Points: 10}}
	status, first := call(t, h, "POST", "/manage/v1/codes", "codes-1", request)
	if status != 200 {
		t.Fatal(first)
	}
	_, again := call(t, h, "POST", "/manage/v1/codes", "codes-1", request)
	a, _ := json.Marshal(first)
	b, _ := json.Marshal(again)
	if !bytes.Equal(a, b) {
		t.Fatal("replay changed receipt")
	}
	request.Quantity = 4
	status, _ = call(t, h, "POST", "/manage/v1/codes", "codes-1", request)
	if status != 409 {
		t.Fatal("conflicting id accepted", status)
	}
	status, result := call(t, h, "GET", "/manage/v1/codes?kind=cdk", "", nil)
	if status != 200 {
		t.Fatal(result)
	}
	page := result["data"].(map[string]any)
	if page["total"] != float64(3) {
		t.Fatal(page)
	}
	s2, err := New(Config{Listen: "127.0.0.1:0", Token: s.config.Token, DataDirectory: s.config.DataDirectory})
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	if len(s2.store.snapshot().Codes) != 3 {
		t.Fatal("codes not durable")
	}
}
func TestFailedGameWriteNeverBecomesSuccess(t *testing.T) {
	_, h := newTestService(t)
	status, body := call(t, h, "POST", "/manage/v1/actions", "gold-1", adminapi.Action{Name: "currency.credit", Currency: "gold", CharacterID: 42, Amount: 10, Confirm: true})
	if status != 503 || body["ok"] != false {
		t.Fatal(status, body)
	}
}
func TestConfigurationRevisionsAndUnknownFields(t *testing.T) {
	_, h := newTestService(t)
	status, body := call(t, h, "PUT", "/manage/v1/config/announcement.launcher", "config-1", map[string]any{"revision": 0, "value": map[string]any{"enabled": true, "title": "测试", "content": "公告"}})
	if status != 200 {
		t.Fatal(body)
	}
	status, _ = call(t, h, "PUT", "/manage/v1/config/announcement.launcher", "config-2", map[string]any{"revision": 0, "value": map[string]any{"enabled": true, "content": "覆盖"}})
	if status != 409 {
		t.Fatal("stale edit accepted", status)
	}
	status, _ = call(t, h, "PUT", "/manage/v1/config/unknown", "config-3", map[string]any{"revision": 0, "value": map[string]any{}})
	if status != 404 {
		t.Fatal(status)
	}
}
func TestDatabaseNeverCreatedByRead(t *testing.T) {
	dir := t.TempDir()
	db := filepath.Join(dir, "absent.db")
	reader := gameReader{database: db}
	if _, err := reader.open(); err == nil {
		t.Fatal("missing DB accepted")
	}
}
