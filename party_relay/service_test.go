package main

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/gorilla/websocket"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

type serviceHarness struct {
	t      *testing.T
	s      *service
	http   *httptest.Server
	key    []byte
	client *http.Client
	dial   websocket.Dialer
	seq    uint64
}

func harness(t *testing.T) *serviceHarness {
	key := bytes.Repeat([]byte{0x31}, 32)
	s := newService(key, 16)
	server := httptest.NewTLSServer(s)
	pool := x509.NewCertPool()
	pool.AddCert(server.Certificate())
	h := &serviceHarness{t: t, s: s, http: server, key: key, client: server.Client(), dial: websocket.Dialer{TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}, Subprotocols: []string{"90us-rlv2-v1"}}}
	t.Cleanup(func() { s.close(); server.Close() })
	return h
}
func (h *serviceHarness) control(method, path string, value any) (int, []byte) {
	var body []byte
	if value != nil {
		body, _ = json.Marshal(value)
	}
	r, _ := http.NewRequest(method, h.http.URL+path, bytes.NewReader(body))
	signRequest(r, body, h.key, "control-v1")
	resp, e := h.client.Do(r)
	if e != nil {
		h.t.Fatal(e)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, raw
}
func loginFor(account uint32) string { return fmt.Sprintf("instance-%032x", account) }
func (h *serviceHarness) grant(account uint32) sessionGrant {
	code, raw := h.control("POST", "/v1/control/session", sessionRequest{Request: randomHex(16), Account: account, Login: loginFor(account), MTU: 512})
	if code != 201 {
		h.t.Fatalf("issue status %d: %s", code, raw)
	}
	var g sessionGrant
	if json.Unmarshal(raw, &g) != nil {
		h.t.Fatal("grant shape")
	}
	return g
}
func (h *serviceHarness) bind(g sessionGrant, account, actor, key, channel uint32) {
	code, raw := h.control("POST", "/v1/control/bind", map[string]any{"member_id": g.Member, "binding": binding{Account: account, Login: loginFor(account), Actor: actor, Key: key, Channel: channel}})
	if code != 200 {
		h.t.Fatalf("bind %d: %s", code, raw)
	}
}

type testEdge struct {
	g        sessionGrant
	c        *websocket.Conn
	up, down *Codec
}

func (h *serviceHarness) edge(g sessionGrant) *testEdge {
	headers := http.Header{}
	headers.Set("Authorization", "Bearer "+g.Token)
	conn, resp, e := h.dial.Dial(strings.Replace(h.http.URL, "https://", "wss://", 1)+"/v1/edge", headers)
	if e != nil {
		h.t.Fatalf("upgrade: %v status: %v", e, resp)
	}
	h.t.Cleanup(func() { conn.Close() })
	secret, _ := hex.DecodeString(g.Secret)
	label := fmt.Sprintf("product/boot-%s/member-%d", g.Boot, g.Member)
	return &testEdge{g: g, c: conn, up: newCodec(secret, label+"/to-relay", g.Session, 1, g.MTU, &Budget{limit: 8 * 1024 * 1024}), down: newCodec(secret, label+"/from-relay", g.Session, 1, g.MTU, &Budget{limit: 8 * 1024 * 1024})}
}
func (h *serviceHarness) routes(members, pending []Member) (int, map[string]any) {
	h.seq++
	s := Snapshot{Boot: h.s.registry.boot, Version: h.seq, Issued: time.Now().UnixMilli(), Lease: 5000, Members: members, Pending: pending}
	s.sign(h.key)
	code, raw := h.control("PUT", "/v1/control/routes", s)
	var status map[string]any
	json.Unmarshal(raw, &status)
	return code, status
}
func (e *testEdge) send(t *testing.T, target uint32, scope, epoch uint64, p []byte) [][]byte {
	frames, err := e.up.encode(e.g.Member, target, scope, epoch, p)
	if err != nil {
		t.Fatal(err)
	}
	for _, b := range frames {
		if err = e.c.WriteMessage(websocket.BinaryMessage, b); err != nil {
			t.Fatal(err)
		}
	}
	return frames
}
func (e *testEdge) receive(t *testing.T) []byte {
	e.c.SetReadDeadline(time.Now().Add(2 * time.Second))
	for {
		kind, b, err := e.c.ReadMessage()
		if err != nil {
			t.Fatal(err)
		}
		if kind != websocket.BinaryMessage {
			t.Fatal("nonbinary")
		}
		_, p, err := e.down.decode(b, time.Now(), func(Header) bool { return true })
		if err != nil {
			t.Fatal(err)
		}
		if p != nil {
			return p
		}
	}
}
func await(t *testing.T, predicate func() bool) {
	deadline := time.Now().Add(time.Second)
	for !predicate() {
		if time.Now().After(deadline) {
			t.Fatal("bounded condition timeout")
		}
		time.Sleep(time.Millisecond)
	}
}

func TestControlAuthenticationReplayAndStrictShape(t *testing.T) {
	h := harness(t)
	r, _ := http.NewRequest("GET", h.http.URL+"/v1/control/status", nil)
	resp, e := h.client.Do(r)
	if e != nil {
		t.Fatal(e)
	}
	resp.Body.Close()
	if resp.StatusCode != 401 {
		t.Fatal("missing auth accepted")
	}
	signRequest(r, nil, h.key, "control-v1")
	resp, e = h.client.Do(r)
	if e != nil {
		t.Fatal(e)
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatal("valid auth rejected")
	}
	retry := r.Clone(r.Context())
	resp, e = h.client.Do(retry)
	if e != nil {
		t.Fatal(e)
	}
	resp.Body.Close()
	if resp.StatusCode != 401 {
		t.Fatal("control replay accepted")
	}
	for _, body := range []string{`{"account_id":1,"account_id":2}`, `{"account_id":1,"extra":0}`, `{} {}`} {
		req, _ := http.NewRequest("POST", h.http.URL+"/v1/control/session", strings.NewReader(body))
		signRequest(req, []byte(body), h.key, "control-v1")
		result, e := h.client.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		result.Body.Close()
		if result.StatusCode != 400 {
			t.Fatalf("ambiguous body admitted: %d", result.StatusCode)
		}
	}
}
func TestSingleUseGrantAndLoginReplacementRevokesOld(t *testing.T) {
	h := harness(t)
	g := h.grant(1)
	h.edge(g)
	headers := http.Header{}
	headers.Set("Authorization", "Bearer "+g.Token)
	if _, resp, e := h.dial.Dial(strings.Replace(h.http.URL, "https://", "wss://", 1)+"/v1/edge", headers); e == nil || resp == nil || resp.StatusCode != 401 {
		t.Fatal("launch token reused")
	}
	replacement := h.grant(1)
	if replacement.Member == g.Member || replacement.Secret == g.Secret || replacement.Session == g.Session {
		t.Fatal("login replacement reused key domain")
	}
	if h.s.registry.lookup(g.Member) != nil {
		t.Fatal("old authenticated login retained")
	}
	unit := newRegistry(h.key, 2)
	expired, _ := unit.issue(sessionRequest{Request: randomHex(16), Account: 2, Login: strings.Repeat("a", 16), MTU: 512}, time.Now().Add(-time.Minute))
	if _, e := unit.redeem(expired.Token, time.Now()); e == nil {
		t.Fatal("expired token accepted")
	}
}
func TestNativeBindingAndScopeIsolation(t *testing.T) {
	h := harness(t)
	a, b := h.grant(1), h.grant(2)
	members := []Member{{ID: a.Member, Scope: 1, Epoch: 1, Active: true}, {ID: b.Member, Scope: 1, Epoch: 1, Active: true}}
	if status, _ := h.routes(members, nil); status != 409 {
		t.Fatal("unbound member route accepted")
	}
	h.bind(a, 1, 101, 71, 10)
	h.bind(b, 2, 102, 72, 11)
	if status, _ := h.routes(members, nil); status != 409 {
		t.Fatal("cross native channel accepted")
	}
	h.bind(b, 2, 102, 72, 10)
	if status, _ := h.routes(members, nil); status != 200 {
		t.Fatal("valid scope refused")
	}
	code, _ := h.control("POST", "/v1/control/bind", map[string]any{"member_id": a.Member, "binding": binding{Account: 1, Login: loginFor(1), Actor: 103, Key: 73, Channel: 10}})
	if code != 409 {
		t.Fatal("live character replaced without route revocation")
	}
}

func TestBoundedPrivateStatusPagesAndEpochRecovery(t *testing.T) {
	h := harness(t)
	a, b := h.grant(1), h.grant(2)
	h.bind(a, 1, 101, 71, 10)
	h.bind(b, 2, 102, 72, 10)
	if code, _ := h.routes([]Member{{ID: a.Member, Scope: 1, Epoch: 37, Active: true}, {ID: b.Member, Scope: 1, Epoch: 37, Active: true}}, nil); code != 200 {
		t.Fatal("routes")
	}
	if code, _ := h.routes([]Member{}, []Member{}); code != 200 {
		t.Fatal("revocation")
	}
	code, raw := h.control("POST", "/v1/control/status", map[string]any{"member_ids": []uint32{a.Member}})
	var page map[string]any
	if code != 200 || json.Unmarshal(raw, &page) != nil {
		t.Fatal("page", code)
	}
	rows := page["sessions"].([]any)
	if len(rows) != 1 || uint32(rows[0].(map[string]any)["member_id"].(float64)) != a.Member {
		t.Fatal("status leaked other session")
	}
	authority := page["authority"].(map[string]any)
	if authority["members"] != nil || authority["pending"] != nil || authority["max_epoch"] != float64(37) {
		t.Fatal("unbounded page or lost tombstone epoch")
	}
	for _, ids := range []any{nil, []uint32{a.Member, a.Member}, []uint32{0}, make([]uint32, 17)} {
		if code, _ := h.control("POST", "/v1/control/status", map[string]any{"member_ids": ids}); code != 400 {
			t.Fatal("invalid status page accepted", code)
		}
	}
}
func TestIndependentWSSPeersFragmentsPendingCancelAndReplay(t *testing.T) {
	h := harness(t)
	a, b := h.grant(1), h.grant(2)
	h.bind(a, 1, 101, 71, 10)
	h.bind(b, 2, 102, 72, 10)
	ea, eb := h.edge(a), h.edge(b)
	await(t, func() bool {
		return h.s.registry.lookup(a.Member).connected.Load() && h.s.registry.lookup(b.Member).connected.Load()
	})
	route := []Member{{ID: a.Member, Scope: 1, Epoch: 1, Active: true}, {ID: b.Member, Scope: 1, Epoch: 1, Active: true}}
	code, status := h.routes(route, nil)
	if code != 200 {
		t.Fatal("routes")
	}
	view := status["route_view"]
	for _, size := range []int{64, 512, 8192} {
		p := bytes.Repeat([]byte{byte(size % 251)}, size)
		ea.send(t, b.Member, 1, 1, p)
		if !bytes.Equal(p, eb.receive(t)) {
			t.Fatal("native datagram boundary")
		}
	}
	pending := []Member{{ID: a.Member, Scope: 2, Epoch: 2}, {ID: b.Member, Scope: 2, Epoch: 2}}
	if code, _ = h.routes(route, pending); code != 200 {
		t.Fatal("pending install")
	}
	ea.send(t, b.Member, 1, 1, []byte("old route during prepare"))
	if string(eb.receive(t)) != "old route during prepare" {
		t.Fatal("prepare replaced active")
	}
	if code, _ = h.routes(route, nil); code != 200 {
		t.Fatal("pending cancel")
	}
	ea.send(t, b.Member, 1, 1, []byte("after cancel"))
	if string(eb.receive(t)) != "after cancel" {
		t.Fatal("cancel revoked party")
	}
	before := h.s.forwarded.Load()
	frames := ea.send(t, b.Member, 1, 1, []byte("capture"))
	eb.receive(t)
	await(t, func() bool { return h.s.forwarded.Load() > before })
	rejects := h.s.rejected.Load()
	ea.c.WriteMessage(websocket.BinaryMessage, frames[0])
	await(t, func() bool { return h.s.rejected.Load() > rejects })
	code, status = h.routes(route, nil)
	if code != 200 {
		t.Fatal("heartbeat")
	}
	if status["route_view"] == view {
		t.Fatal("pending revision missing")
	}
	stable := status["route_view"]
	_, status = h.routes(route, nil)
	if status["route_view"] != stable {
		t.Fatal("heartbeat advanced client install view")
	}
	raw, _ := json.Marshal(h.s.status())
	for _, secret := range []string{a.Secret, a.Token, b.Secret, b.Token, hex.EncodeToString(h.key)} {
		if strings.Contains(string(raw), secret) {
			t.Fatal("status exposed secret")
		}
	}
}
func TestAuthorityTimeoutAndUnauthenticatedResourceBounds(t *testing.T) {
	h := harness(t)
	a, b := h.grant(1), h.grant(2)
	h.bind(a, 1, 101, 71, 10)
	h.bind(b, 2, 102, 72, 10)
	route := []Member{{ID: a.Member, Scope: 1, Epoch: 1, Active: true}, {ID: b.Member, Scope: 1, Epoch: 1, Active: true}}
	h.routes(route, nil)
	h.s.registry.authority.mu.Lock()
	h.s.registry.authority.heartbeat = time.Now().Add(-4 * time.Second)
	h.s.registry.authority.mu.Unlock()
	if h.s.registry.authority.permits(Header{Source: a.Member, Target: b.Member, Scope: 1, Epoch: 1}, time.Now()) {
		t.Fatal("expired trusted heartbeat allowed")
	}
	if h.s.edgeState(h.s.registry.lookup(a.Member))["active"] != false {
		t.Fatal("expired edge view")
	}
	budget := &Budget{limit: 100}
	q := newBudgetQueue(budget)
	if !q.put(Packet{Data: make([]byte, 80)}) {
		t.Fatal("queue first")
	}
	if q.put(Packet{Data: make([]byte, 30)}) || budget.bytes.Load() != 0 {
		t.Fatal("global queue overflow did not release all ownership")
	}
	aLimit := &admission{}
	now := time.Now()
	for i := 0; i < 256; i++ {
		if !aLimit.accept("127.0.0.1:1234", now) {
			t.Fatal("burst bound too early")
		}
	}
	if aLimit.accept("127.0.0.1:1234", now) {
		t.Fatal("source budget bypass")
	}
}
