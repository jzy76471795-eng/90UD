package manager

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func launcherFixture(t *testing.T) (*launcherService, int, int) {
	t.Helper()
	s, _, a, c := directFixture(t)
	for _, q := range []string{"DELETE FROM gmkit_bans", "DELETE FROM gmkit_ban_rules", `INSERT OR REPLACE INTO gmkit_config VALUES('maintenancePolicy','{"enabled":false,"allowAccountIds":[]}')`, `INSERT OR REPLACE INTO gmkit_config VALUES('registrationPolicy','{"closed":false,"codeRequired":false,"maxPerIp":-1}')`} {
		if _, err := s.store.db.Exec(q); err != nil {
			t.Fatal(err)
		}
	}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	l := newLauncherService(s, key, "127.0.0.1")
	return l, a, c
}
func launcherRequest(t *testing.T, l *launcherService, method, path string, value any, ip string) (int, map[string]any) {
	t.Helper()
	raw, _ := json.Marshal(value)
	r := httptest.NewRequest(method, "https://127.0.0.1"+path, bytes.NewReader(raw))
	r.TLS = &tls.ConnectionState{}
	r.RemoteAddr = ip + ":4242"
	w := httptest.NewRecorder()
	l.ServeHTTP(w, r)
	var out map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatalf("response %d: %v", w.Code, err)
	}
	return w.Code, out
}
func TestLauncherPasswordCompatibilityAndBounds(t *testing.T) {
	hash, err := launcherHashWithSalt("测试password123", []byte("0123456789abcdef"))
	if err != nil {
		t.Fatal(err)
	}
	// Independent Python hashlib.scrypt vector is also checked by the integration runner.
	if !launcherVerify("测试password123", hash) || launcherVerify("bad-password", hash) {
		t.Fatal("password verification")
	}
	for _, bad := range []string{strings.Replace(hash, "$32768$", "$1048576$", 1), strings.Replace(hash, "$8$1$", "$8$999999$", 1), "scrypt-v1$32768$8$1$32$x$x"} {
		if launcherVerify("测试password123", bad) {
			t.Fatal("unbounded hash accepted")
		}
	}
	answer, err := launcherAnswer(" ＡＢＣ　Straße ")
	if err != nil || answer != "security-answer-v1:abc strasse" {
		t.Fatal(answer, err)
	}
	if _, err = launcherAnswer("a\x00b"); err == nil {
		t.Fatal("control character accepted")
	}
}

func TestLauncherPythonAtomicReceiptDigest(t *testing.T) {
	digest, err := launcherCDKDigest("中文<&>", 10, 20)
	if err != nil || digest != "28f4ce50c5db955f2aeb2b820241c4e9d84fd02a7ff2b9db80b949e55ce1841e" {
		t.Fatal("legacy Python receipt digest changed", digest, err)
	}
}
func TestLauncherTLSJSONAndPeerBoundary(t *testing.T) {
	l, _, _ := launcherFixture(t)
	r := httptest.NewRequest("GET", "http://127.0.0.1/launcher/v1/registration", nil)
	r.RemoteAddr = "192.0.2.1:12"
	w := httptest.NewRecorder()
	l.ServeHTTP(w, r)
	if w.Code != 426 {
		t.Fatal(w.Code)
	}
	for _, raw := range []string{`{"account":"a","account":"b"}`, `{"account":"a"} {}`, `{"extra":1}`, `[]`} {
		r := httptest.NewRequest("POST", "https://localhost", strings.NewReader(raw))
		if _, err := launcherBody(r, []string{"account"}); err == nil {
			t.Fatal("malformed JSON accepted")
		}
	}
	r = httptest.NewRequest("GET", "https://localhost", nil)
	r.RemoteAddr = "192.0.2.3:22"
	r.Header.Set("X-Forwarded-For", "127.0.0.1")
	ip, err := launcherRemoteIP(r)
	if err != nil || ip != "192.0.2.3" {
		t.Fatal("forwarded IP trusted")
	}
	if status, _ := launcherRequest(t, l, "GET", "/manage/v1/accounts", nil, "192.0.2.3"); status != 404 {
		t.Fatal("admin route exposed", status)
	}
}

