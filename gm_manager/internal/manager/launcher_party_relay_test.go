package manager

import (
	"crypto/tls"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
)

func TestPartyRelayGrantUsesAuthenticatedPrivateAdapter(t *testing.T) {
	l, account, _ := launcherFixture(t)
	var issued atomic.Int32
	engine := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer private-control-fixture-token" {
			t.Error("private control authentication missing")
			w.WriteHeader(401)
			return
		}
		var body map[string]any
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("adapter body")
			w.WriteHeader(400)
			return
		}
		if body["token"] != "fixture-player-token" || body["remote_ip"] != "192.0.2.1" {
			w.WriteHeader(401)
			json.NewEncoder(w).Encode(map[string]any{"ok": false, "error": map[string]any{"code": "AUTH_FAILED", "message": "invalid session"}})
			return
		}
		var result any
		switch r.URL.Path {
		case "/api/v1/launcher/resolve":
			result = map[string]any{"account_id": account, "remote_ip": "192.0.2.1", "session_id": strings.Repeat("a", 32), "device_id": strings.Repeat("b", 64)}
		case "/api/v1/launcher/relay_grant":
			if len(body) != 3 || body["request_id"] != strings.Repeat("c", 32) {
				t.Error("player identity fields forwarded")
			}
			issued.Add(1)
			result = map[string]any{"protocol": 1, "relay_url": "https://192.0.2.10:14443", "grant": map[string]any{"fixture": true}}
		default:
			t.Error("unexpected adapter operation")
			w.WriteHeader(404)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"ok": true, "data": result})
	}))
	defer engine.Close()
	_, portText, _ := net.SplitHostPort(strings.TrimPrefix(engine.URL, "http://"))
	port, _ := strconv.Atoi(portText)
	endpoint := filepath.Join(t.TempDir(), "private-control.json")
	raw, _ := json.Marshal(map[string]any{"api_version": 1, "host": "127.0.0.1", "port": port, "token": "private-control-fixture-token"})
	if os.WriteFile(endpoint, raw, 0600) != nil {
		t.Fatal("endpoint fixture")
	}
	l.manager.config.ControlEndpointFile = endpoint
	l.manager.game.client = engine.Client()
	call := func(body, token string) int {
		r := httptest.NewRequest("POST", "https://localhost/launcher/v1/party-relay/grants", strings.NewReader(body))
		r.TLS = &tls.ConnectionState{}
		r.RemoteAddr = "192.0.2.1:1234"
		if token != "" {
			r.Header.Set("Authorization", "Bearer "+token)
		}
		w := httptest.NewRecorder()
		l.ServeHTTP(w, r)
		return w.Code
	}
	valid := `{"request_id":"` + strings.Repeat("c", 32) + `"}`
	if code := call(valid, ""); code != 401 {
		t.Fatal("unauthenticated grant", code)
	}
	if code := call(valid, "wrong-token"); code != 401 {
		t.Fatal("invalid login grant", code)
	}
	for _, body := range []string{`{"request_id":"short"}`, `{"request_id":123}`, `{"request_id":"c!cccccccccccccccc"}`, `{"request_id":"cccccccccccccccc","account_id":1}`, `{"request_id":"cccccccccccccccc","request_id":"dddddddddddddddd"}`} {
		if code := call(body, "fixture-player-token"); code != 400 {
			t.Fatal("invalid launch body", code)
		}
	}
	if issued.Load() != 0 {
		t.Fatal("invalid request reached relay")
	}
	if code := call(valid, "fixture-player-token"); code != 200 || issued.Load() != 1 {
		t.Fatal("authenticated launch", code, issued.Load())
	}
}
