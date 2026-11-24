package player

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto"
	"crypto/hmac"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"newadmin/internal/launcherbundle"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fixtureCert(t *testing.T) (*rsa.PrivateKey, string, tls.Certificate) {
	t.Helper()
	key, e := rsa.GenerateKey(rand.Reader, 2048)
	if e != nil {
		t.Fatal(e)
	}
	c := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "local-test"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}, DNSNames: []string{"localhost"}, KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, IsCA: true, BasicConstraintsValid: true}
	der, e := x509.CreateCertificate(rand.Reader, c, c, &key.PublicKey, key)
	if e != nil {
		t.Fatal(e)
	}
	cert := string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))
	pair, e := tls.X509KeyPair([]byte(cert), pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)}))
	if e != nil {
		t.Fatal(e)
	}
	return key, cert, pair
}
func fixtureStage() Stage {
	return Stage{ExeSHA256: FrozenExeSHA256, PvfSHA256: strings.Repeat("a", 64), PvfProfile: "keyset96", ChannelHost: "127.0.0.1", ChannelPort: 7001, LaunchMode: 9, Account: "1", Token: strings.Repeat("b", 64), Tail: []int{0, 0, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}, NativeTunnelVersion: 2, GamePorts: []int{7002}, Patches: PatchManifest{Revision: 0, Items: []Patch{}}}
}
func signedFixture(t *testing.T, key *rsa.PrivateKey, kind string, payload any) []byte {
	t.Helper()
	raw, e := json.Marshal(payload)
	if e != nil {
		t.Fatal(e)
	}
	sum := sha256.Sum256(raw)
	message := append([]byte("90US-UPDATE-V1\x00"+kind+"\x00"), sum[:]...)
	digest := sha256.Sum256(message)
	sig, e := rsa.SignPSS(rand.Reader, key, crypto.SHA256, digest[:], &rsa.PSSOptions{SaltLength: rsa.PSSSaltLengthEqualsHash})
	if e != nil {
		t.Fatal(e)
	}
	env, _ := json.Marshal(map[string]string{"payload": base64.StdEncoding.EncodeToString(raw), "signature": base64.StdEncoding.EncodeToString(sig)})
	return env
}
func fixtureAPI(t *testing.T, handler http.Handler) (*API, *rsa.PrivateKey) {
	t.Helper()
	key, cert, pair := fixtureCert(t)
	s := httptest.NewUnstartedServer(handler)
	s.TLS = &tls.Config{Certificates: []tls.Certificate{pair}}
	s.StartTLS()
	t.Cleanup(s.Close)
	api, e := NewAPI(Config{2, "local-fixture", "本机测试", s.URL, cert})
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(api.Close)
	return api, key
}
func TestStageGuards(t *testing.T) {
	if e := fixtureStage().Validate(); e != nil {
		t.Fatal(e)
	}
	cases := map[string]func(*Stage){"exe": func(s *Stage) { s.ExeSHA256 = strings.Repeat("0", 64) }, "pvf": func(s *Stage) { s.PvfSHA256 = "bad" }, "profile": func(s *Stage) { s.PvfProfile = "untrusted" }, "host": func(s *Stage) { s.ChannelHost = "example.com" }, "multicast": func(s *Stage) { s.ChannelHost = "224.0.0.1" }, "port": func(s *Stage) { s.ChannelPort = 80 }, "launch mode": func(s *Stage) { s.LaunchMode = 1 }, "account": func(s *Stage) { s.Account = "1?injected" }, "token": func(s *Stage) { s.Token = "secret?invalid" }, "tail": func(s *Stage) { s.Tail = []int{1} }, "tunnel": func(s *Stage) { s.NativeTunnelVersion = 1 }, "duplicate ports": func(s *Stage) { s.GamePorts = []int{7002, 7002} }, "channel port in games": func(s *Stage) { s.GamePorts = []int{7001} }, "patch name": func(s *Stage) {
		s.Patches = PatchManifest{Revision: 1, Items: []Patch{{"../a.dll", strings.Repeat("a", 64), 256, true, "block"}}}
	}}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			s := fixtureStage()
			mutate(&s)
			if s.Validate() == nil {
				t.Fatal("accepted invalid stage")
			}
		})
	}
}
func TestStrictConfigAndPaths(t *testing.T) {
	for _, raw := range []string{`{"a":1,"a":2}`, `{"x":{"a":1,"a":2}}`, `[] {}`, `{"a":[{"b":1,"b":2}]}`} {
		if uniqueJSON([]byte(raw)) == nil {
			t.Fatal("accepted ambiguous JSON")
		}
	}
	for _, p := range []string{"../a", "a/../b", "C:/a", "/a", "a\\b", "a/CON.txt", "x.", "x ", "a//b", "a/xx:stream", "x\n.dll"} {
		if Relative(p) == nil {
			t.Fatalf("accepted path %q", p)
		}
	}
	for _, u := range []string{"http://127.0.0.1", "https://user:pass@example.com", "https://example.com/#secret", "https://example.com\\evil", "https://example.com/path"} {
		if _, e := HTTPS(u, true); e == nil {
			t.Fatal("accepted unsafe endpoint")
		}
	}
}
func TestGMEmbeddedPublicConfiguration(t *testing.T) {
	_, cert, _ := fixtureCert(t)
	c := Config{2, "fixture-1", "Go玩家登录器", "https://127.0.0.1:443", cert}
	path := filepath.Join(t.TempDir(), "player.exe")
	f, e := os.Create(path)
	if e != nil {
		t.Fatal(e)
	}
	f.Write([]byte("Go template fixture"))
	raw, _ := json.Marshal(c)
	if e = launcherbundle.Append(f, raw, launcherbundle.ConfigMagic); e != nil {
		t.Fatal(e)
	}
	f.Close()
	actual, e := LoadConfig(path)
	if e != nil || actual.ServerID != c.ServerID || actual.CertificatePEM != c.CertificatePEM {
		t.Fatal("GM public config round trip", e)
	}
	f, _ = os.OpenFile(path, os.O_WRONLY, 0)
	f.WriteAt([]byte{9}, int64(len("Go template fixture")+2))
	f.Close()
	if _, e = LoadConfig(path); e == nil {
		t.Fatal("accepted changed embedded config")
	}
}
func TestSignedUpdateRejectsTamperingAndWrongPurpose(t *testing.T) {
	key, cert, _ := fixtureCert(t)
	raw := signedFixture(t, key, "patches", map[string]any{"revision": 0, "items": []Patch{}})
	var m PatchManifest
	if e := VerifySigned(raw, cert, "patches", &m); e != nil {
		t.Fatal(e)
	}
	if VerifySigned(raw, cert, "resources", &m) == nil {
		t.Fatal("cross-purpose signature accepted")
	}
	var env map[string]string
	json.Unmarshal(raw, &env)
	env["payload"] = base64.StdEncoding.EncodeToString([]byte(`{"revision":9,"items":[]}`))
	bad, _ := json.Marshal(env)
	if VerifySigned(bad, cert, "patches", &m) == nil {
		t.Fatal("modified payload accepted")
	}
	if VerifySigned([]byte(`{"payload":"","signature":""}`), cert, "patches", &m) == nil {
		t.Fatal("unsigned payload accepted")
	}
}
func TestHTTPSLoginAndCleanupContract(t *testing.T) {
	stage := fixtureStage()
	var gotDevice bool
	var logout bool
	api, _ := fixtureAPI(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/launcher/v1/meta":
			json.NewEncoder(w).Encode(map[string]any{"api_version": 1, "transport": "http-direct-tcp", "server_ready": true, "stage3": stage})
		case "/launcher/v1/sessions":
			var body map[string]string
			json.NewDecoder(r.Body).Decode(&body)
			gotDevice = body["device_id"] == "fixture-device"
			json.NewEncoder(w).Encode(Session{"fixture-token", "Bearer", strings.Repeat("c", 32), stage})
		case "/launcher/v1/sessions/current":
			logout = r.Header.Get("Authorization") == "Bearer fixture-token"
			w.Write([]byte(`{}`))
		default:
			http.NotFound(w, r)
		}
	}))
	ctx := context.Background()
	m, e := api.Meta(ctx)
	if e != nil {
		t.Fatal(e)
	}
	s, e := api.Login(ctx, "test-account", "test-password", "fixture-device", m)
	if e != nil || !gotDevice {
		t.Fatal("login contract", e)
	}
	if e = api.Logout(ctx, s); e != nil || !logout {
		t.Fatal("logout contract", e)
	}
	changed := m
	changed.PvfSHA256 = strings.Repeat("d", 64)
	logout = false
	if _, e = api.Login(ctx, "test-account", "test-password", "fixture-device", changed); e == nil || !logout {
		t.Fatal("stale release did not revoke acquired session")
	}
}
func TestHTTPSRejectsUntrustedAndRedirects(t *testing.T) {
	api, _ := fixtureAPI(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "https://example.invalid/steal", http.StatusFound)
	}))
	if e := api.Request(context.Background(), "GET", "/launcher/v1/meta", "fixture-token", nil, new(any)); e == nil {
		t.Fatal("followed auth redirect")
	}
	_, other, _ := fixtureCert(t)
	bad := api.Config
	bad.CertificatePEM = other
	client, e := NewAPI(bad)
	if e != nil {
		t.Fatal(e)
	}
	defer client.Close()
	if client.Request(context.Background(), "GET", "/launcher/v1/meta", "", nil, new(any)) == nil {
		t.Fatal("accepted wrong TLS root")
	}
}
func TestNativeChallengeProofAndHalfClose(t *testing.T) {
	left, right := net.Pipe()
	defer left.Close()
	defer right.Close()
	c := credential{strings.Repeat("a", 32), "fixture-token"}
	result := make(chan error, 1)
	go func() {
		prefix := make([]byte, 24)
		if _, e := io.ReadFull(right, prefix); e != nil {
			result <- e
			return
		}
		if string(prefix[:8]) != "PY90NT2\n" {
			result <- errors.New("prefix mismatch")
			return
		}
		nonce := bytes.Repeat([]byte{7}, 32)
		for _, b := range nonce {
			if _, e := right.Write([]byte{b}); e != nil {
				result <- e
				return
			}
		}
		proof := make([]byte, 32)
		_, e := io.ReadFull(right, proof)
		key := sha256.Sum256([]byte(c.Token))
		mac := hmac.New(sha256.New, key[:])
		mac.Write(prefix)
		mac.Write(nonce)
		if e == nil && !hmac.Equal(proof, mac.Sum(nil)) {
			e = errors.New("proof mismatch")
		}
		result <- e
	}()
	if e := NativeProof(left, c); e != nil {
		t.Fatal(e)
	}
	if e := <-result; e != nil {
		t.Fatal(e)
	}
}
func TestUpdateTransactionRollsBackAndPreservesExternalChanges(t *testing.T) {
	for _, mode := range []string{"validation failure", "mid-install failure", "success"} {
		t.Run(mode, func(t *testing.T) {
			base := t.TempDir()
			root := filepath.Join(base, "client")
			cache := filepath.Join(base, "cache")
			os.MkdirAll(root, 0700)
			old := filepath.Join(root, "a.bin")
			os.WriteFile(old, []byte("old"), 0600)
			next := filepath.Join(base, "next")
			os.WriteFile(next, []byte("new"), 0600)
			tx := Transaction{Root: root, Cache: cache}
			if mode == "mid-install failure" {
				tx.AfterReplace = func(int) error { return errors.New("forced failure") }
			}
			validate := func() error {
				if mode == "validation failure" {
					return errors.New("bad installed client")
				}
				return nil
			}
			e := tx.Apply(map[string]string{"a.bin": next, "nested/b.bin": next}, validate)
			if mode == "success" {
				if e != nil {
					t.Fatal(e)
				}
				b, _ := os.ReadFile(old)
				if string(b) != "new" {
					t.Fatal("did not commit")
				}
			} else {
				if e == nil {
					t.Fatal("expected failure")
				}
				b, _ := os.ReadFile(old)
				if string(b) != "old" {
					t.Fatal("old file not restored")
				}
				if _, e = os.Stat(filepath.Join(root, "nested", "b.bin")); !os.IsNotExist(e) {
					t.Fatal("new file not removed on rollback")
				}
			}
			if e = tx.Recover(); e != nil {
				t.Fatal(e)
			}
		})
	}
	base := t.TempDir()
	root := filepath.Join(base, "client")
	os.MkdirAll(root, 0700)
	cache := filepath.Join(base, "cache")
	os.MkdirAll(cache, 0700)
	job := filepath.Join(cache, "transaction-"+ID())
	os.MkdirAll(filepath.Join(job, "previous"), 0700)
	os.WriteFile(filepath.Join(job, "previous", "a"), []byte("old"), 0600)
	before, _ := FileHash(filepath.Join(job, "previous", "a"))
	os.WriteFile(filepath.Join(root, "a"), []byte("external"), 0600)
	j := transactionJournal{1, root, job, []TransactionEntry{{"a", before, shaText("new")}}}
	raw, _ := json.Marshal(j)
	tx := Transaction{Root: root, Cache: cache}
	os.WriteFile(tx.pending(), raw, 0600)
	if tx.Recover() == nil {
		t.Fatal("overwrote external change")
	}
	b, _ := os.ReadFile(filepath.Join(root, "a"))
	if string(b) != "external" {
		t.Fatal("external change lost")
	}
}
func TestResourceArchiveRejectsUnsignedBeforeChanges(t *testing.T) {
	_, cert, _ := fixtureCert(t)
	base := t.TempDir()
	root := filepath.Join(base, "client")
	os.MkdirAll(root, 0700)
	original := filepath.Join(root, "Script.pvf")
	os.WriteFile(original, []byte("original"), 0600)
	path := filepath.Join(base, "bad.zip")
	f, _ := os.Create(path)
	z := zip.NewWriter(f)
	e, _ := z.Create(".90us-update.json")
	e.Write([]byte(`{"payload":"","signature":""}`))
	entry, _ := z.Create("Script.pvf")
	entry.Write([]byte("changed"))
	z.Close()
	f.Close()
	a := &API{Config: Config{CertificatePEM: cert}}
	if a.InstallArchive(context.Background(), path, filepath.Join(base, "job"), root, "launcher.exe", ResourceRelease{Version: "fixture"}, fixtureStage(), func(string) {}) == nil {
		t.Fatal("installed unsigned resource")
	}
	b, _ := os.ReadFile(original)
	if string(b) != "original" {
		t.Fatal("changed original before trust checks")
	}
}
func TestPasswordsAreEncryptedAndScoped(t *testing.T) {
	_, cert, _ := fixtureCert(t)
	c := Config{2, "fixture-" + ID(), "test", "https://127.0.0.1:443", cert}
	rows := []SavedLogin{{0, "test-account", "test-password-secret"}, {1, "other-account", "other-secret"}}
	if e := c.SaveLogin(rows); e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { _ = c.SaveLogin(nil) })
	raw, e := os.ReadFile(filepath.Join(c.DataDirectory(), "remembered-login.bin"))
	if e != nil {
		t.Fatal(e)
	}
	if bytes.Contains(raw, []byte("test-password-secret")) || bytes.Contains(raw, []byte("test-account")) {
		t.Fatal("plaintext credentials on disk")
	}
	actual, e := c.ReadLogin()
	if e != nil || len(actual) != 2 || actual[0].Password != rows[0].Password {
		t.Fatal("DPAPI round trip", e)
	}
	entropy := sha256.Sum256([]byte("different-server"))
	if _, e = crypt(raw, entropy[:], false); e == nil {
		t.Fatal("accepted different credential scope")
	}
}
func TestOptionalExistingClientReadOnly(t *testing.T) {
	root := os.Getenv("GO_LAUNCHER_LOCAL_CLIENT")
	if root == "" {
		t.Skip("only with explicitly selected local client")
	}
	if !strings.EqualFold(filepath.Clean(root), `E:\90USADFO\客户端`) {
		t.Fatal("unexpected client directory")
	}
	before, e := FileHash(filepath.Join(root, "Script.pvf"))
	if e != nil {
		t.Fatal(e)
	}
	s := fixtureStage()
	s.PvfSHA256 = before
	components, e := os.ReadDir(filepath.Join(root, "us_extend_dll"))
	if e != nil {
		t.Fatal(e)
	}
	for _, p := range components {
		if strings.EqualFold(filepath.Ext(p.Name()), ".dll") {
			h, e := FileHash(filepath.Join(root, "us_extend_dll", p.Name()))
			if e != nil {
				t.Fatal(e)
			}
			info, _ := os.Stat(filepath.Join(root, "us_extend_dll", p.Name()))
			s.Patches.Revision = 1
			s.Patches.Items = append(s.Patches.Items, Patch{p.Name(), h, info.Size(), true, "block"})
		}
	}
	p, e := ValidateClient(root, s)
	if e != nil {
		t.Fatal(e)
	}
	after, _ := FileHash(filepath.Join(root, "Script.pvf"))
	if before != after || len(p.DLLs) < 15 {
		t.Fatal("client validation scope mismatch")
	}
	if report := os.Getenv("GO_LAUNCHER_CLIENT_REPORT"); report != "" {
		raw, _ := json.MarshalIndent(map[string]any{"scope": "explicit local client; synthetic stage from local hashes; not current server compatibility", "pvf_hash_unchanged": true, "validated_dll_count": len(p.DLLs), "game_started": false, "server_contacted": false}, "", "  ")
		if e = os.WriteFile(report, raw, 0600); e != nil {
			t.Fatal(e)
		}
	}
}
func TestRequestErrorsDoNotExposeBodies(t *testing.T) {
	api, _ := fixtureAPI(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(403)
		w.Write([]byte(`{"error":{"code":"AUTH_FAILED","message":"DO-NOT-LEAK-SECRET"}}`))
	}))
	e := api.Request(context.Background(), "POST", "/launcher/v1/sessions", "", nil, new(any))
	if e == nil || strings.Contains(e.Error(), "DO-NOT-LEAK") {
		t.Fatal("server body leaked")
	}
	var status *HTTPError
	if !errors.As(e, &status) || status.Status != 403 {
		t.Fatal(fmt.Sprintf("missing typed status: %v", e))
	}
}