func TestLauncherNoticeListIncludesPublishedBody(t *testing.T) {
	l, _, _ := launcherFixture(t)
	if _, err := l.manager.store.db.Exec("UPDATE gmkit_notices SET enabled=0"); err != nil {
		t.Fatal(err)
	}
	_, err := l.manager.store.db.Exec(
		"INSERT INTO gmkit_notices(title,body,date,enabled,sort_order) VALUES(?,?,?,?,?),(?,?,?,?,?)",
		"[更新] 版本公告", "第一行\n第二行", "2026-11-24", 1, 0,
		"[指南] 草稿", "未启用", "2026-11-24", 0, 0,
	)
	if err != nil {
		t.Fatal(err)
	}
	status, result := launcherRequest(t, l, "GET", "/launcher/v1/notices", nil, "127.0.0.1")
	if status != 200 {
		t.Fatalf("notice response: %d %v", status, result)
	}
	if wrapped, ok := result["data"].(map[string]any); ok {
		result = wrapped
	}
	items, ok := result["notices"].([]any)
	if !ok || len(items) != 1 {
		t.Fatalf("notice list: %v", result)
	}
	notice, ok := items[0].(map[string]any)
	if !ok || notice["title"] != "[更新] 版本公告" || notice["notice_date"] != "2026-11-24" || notice["body"] != "第一行\n第二行" {
		t.Fatalf("notice fields: %v", items[0])
	}
}
func TestLauncherConcurrentRegistrationLimitsAndCodeRollback(t *testing.T) {
	l, _, _ := launcherFixture(t)
	_, err := l.manager.store.db.Exec(`INSERT OR REPLACE INTO gmkit_config VALUES('registrationPolicy','{"closed":false,"codeRequired":true,"maxPerIp":1}');INSERT INTO gmkit_registration_codes(code,created_at) VALUES('GO-TEST-CODE',CURRENT_TIMESTAMP)`)
	if err != nil {
		t.Fatal(err)
	}
	// Stored registration codes are normalized without hyphens by the launcher.
	_, _ = l.manager.store.db.Exec("UPDATE gmkit_registration_codes SET code='GOTESTCODE' WHERE code='GO-TEST-CODE'")
	statuses := make(chan int, 2)
	var wg sync.WaitGroup
	for _, name := range []string{"go-race-one", "go-race-two"} {
		wg.Add(1)
		go func(name string) {
			defer wg.Done()
			status, _ := launcherRequest(t, l, "POST", "/launcher/v1/accounts", map[string]any{"account": name, "password": "test-password-123", "security_question": "first_school", "security_answer": "ＡＢＣ 学校", "registration_code": "go-test-code"}, "192.0.2.20")
			statuses <- status
		}(name)
	}
	wg.Wait()
	close(statuses)
	success, rejected := 0, 0
	for status := range statuses {
		if status == 201 {
			success++
		} else if status == 429 {
			rejected++
		} else {
			t.Fatal("unexpected registration status", status)
		}
	}
	if success != 1 || rejected != 1 {
		t.Fatal(success, rejected)
	}
	if number(t, l.manager.store.db, "SELECT COUNT(*) FROM accounts WHERE m_id IN ('go-race-one','go-race-two')") != 1 || number(t, l.manager.store.db, "SELECT COUNT(*) FROM gmkit_registrations WHERE remote_ip='192.0.2.20'") != 1 {
		t.Fatal("partial or duplicate registration")
	}
}
func TestLauncherSecurityRecoveryLockAndPasswordChange(t *testing.T) {
	l, _, _ := launcherFixture(t)
	body := map[string]any{"account": "go-recovery", "password": "before-password", "security_question": "first_school", "security_answer": "Test School"}
	if status, out := launcherRequest(t, l, "POST", "/launcher/v1/accounts", body, "192.0.2.30"); status != 201 {
		t.Fatal(status, out)
	}
	recovery := map[string]any{"account": "go-recovery", "new_password": "after-password", "security_question": "first_school", "security_answer": "Wrong answer"}
	for i := 0; i < 5; i++ {
		if status, out := launcherRequest(t, l, "POST", "/launcher/v1/accounts/security-recovery", recovery, "192.0.2.31"); status != 403 {
			t.Fatal(status, out)
		}
	}
	recovery["security_answer"] = "ＴＥＳＴ school"
	if status, _ := launcherRequest(t, l, "POST", "/launcher/v1/accounts/security-recovery", recovery, "192.0.2.32"); status != 403 {
		t.Fatal("locked account recovered", status)
	}
	if _, err := l.manager.store.db.Exec("UPDATE launcher_security_questions SET locked_until=? WHERE account_id=(SELECT account_id FROM accounts WHERE m_id='go-recovery')", time.Now().Unix()-1); err != nil {
		t.Fatal(err)
	}
	if status, out := launcherRequest(t, l, "POST", "/launcher/v1/accounts/security-recovery", recovery, "192.0.2.33"); status != 200 || out["recovery_code"] == nil {
		t.Fatal(status, out)
	}
	var hash string
	if err := l.manager.store.db.QueryRow("SELECT password_hash FROM accounts WHERE m_id='go-recovery'").Scan(&hash); err != nil {
		t.Fatal(err)
	}
	if !launcherVerify("after-password", hash) || launcherVerify("before-password", hash) {
		t.Fatal("password not changed")
	}
}
func TestLauncherCDKAtomicReplayOwnershipAndOverflow(t *testing.T) {
	l, a, c := launcherFixture(t)
	_, err := l.manager.store.db.Exec("INSERT INTO gmkit_cdks(code,name,points,item_id,item_name,quantity,max_uses,created_at,rewards_json) VALUES('GOCDK','Go test',0,0,'',0,1,CURRENT_TIMESTAMP,?)", `[{"kind":"points","amount":17},{"kind":"gold","amount":23}]`)
	if err != nil {
		t.Fatal(err)
	}
	body := map[string]any{"code": "GOCDK", "character_id": c, "request_id": strings.Repeat("a", 32)}
	redeem := func(aid int, b map[string]any) (any, error) {
		raw, _ := json.Marshal(b)
		r := httptest.NewRequest("POST", "https://localhost", bytes.NewReader(raw))
		return l.redeem(context.Background(), r, aid)
	}
	before := number(t, l.manager.store.db, "SELECT COUNT(*) FROM gmkit_mails")
	wrong := a + 100000
	if _, err = redeem(wrong, body); err == nil {
		t.Fatal("foreign character accepted")
	}
	if number(t, l.manager.store.db, "SELECT used FROM gmkit_cdks WHERE code='GOCDK'") != 0 {
		t.Fatal("ownership failure mutated code")
	}
	var wg sync.WaitGroup
	errs := make(chan error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _, e := redeem(a, body); errs <- e }()
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		if e != nil {
			t.Fatal(e)
		}
	}
	if number(t, l.manager.store.db, "SELECT cera FROM accounts WHERE account_id=?", a) != 17 || number(t, l.manager.store.db, "SELECT COUNT(*) FROM gmkit_mails") != before+1 || number(t, l.manager.store.db, "SELECT used FROM gmkit_cdks WHERE code='GOCDK'") != 1 {
		t.Fatal("duplicate award")
	}
	if number(t, l.manager.store.db, "SELECT gold FROM character_wallets WHERE character_id=?", c) != 0 {
		t.Fatal("gold credited before mail claim")
	}
	body["request_id"] = strings.Repeat("b", 32)
	if _, err = redeem(a, body); err == nil {
		t.Fatal("one-per-account restriction bypassed")
	}
	body["request_id"] = strings.Repeat("a", 32)
	body["code"] = "OTHER"
	if _, err = redeem(a, body); err == nil {
		t.Fatal("receipt content conflict accepted")
	}
	_, err = l.manager.store.db.Exec("UPDATE accounts SET cera=99999999 WHERE account_id=?; INSERT INTO gmkit_cdks(code,name,points,item_id,item_name,quantity,max_uses,created_at) VALUES('OVERFLOW','Overflow',1,0,'',0,1,CURRENT_TIMESTAMP)", a)
	if err != nil {
		t.Fatal(err)
	}
	body["request_id"] = strings.Repeat("c", 32)
	body["code"] = "OVERFLOW"
	if _, err = redeem(a, body); err == nil {
		t.Fatal("overflow accepted")
	}
	if number(t, l.manager.store.db, "SELECT used FROM gmkit_cdks WHERE code='OVERFLOW'") != 0 || number(t, l.manager.store.db, "SELECT COUNT(*) FROM gmkit_atomic_receipts WHERE id=?", "launcher-cdk-"+strings.Repeat("c", 32)) != 0 {
		t.Fatal("partial overflow transaction")
	}
}
func TestLauncherPatchSignatureAndPublicPVFBoundary(t *testing.T) {
	l, _, _ := launcherFixture(t)
	ctx := context.Background()
	document := map[string]any{"revision": 3, "items": []any{}}
	signed, err := l.sign("patches", document)
	if err != nil {
		t.Fatal(err)
	}
	payload, _ := base64.StdEncoding.DecodeString(signed["payload"])
	signature, _ := base64.StdEncoding.DecodeString(signed["signature"])
	sum := sha256.Sum256(payload)
	message := append([]byte("90US-UPDATE-V1\x00patches\x00"), sum[:]...)
	digest := sha256.Sum256(message)
	if err = rsa.VerifyPSS(&l.key.PublicKey, crypto.SHA256, digest[:], signature, &rsa.PSSOptions{SaltLength: 32}); err != nil {
		t.Fatal(err)
	}
	wrong := sha256.Sum256(append([]byte("90US-UPDATE-V1\x00resources\x00"), sum[:]...))
	if rsa.VerifyPSS(&l.key.PublicKey, crypto.SHA256, wrong[:], signature, nil) == nil {
		t.Fatal("signature domain confusion")
	}
	latest, err := l.pvfLatest(ctx)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(latest)
	if bytes.Contains(raw, []byte("key_set")) {
		t.Fatal("public PVF key leak")
	}
	data := []byte("test-patch-payload")
	sha := sha256.Sum256(data)
	hash := hex.EncodeToString(sha[:])
	_, err = l.manager.store.db.Exec("INSERT OR REPLACE INTO gmkit_patch_blobs VALUES(?,?,?)", hash, data, len(data))
	if err != nil {
		t.Fatal(err)
	}
	_, err = l.manager.store.db.Exec("INSERT OR REPLACE INTO gmkit_patch_current VALUES(1,100,?)", jsonText([]map[string]any{{"sha256": hash}}))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = l.patchBlob(ctx, hash); err != nil {
		t.Fatal(err)
	}
	_, _ = l.manager.store.db.Exec("UPDATE gmkit_patch_blobs SET data=? WHERE sha256=?", []byte("corrupt"), hash)
	if _, err = l.patchBlob(ctx, hash); err == nil {
		t.Fatal("corrupt patch served")
	}
}
func TestLauncherLegacyScheduleCutoverGate(t *testing.T) {
	l, _, _ := launcherFixture(t)
	if err := l.manager.validateLauncherMigration(context.Background()); err != nil {
		t.Fatal(err)
	}
	_, _ = l.manager.store.db.Exec(`INSERT OR REPLACE INTO gmkit_config VALUES('welfare','{"enabled":true}')`)
	if err := l.manager.validateLauncherMigration(context.Background()); err == nil {
		t.Fatal("enabled legacy worker silently discarded")
	}
}

