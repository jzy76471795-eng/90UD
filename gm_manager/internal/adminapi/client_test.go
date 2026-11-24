package adminapi

import (
	"context"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestClientRejectsUnsafeEndpoints(t *testing.T) {
	for _, address := range []string{"http://example.com:123", "https://user:pass@example.com", "https://example.com/path", "https://example.com?token=x"} {
		if _, err := NewClient(address, "01234567890123456789012345678901", ""); err == nil {
			t.Fatal(address)
		}
	}
}

func TestHTTPSRequiresTrustedCertificate(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"ok":true,"data":{"language":"go"}}`))
	}))
	defer server.Close()
	token := "01234567890123456789012345678901"
	client, err := NewClient(server.URL, token, "")
	if err != nil {
		t.Fatal(err)
	}
	var data map[string]any
	if client.Do(context.Background(), "GET", "/manage/v1/status", "", nil, &data) == nil {
		t.Fatal("untrusted certificate accepted")
	}
	client.Close()
	certificate := filepath.Join(t.TempDir(), "local-test.pem")
	if err = os.WriteFile(certificate, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	client, err = NewClient(server.URL, token, certificate)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if err = client.Do(context.Background(), "GET", "/manage/v1/status", "", nil, &data); err != nil || data["language"] != "go" {
		t.Fatal(err, data)
	}
}
func TestClientDoesNotReplayUnknownWrite(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(409)
		_, _ = w.Write([]byte(`{"ok":false,"request_id":"same-id","error":{"code":"result_unknown","message":"待核对","unknown":true}}`))
	}))
	defer server.Close()
	client, err := NewClient(server.URL, "01234567890123456789012345678901", "")
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	err = client.Do(context.Background(), "POST", "/manage/v1/actions", "same-id", map[string]any{}, nil)
	e, ok := err.(*ClientError)
	if !ok || !e.Unknown || e.RequestID != "same-id" || calls != 1 {
		t.Fatal(err, calls)
	}
}
