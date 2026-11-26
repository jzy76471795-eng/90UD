package manager

// The player API has its own handler, TLS identity and authentication boundary.
// It never exposes the administrator token or forwards player-selected commands.
import (
	"bytes"
	"context"
	"crypto/rsa"
	"crypto/tls"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

type launcherService struct {
	manager   *Service
	key       *rsa.PrivateKey
	host      string
	slots     chan struct{}
	downloads chan struct{}
	rateMu    sync.Mutex
	rates     map[string]launcherRate
}
type launcherRate struct {
	start time.Time
	count int
}
type launcherIdentity struct {
	AccountID     int    `json:"account_id"`
	Account       string `json:"account"`
	SessionID     string `json:"session_id"`
	RemoteIP      string `json:"remote_ip"`
	DeviceID      string `json:"device_id"`
	IssuedAt      string `json:"issued_at"`
	ExpiresAt     string `json:"expires_at"`
	AdvertiseHost string `json:"advertise_host"`
}

func (s *Service) LauncherHandler() (http.Handler, error) {
	if !s.config.DirectDatabase {
		return nil, fmt.Errorf("player API requires the existing game database")
	}
	ip := net.ParseIP(s.config.LauncherPublicHost)
	if ip == nil || ip.To4() == nil {
		return nil, fmt.Errorf("launcher_public_host must be the published IPv4 address")
	}
	pair, err := tls.LoadX509KeyPair(s.config.LauncherCertificate, s.config.LauncherKey)
	if err != nil {
		return nil, fmt.Errorf("launcher TLS identity unavailable: %w", err)
	}
	key, ok := pair.PrivateKey.(*rsa.PrivateKey)
	if !ok || key.N.BitLen() < 2048 {
		return nil, fmt.Errorf("launcher signing identity requires RSA 2048 or stronger")
	}
	if err := s.validateLauncherMigration(context.Background()); err != nil {
		return nil, err
	}
	return newLauncherService(s, key, ip.String()), nil
}

// Retiring a worker must never silently discard queued awards or enabled legacy
// schedules. The current manager writes its own rules; reconcile older rules
// before enabling the replacement player listener.
func (s *Service) validateLauncherMigration(ctx context.Context) error {
	for _, section := range []string{"registrationBonus", "welfare"} {
		doc, err := getGameConfig(ctx, s.store.db, section)
		if err != nil {
			return err
		}
		if doc["enabled"] == true {
			return fmt.Errorf("legacy %s rule must be migrated before launcher cutover", section)
		}
	}
	for _, query := range []string{
		"SELECT COUNT(*) FROM gmkit_first_role_rewards WHERE claimed_at IS NULL",
		"SELECT COUNT(*) FROM gmkit_schedules WHERE json_extract(document,'$.enabled')=1",
		"SELECT COUNT(*) FROM gmkit_announcement_schedule WHERE json_extract(document,'$.enabled')=1",
	} {
		var count int
		if err := s.store.db.QueryRowContext(ctx, query).Scan(&count); err != nil {
			return err
		}
		if count > 0 {
			return fmt.Errorf("pending legacy awards or schedules must be reconciled before launcher cutover")
		}
	}
	_, err := s.store.db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS launcher_account_recovery(account_id INTEGER PRIMARY KEY REFERENCES accounts(account_id),code_hash TEXT NOT NULL UNIQUE,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS launcher_security_questions(account_id INTEGER PRIMARY KEY REFERENCES accounts(account_id),question_id TEXT NOT NULL,answer_hash TEXT NOT NULL,failed_attempts INTEGER NOT NULL DEFAULT 0,locked_until REAL NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);`)
	return err
}

func newLauncherService(s *Service, key *rsa.PrivateKey, host string) *launcherService {
	return &launcherService{manager: s, key: key, host: host, slots: make(chan struct{}, 3), downloads: make(chan struct{}, 3), rates: map[string]launcherRate{}}
}

func (l *launcherService) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/recharge" {
		l.serveRecharge(w, r)
		return
	}
	if r.TLS != nil && strings.HasPrefix(r.URL.Path, launcherResourcePrefix) && (r.Method == http.MethodGet || r.Method == http.MethodHead) {
		l.serveResourceUpdate(w, r)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	var result any
	var err error
	status := http.StatusOK
	if r.TLS == nil {
		err = fail(426, "HTTPS_REQUIRED", "HTTPS required")
	} else {
		ip, e := launcherRemoteIP(r)
		if e != nil {
			err = e
		} else {
			ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
			defer cancel()
			result, status, err = l.dispatch(ctx, r, ip)
		}
	}
	if err != nil {
		failure := &apiFailure{Status: 500, Code: "INTERNAL_ERROR", Message: "launcher request failed"}
		var known *apiFailure
		if errors.As(err, &known) {
			failure = known
		}
		status = failure.Status
		result = map[string]any{"error": map[string]string{"code": failure.Code, "message": failure.Message}}
	}
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(result)
}

func launcherRemoteIP(r *http.Request) (string, error) {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return "", fail(400, "INVALID_REQUEST", "invalid peer address")
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return "", fail(400, "INVALID_REQUEST", "invalid peer address")
	}
	// Never trust X-Forwarded-For or a client-supplied IP/device override.
	return ip.String(), nil
}

func launcherBody(r *http.Request, required []string, optional ...string) (map[string]any, error) {
	raw, err := io.ReadAll(io.LimitReader(r.Body, 65537))
	if err != nil || len(raw) == 0 || len(raw) > 65536 || !utf8.Valid(raw) {
		return nil, fail(400, "INVALID_REQUEST", "invalid JSON body")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	token, err := dec.Token()
	if err != nil || token != json.Delim('{') {
		return nil, fail(400, "INVALID_REQUEST", "JSON object required")
	}
	allowed := map[string]bool{}
	for _, name := range append(append([]string{}, required...), optional...) {
		allowed[name] = true
	}
	result := map[string]any{}
	for dec.More() {
		t, err := dec.Token()
		name, ok := t.(string)
		if err != nil || !ok || !allowed[name] {
			return nil, fail(400, "INVALID_REQUEST", "unknown field")
		}
		if _, exists := result[name]; exists {
			return nil, fail(400, "INVALID_REQUEST", "duplicate field")
		}
		var value any
		if dec.Decode(&value) != nil {
			return nil, fail(400, "INVALID_REQUEST", "invalid field")
		}
		result[name] = value
	}
	if _, err = dec.Token(); err != nil {
		return nil, fail(400, "INVALID_REQUEST", "invalid JSON body")
	}
	var extra any
	if dec.Decode(&extra) != io.EOF {
		return nil, fail(400, "INVALID_REQUEST", "trailing JSON data")
	}
	for _, name := range required {
		if _, ok := result[name]; !ok {
			return nil, fail(400, "INVALID_REQUEST", "missing field")
		}
	}
	return result, nil
}

func launcherString(value any, maximum int) (string, error) {
	v, ok := value.(string)
	if !ok || utf8.RuneCountInString(v) > maximum {
		return "", fail(400, "INVALID_REQUEST", "invalid text field")
	}
	return v, nil
}
func launcherInteger(value any, min, max int64) (int64, error) {
	n, ok := value.(json.Number)
	if !ok {
		return 0, fail(400, "INVALID_REQUEST", "integer required")
	}
	v, err := n.Int64()
	if err != nil || v < min || v > max {
		return 0, fail(400, "INVALID_REQUEST", "integer out of range")
	}
	return v, nil
}
func (l *launcherService) limited(ip, kind string, window time.Duration, maximum int) error {
	l.rateMu.Lock()
	defer l.rateMu.Unlock()
	now := time.Now()
	for k, v := range l.rates {
		if now.Sub(v.start) > 10*time.Minute {
			delete(l.rates, k)
		}
	}
	k := kind + ":" + ip
	v, exists := l.rates[k]
	if !exists && len(l.rates) >= 8192 {
		return fail(429, "RATE_LIMITED", "request rate limit")
	}
	if now.Sub(v.start) >= window {
		v = launcherRate{start: now}
	}
	if v.count >= maximum {
		return fail(429, "RATE_LIMITED", "request rate limit")
	}
	v.count++
	l.rates[k] = v
	return nil
}
func (l *launcherService) acquire() error {
	select {
	case l.slots <- struct{}{}:
		return nil
	default:
		return fail(429, "REGISTRATION_BUSY", "authentication busy")
	}
}
func (l *launcherService) release() { <-l.slots }

func (l *launcherService) engine(ctx context.Context, action string, body any, out any) error {
	// Read the current per-process control token; it is never sent to players.
	raw, err := os.ReadFile(l.manager.config.ControlEndpointFile)
	if err != nil || len(raw) > 16384 {
		return fail(503, "SERVER_NOT_READY", "game service unavailable")
	}
	var endpoint struct {
		Version int    `json:"api_version"`
		Host    string `json:"host"`
		Port    int    `json:"port"`
		Token   string `json:"token"`
	}
	if json.Unmarshal(raw, &endpoint) != nil || endpoint.Version != 1 || endpoint.Port < 1 || endpoint.Port > 65535 || len(endpoint.Token) < 16 || (endpoint.Host != "127.0.0.1" && endpoint.Host != "localhost" && endpoint.Host != "0.0.0.0") {
		return fail(503, "SERVER_NOT_READY", "invalid game endpoint")
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", "http://127.0.0.1:"+strconv.Itoa(endpoint.Port)+"/api/v1/launcher/"+action, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+endpoint.Token)
	req.Header.Set("Content-Type", "application/json")
	client := l.manager.game.client
	if action == "assist" {
		// A full equipment tab can require first-use PVF and material lookups.
		assistClient := *client
		assistClient.Timeout = 10 * time.Second
		client = &assistClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return fail(503, "SERVER_NOT_READY", "game service unavailable")
	}
	defer resp.Body.Close()
	raw, err = io.ReadAll(io.LimitReader(resp.Body, 2<<20+1))
	if err != nil || len(raw) > 2<<20 {
		return fail(502, "SERVER_NOT_READY", "incomplete game response")
	}
	var envelope struct {
		OK    bool            `json:"ok"`
		Data  json.RawMessage `json:"data"`
		Error struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if json.Unmarshal(raw, &envelope) != nil {
		return fail(502, "SERVER_NOT_READY", "invalid game response")
	}
	if resp.StatusCode != 200 || !envelope.OK {
		code := envelope.Error.Code
		if code == "" {
			code = "SERVER_NOT_READY"
		}
		status := resp.StatusCode
		if status < 400 || status > 599 {
			status = 502
		}
		return fail(status, code, envelope.Error.Message)
	}
	if out != nil {
		return json.Unmarshal(envelope.Data, out)
	}
	return nil
}

func (l *launcherService) identity(ctx context.Context, r *http.Request, ip string) (launcherIdentity, error) {
	var id launcherIdentity
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, "Bearer ") || len(auth) > 4103 {
		return id, fail(401, "AUTH_FAILED", "game login required")
	}
	err := l.engine(ctx, "resolve", map[string]any{"token": strings.TrimPrefix(auth, "Bearer "), "remote_ip": ip}, &id)
	if err != nil {
		return id, err
	}
	if id.AccountID < 1 || id.RemoteIP != ip {
		return id, fail(401, "AUTH_FAILED", "invalid game identity")
	}
	err = l.policy(ctx, l.manager.store.db, id.AccountID, ip, id.DeviceID, true)
	return id, err
}

func (l *launcherService) dispatch(ctx context.Context, r *http.Request, ip string) (any, int, error) {
	path := r.URL.Path
	method := r.Method
	if path == "/launcher/v1/recharge" && method == "GET" {
		entry, err := readConfig(ctx, l.manager.store.db, "recharge")
		if err != nil {
			return nil, 0, err
		}
		if len(entry.Value) == 0 {
			return map[string]any{"enabled": false, "url": ""}, 200, nil
		}
		if err = validateRecharge(entry.Value); err != nil {
			return nil, 0, err
		}
		if entry.Value["enabled"] != true {
			return map[string]any{"enabled": false, "url": ""}, 200, nil
		}
		return map[string]any{"enabled": true, "url": entry.Value["url"]}, 200, nil
	}
	if path == "/launcher/v1/meta" && method == "GET" {
		result, err := l.meta(ctx)
		return result, 200, err
	}
	if path == "/launcher/v1/registration" && method == "GET" {
		doc, err := l.registrationPolicy(ctx, l.manager.store.db)
		return map[string]any{"closed": doc.Closed, "code_required": doc.CodeRequired}, 200, err
	}
	if path == "/launcher/v1/multi-open" && method == "GET" {
		doc, err := getGameConfig(ctx, l.manager.store.db, "multiOpenPolicy")
		enabled := doc["enabled"] != false
		limit := 1
		if enabled {
			limit = 2
		}
		return map[string]any{"enabled": enabled, "configured": len(doc) > 0, "max_games": limit, "scope": "device", "existing_sessions_preserved": true, "device_source": "launcher-reported"}, 200, err
	}
	if path == "/launcher/v1/notices" && method == "GET" {
		result, err := l.notices(ctx)
		return result, 200, err
	}
	if path == "/launcher/v1/accounts" && method == "POST" {
		result, err := l.register(ctx, r, ip)
		return result, 201, err
	}
	if path == "/launcher/v1/accounts/recovery" && method == "POST" {
		return nil, 403, fail(403, "RECOVERY_DISABLED", "use security question recovery")
	}
	if path == "/launcher/v1/accounts/security-recovery" && method == "POST" {
		result, err := l.recoverSecurity(ctx, r, ip)
		return result, 200, err
	}
	if path == "/launcher/v1/sessions" && method == "POST" {
		result, err := l.createSession(ctx, r, ip)
		return result, 201, err
	}
	if path == "/launcher/v1/pvf/latest" && method == "GET" {
		result, err := l.pvfLatest(ctx)
		return result, 200, err
	}
	if strings.HasPrefix(path, "/launcher/v1/patches/") && method == "GET" {
		result, err := l.patchBlob(ctx, strings.TrimPrefix(path, "/launcher/v1/patches/"))
		return result, 200, err
	}
	known := map[string]bool{"/launcher/v1/sessions/current": true, "/launcher/v1/pvf/key": true, "/launcher/v1/cdk/characters": true, "/launcher/v1/cdk/redemptions": true, "/launcher/v1/announcements": true, "/launcher/v1/announcements/receipt": true, "/launcher/v1/assist": true}
	if !known[path] && path != "/launcher/v1/party-relay/grants" && path != "/launcher/v1/adventure/collection" && path != "/launcher/v1/adventure/operation" {
		return nil, 404, fail(404, "NOT_FOUND", "endpoint does not exist")
	}
	id, err := l.identity(ctx, r, ip)
	if err != nil {
		return nil, 0, err
	}
	switch {
	case (path == "/launcher/v1/adventure/collection" || path == "/launcher/v1/adventure/operation") && method == "POST":
		return l.adventure(ctx, path, r, ip)
	case path == "/launcher/v1/party-relay/grants" && method == "POST":
		body, err := launcherBody(r, []string{"request_id"})
		if err != nil {
			return nil, 400, err
		}
		rid, err := launcherString(body["request_id"], 128)
		if err != nil || len(rid) < 16 {
			return nil, 400, fail(400, "INVALID_REQUEST", "invalid launch request identity")
		}
		for _, c := range rid {
			if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-' || c == '_') {
				return nil, 400, fail(400, "INVALID_REQUEST", "invalid launch request identity")
			}
		}
		// Resolve again in the private adapter immediately before issuance.
		// Account and login-instance fields are never accepted from players.
		var result json.RawMessage
		err = l.engine(ctx, "relay_grant", map[string]any{"request_id": rid, "token": strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "), "remote_ip": ip}, &result)
		return result, 200, err
	case path == "/launcher/v1/assist" && method == "POST":
		body, err := launcherBody(r, []string{"action", "request_id"}, "pickup", "target", "rarities")
		if err != nil {
			return nil, 400, err
		}
		body["account_id"] = id.AccountID
		body["session_id"] = id.SessionID
		var result json.RawMessage
		err = l.engine(ctx, "assist", body, &result)
		return result, 200, err
	case path == "/launcher/v1/sessions/current" && method == "GET":
		meta, err := l.meta(ctx)
		return map[string]any{"session_id": id.SessionID, "account": id.Account, "remote_ip": id.RemoteIP, "advertise_host": id.AdvertiseHost, "issued_at": id.IssuedAt, "expires_at": id.ExpiresAt, "stage3": meta["stage3"]}, 200, err
	case path == "/launcher/v1/sessions/current" && method == "DELETE":
		err = l.engine(ctx, "revoke", map[string]any{"token": strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "), "remote_ip": ip}, nil)
		return map[string]bool{"revoked": true}, 200, err
	case path == "/launcher/v1/pvf/key" && method == "GET":
		result, err := l.pvfKey(ctx, r.URL.Query())
		return result, 200, err
	case path == "/launcher/v1/cdk/characters" && method == "GET":
		result, err := l.characters(ctx, id.AccountID)
		return result, 200, err
	case path == "/launcher/v1/cdk/redemptions" && method == "GET":
		result, err := l.redemptionHistory(ctx, id.AccountID)
		return result, 200, err
	case path == "/launcher/v1/cdk/redemptions" && method == "POST":
		result, err := l.redeem(ctx, r, id.AccountID)
		return result, 200, err
	case path == "/launcher/v1/announcements" && method == "GET":
		result, err := l.announcements(ctx, r)
		return result, 200, err
	case path == "/launcher/v1/announcements/receipt" && method == "POST":
		result, err := l.acknowledge(ctx, r, id.AccountID)
		return result, 200, err
	}
	return nil, 405, fail(405, "INVALID_REQUEST", "method not allowed")
}

type launcherQuerier interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}