func TestLauncherAdminPatchPublicationIsAtomicAndIdempotent(t *testing.T) {
	l, _, _ := launcherFixture(t)
	s := l.manager
	if _, err := s.store.db.Exec("DELETE FROM gmkit_patch_current"); err != nil {
		t.Fatal(err)
	}
	h := httptest.NewServer(s.Handler())
	defer h.Close()
	data := make([]byte, 256)
	copy(data, "MZ")
	binary.LittleEndian.PutUint32(data[0x3c:], 0x80)
	copy(data[0x80:], "PE\x00\x00")
	binary.LittleEndian.PutUint16(data[0x84:], 0x14c)
	binary.LittleEndian.PutUint16(data[0x96:], 0x2000)
	digest := sha256.Sum256(data)
	hash := hex.EncodeToString(digest[:])
	upload := map[string]any{"sha256": hash, "content_base64": base64.StdEncoding.EncodeToString(data)}
	for i := 0; i < 2; i++ {
		if status, body := call(t, h, "POST", "/manage/v1/launcher/patches/upload", "patch-upload-once", upload); status != 200 {
			t.Fatal(status, body)
		}
	}
	publish := map[string]any{"expected_revision": 0, "items": []map[string]any{{"file_name": "test_patch.dll", "sha256": hash, "bytes": 256, "enabled": true, "on_mismatch": "block"}}}
	for i := 0; i < 2; i++ {
		if status, body := call(t, h, "POST", "/manage/v1/launcher/patches/publish", "patch-publish-once", publish); status != 200 {
			t.Fatal(status, body)
		}
	}
	if number(t, s.store.db, "SELECT revision FROM gmkit_patch_current WHERE id=1") != 1 {
		t.Fatal("publication repeated")
	}
	if status, _ := call(t, h, "POST", "/manage/v1/launcher/patches/publish", "patch-stale", publish); status != 409 {
		t.Fatal("stale revision accepted", status)
	}
	if _, err := l.patchBlob(context.Background(), hash); err != nil {
		t.Fatal(err)
	}
	publish["expected_revision"] = 1
	publish["items"] = []map[string]any{{"file_name": "other.dll", "sha256": hash, "bytes": 256, "enabled": true, "on_mismatch": "block"}}
	if status, _ := call(t, h, "POST", "/manage/v1/launcher/patches/publish", "patch-removal", publish); status != 409 {
		t.Fatal("published patch removed", status)
	}
}
