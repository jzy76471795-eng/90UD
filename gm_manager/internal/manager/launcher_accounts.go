package manager

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"golang.org/x/crypto/scrypt"
	"golang.org/x/text/cases"
	"golang.org/x/text/unicode/norm"
)

func launcherAccount(v any) (string, error) {
	s, err := launcherString(v, 256)
	s = strings.TrimSpace(s)
	if err != nil || utf8.RuneCountInString(s) < 1 || utf8.RuneCountInString(s) > 64 || strings.ContainsAny(s, "\x00\r\n?") {
		return "", fail(400, "INVALID_REQUEST", "invalid account")
	}
	return s, nil
}
func launcherPassword(v any) (string, error) {
	s, err := launcherString(v, 128)
	if err != nil || utf8.RuneCountInString(s) < 8 || strings.ContainsAny(s, "\x00\r\n") {
		return "", fail(400, "INVALID_REQUEST", "invalid password")
	}
	return s, nil
}
func launcherAnswer(v any) (string, error) {
	s, err := launcherString(v, 256)
	if err != nil {
		return "", err
	}
	for _, r := range s {
		if unicode.Is(unicode.C, r) {
			return "", fail(400, "SECURITY_REQUIRED", "invalid security answer")
		}
	}
	s = strings.Join(strings.Fields(cases.Fold().String(norm.NFKC.String(s))), " ")
	if n := utf8.RuneCountInString(s); n < 2 || n > 100 {
		return "", fail(400, "SECURITY_REQUIRED", "invalid security answer")
	}
	return "security-answer-v1:" + s, nil
}
func launcherQuestion(v any) (string, error) {
	s, _ := v.(string)
	switch s {
	case "childhood_nickname", "first_school", "memorable_city", "childhood_book", "favorite_teacher":
		return s, nil
	}
	return "", fail(400, "SECURITY_REQUIRED", "security question required")
}
func launcherHashWithSalt(password string, salt []byte) (string, error) {
	digest, err := scrypt.Key([]byte(password), salt, 32768, 8, 1, 32)
	if err != nil {
		return "", err
	}
	return "scrypt-v1$32768$8$1$32$" + base64.RawURLEncoding.EncodeToString(salt) + "$" + base64.RawURLEncoding.EncodeToString(digest), nil
}
func launcherHash(password string) (string, error) {
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		return "", err
	}
	return launcherHashWithSalt(password, salt)
}
func launcherVerify(password, encoded string) bool {
	parts := strings.Split(strings.TrimSpace(encoded), "$")
	if len(parts) != 7 || parts[0] != "scrypt-v1" || len(encoded) > 2048 {
		return false
	}
	nums := [4]int{}
	for i := range nums {
		n, err := strconv.Atoi(parts[i+1])
		if err != nil {
			return false
		}
		nums[i] = n
	}
	n, r, p, dk := nums[0], nums[1], nums[2], nums[3]
	// Match the Python 64 MiB memory ceiling; also bound CPU and intermediate arithmetic.
	if n < 16384 || n > 1048576 || n&(n-1) != 0 || r < 1 || r > 32 || p < 1 || p > 16 || dk < 16 || dk > 64 || int64(128)*int64(r)*int64(n+p+2) >= 64<<20 || int64(n)*int64(r)*int64(p) > 1048576 {
		return false
	}
	salt, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[5], "="))
	if err != nil || len(salt) < 16 || len(salt) > 256 {
		return false
	}
	expected, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[6], "="))
	if err != nil || len(expected) != dk {
		return false
	}
	actual, err := scrypt.Key([]byte(password), salt, n, r, p, dk)
	return err == nil && subtle.ConstantTimeCompare(actual, expected) == 1
}

// Generated once at service initialization, never once per unauthenticated request.
var launcherDummyHash, _ = launcherHashWithSalt("usdof-dummy-login-password", []byte("usdof-login-dummy"))

func launcherRecovery() (string, string, error) {
	raw := make([]byte, 20)
	if _, err := rand.Read(raw); err != nil {
		return "", "", err
	}
	value := strings.ToUpper(hex.EncodeToString(raw))
	digest := sha256.Sum256([]byte(value))
	pieces := []string{}
	for i := 0; i < len(value); i += 8 {
		pieces = append(pieces, value[i:i+8])
	}
	return strings.Join(pieces, "-"), hex.EncodeToString(digest[:]), nil
}

type launcherRegistrationPolicy struct {
	Closed, CodeRequired bool
	MaxPerIP             int64
}

func (l *launcherService) registrationPolicy(ctx context.Context, q launcherQuerier) (launcherRegistrationPolicy, error) {
	var result launcherRegistrationPolicy
	result.MaxPerIP = -1
	doc, err := getGameConfig(ctx, q, "registrationPolicy")
	if err != nil {
		return result, err
	}
	if len(doc) == 0 {
		doc, err = getGameConfig(ctx, q, "server")
		if err != nil {
			return result, err
		}
		result.Closed = doc["registration"] != true
		v, _ := doc["maxPerIp"].(float64)
		if v != 0 {
			result.MaxPerIP = int64(v)
		}
		return result, nil
	}
	var ok bool
	result.Closed, ok = doc["closed"].(bool)
	if !ok {
		return result, fmt.Errorf("invalid registration policy")
	}
	result.CodeRequired, ok = doc["codeRequired"].(bool)
	if !ok {
		return result, fmt.Errorf("invalid registration policy")
	}
	v, ok := doc["maxPerIp"].(float64)
	if !ok || v < -1 || v != float64(int64(v)) {
		return result, fmt.Errorf("invalid registration limit")
	}
	result.MaxPerIP = int64(v)
	return result, nil
}
func launcherBanActive(expiry sql.NullString) bool {
	if !expiry.Valid || expiry.String == "" {
		return true
	}
	for _, format := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02 15:04:05.999999999"} {
		if t, err := time.ParseInLocation(format, expiry.String, chinaZone()); err == nil {
			return t.After(time.Now())
		}
	}
	return true // Invalid stored expiry must not lift a ban.
}
func (l *launcherService) policy(ctx context.Context, q launcherQuerier, aid int, ip, device string, maintenance bool) error {
	rows, err := q.QueryContext(ctx, `SELECT expires_at FROM gmkit_ban_rules WHERE (revoked_at IS NULL OR revoked_at='') AND ((kind='account' AND value=?) OR (kind='ip' AND value=? AND value<>'') OR (kind='machine' AND value=? AND value<>'')) UNION ALL SELECT expires_at FROM gmkit_bans WHERE account_id=?`, strconv.Itoa(aid), ip, device, aid)
	if err != nil {
		return err
	}
	banned := false
	for rows.Next() {
		var expiry sql.NullString
		if err = rows.Scan(&expiry); err != nil {
			rows.Close()
			return err
		}
		if launcherBanActive(expiry) {
			banned = true
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	if banned {
		return fail(403, "ACCOUNT_BANNED", "account or source is banned")
	}
	if !maintenance {
		return nil
	}
	doc, err := getGameConfig(ctx, q, "maintenancePolicy")
	if err != nil {
		return err
	}
	if len(doc) == 0 {
		legacy, e := getGameConfig(ctx, q, "server")
		if e != nil {
			return e
		}
		doc = map[string]any{"enabled": legacy["maintenance"]}
	}
	enabled, ok := doc["enabled"].(bool)
	if !ok {
		return fmt.Errorf("invalid maintenance policy")
	}
	if !enabled {
		return nil
	}
	allowed, _ := doc["allowAccountIds"].([]any)
	for _, a := range allowed {
		if n, ok := a.(float64); ok && n == float64(aid) && aid > 0 {
			return nil
		}
	}
	return fail(503, "MAINTENANCE", "server is in maintenance")
}

func (l *launcherService) register(ctx context.Context, r *http.Request, ip string) (any, error) {
	body, err := launcherBody(r, []string{"account", "password", "security_question", "security_answer"}, "registration_code")
	if err != nil {
		return nil, err
	}
	account, err := launcherAccount(body["account"])
	if err != nil {
		return nil, err
	}
	password, err := launcherPassword(body["password"])
	if err != nil {
		return nil, err
	}
	question, err := launcherQuestion(body["security_question"])
	if err != nil {
		return nil, err
	}
	answer, err := launcherAnswer(body["security_answer"])
	if err != nil {
		return nil, err
	}
	code := ""
	if v, ok := body["registration_code"]; ok {
		code, err = launcherString(v, 256)
		if err != nil {
			return nil, err
		}
		code = strings.ToUpper(strings.Map(func(r rune) rune {
			if unicode.IsSpace(r) || r == '-' {
				return -1
			}
			return r
		}, code))
		if len(code) > 100 {
			return nil, fail(400, "INVALID_REQUEST", "invalid registration code")
		}
	}
	if err = l.limited(ip, "register", 10*time.Minute, 12); err != nil {
		return nil, err
	}
	if err = l.acquire(); err != nil {
		return nil, err
	}
	defer l.release()
	encoded, err := launcherHash(password)
	if err != nil {
		return nil, err
	}
	answerHash, err := launcherHash(answer)
	if err != nil {
		return nil, err
	}
	recovery, recoveryHash, err := launcherRecovery()
	if err != nil {
		return nil, err
	}
	err = l.manager.store.transaction(ctx, func(tx *sql.Tx) error {
		policy, e := l.registrationPolicy(ctx, tx)
		if e != nil {
			return e
		}
		if policy.Closed {
			return fail(403, "REGISTRATION_CLOSED", "registration closed")
		}
		if e = l.policy(ctx, tx, -1, ip, "", false); e != nil {
			return e
		}
		var count int64
		if e = tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM gmkit_registrations WHERE remote_ip=?", ip).Scan(&count); e != nil {
			return e
		}
		if policy.MaxPerIP >= 0 && count >= policy.MaxPerIP {
			return fail(429, "REGISTRATION_LIMIT", "registration limit")
		}
		if policy.CodeRequired || code != "" {
			var state string
			e = tx.QueryRowContext(ctx, "SELECT state FROM gmkit_registration_codes WHERE code=?", code).Scan(&state)
			if e == sql.ErrNoRows || e == nil && state != "unused" {
				return fail(403, "REGISTRATION_CODE_REQUIRED", "unused registration code required")
			}
			if e != nil {
				return e
			}
		}
		var exists int
		if e = tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM accounts WHERE m_id=?", account).Scan(&exists); e != nil {
			return e
		}
		if exists != 0 {
			return fail(409, "ACCOUNT_EXISTS", "account exists")
		}
		result, e := tx.ExecContext(ctx, "INSERT INTO accounts(m_id,password_hash) VALUES(?,?)", account, encoded)
		if e != nil {
			return e
		}
		aid, e := result.LastInsertId()
		if e != nil {
			return e
		}
		if _, e = tx.ExecContext(ctx, "INSERT INTO launcher_account_recovery(account_id,code_hash,updated_at) VALUES(?,?,?)", aid, recoveryHash, gameStamp()); e != nil {
			return e
		}
		if _, e = tx.ExecContext(ctx, "INSERT INTO launcher_security_questions(account_id,question_id,answer_hash,updated_at) VALUES(?,?,?,?)", aid, question, answerHash, gameStamp()); e != nil {
			return e
		}
		if _, e = tx.ExecContext(ctx, "INSERT INTO gmkit_registrations(remote_ip,account_id,created_at) VALUES(?,?,?)", ip, aid, gameStamp()); e != nil {
			return e
		}
		if code != "" {
			changed, e := tx.ExecContext(ctx, "UPDATE gmkit_registration_codes SET state='used',account_id=?,used_at=? WHERE code=? AND state='unused'", aid, gameStamp(), code)
			if e != nil {
				return e
			}
			n, _ := changed.RowsAffected()
			if n != 1 {
				return fail(409, "REGISTRATION_CODE_REQUIRED", "registration code used")
			}
		}
		// The new manager owns registration rewards. Legacy enabled rules must be
		// explicitly migrated before this listener can start; never award twice.
		return nil
	})
	if err != nil {
		return nil, err
	}
	return map[string]any{"registered": true, "account": account, "recovery_code": recovery}, nil
}

func (l *launcherService) recoverSecurity(ctx context.Context, r *http.Request, ip string) (any, error) {
	body, err := launcherBody(r, []string{"account", "security_question", "security_answer", "new_password"})
	if err != nil {
		return nil, err
	}
	account, err := launcherAccount(body["account"])
	if err != nil {
		return nil, err
	}
	password, err := launcherPassword(body["new_password"])
	if err != nil {
		return nil, err
	}
	question, err := launcherQuestion(body["security_question"])
	if err != nil {
		return nil, err
	}
	answer, err := launcherAnswer(body["security_answer"])
	if err != nil {
		return nil, err
	}
	if err = l.limited(ip, "recovery", 10*time.Minute, 6); err != nil {
		return nil, err
	}
	if err = l.acquire(); err != nil {
		return nil, err
	}
	defer l.release()
	var aid int
	var storedQuestion, answerHash string
	var locked float64
	err = l.manager.store.db.QueryRowContext(ctx, "SELECT s.account_id,s.question_id,s.answer_hash,s.locked_until FROM launcher_security_questions s JOIN accounts a ON a.account_id=s.account_id WHERE a.m_id=?", account).Scan(&aid, &storedQuestion, &answerHash, &locked)
	if err != nil && err != sql.ErrNoRows {
		return nil, err
	}
	if err == sql.ErrNoRows {
		answerHash = launcherDummyHash
	}
	now := float64(time.Now().UnixNano()) / 1e9
	valid := launcherVerify(answer, answerHash) && aid > 0 && question == storedQuestion && locked <= now
	rejected := fail(403, "SECURITY_INVALID", "security recovery rejected")
	if !valid {
		if aid > 0 {
			err = l.manager.store.transaction(ctx, func(tx *sql.Tx) error {
				var failures int
				var current float64
				e := tx.QueryRowContext(ctx, "SELECT failed_attempts,locked_until FROM launcher_security_questions WHERE account_id=?", aid).Scan(&failures, &current)
				if e != nil {
					return e
				}
				if current > now {
					return nil
				}
				if current > 0 {
					failures = 0
				}
				failures++
				until := 0.0
				if failures >= 5 {
					until = now + 900
				}
				_, e = tx.ExecContext(ctx, "UPDATE launcher_security_questions SET failed_attempts=?,locked_until=? WHERE account_id=?", failures, until, aid)
				return e
			})
			if err != nil {
				return nil, err
			}
		}
		return nil, rejected
	}
	encoded, err := launcherHash(password)
	if err != nil {
		return nil, err
	}
	recovery, recoveryHash, err := launcherRecovery()
	if err != nil {
		return nil, err
	}
	err = l.manager.store.transaction(ctx, func(tx *sql.Tx) error {
		var current, q string
		var until float64
		if e := tx.QueryRowContext(ctx, "SELECT answer_hash,question_id,locked_until FROM launcher_security_questions WHERE account_id=?", aid).Scan(&current, &q, &until); e != nil {
			return e
		}
		if current != answerHash || q != question || until > float64(time.Now().UnixNano())/1e9 {
			return rejected
		}
		if _, e := tx.ExecContext(ctx, "UPDATE accounts SET password_hash=? WHERE account_id=?", encoded, aid); e != nil {
			return e
		}
		if _, e := tx.ExecContext(ctx, "INSERT INTO launcher_account_recovery(account_id,code_hash,updated_at) VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET code_hash=excluded.code_hash,updated_at=excluded.updated_at", aid, recoveryHash, gameStamp()); e != nil {
			return e
		}
		_, e := tx.ExecContext(ctx, "UPDATE launcher_security_questions SET failed_attempts=0,locked_until=0,updated_at=? WHERE account_id=?", gameStamp(), aid)
		return e
	})
	if err != nil {
		return nil, err
	}
	// Native authentication also compares the captured password hash on every
	// authentication. A lost control reply cannot leave the old credential valid.
	_ = l.engine(ctx, "revoke_account", map[string]any{"account_id": aid}, nil)
	return map[string]any{"account": account, "recovery_code": recovery}, nil
}

func (l *launcherService) createSession(ctx context.Context, r *http.Request, ip string) (any, error) {
	body, err := launcherBody(r, []string{"account", "password"}, "device_id")
	if err != nil {
		return nil, err
	}
	account, err := launcherAccount(body["account"])
	if err != nil {
		return nil, err
	}
	password, err := launcherString(body["password"], 128)
	if err != nil {
		return nil, err
	}
	device := ""
	if v, ok := body["device_id"]; ok {
		device, err = launcherString(v, 64)
		if err != nil || device != "" && !launcherHex(device, 64) {
			return nil, fail(400, "INVALID_DEVICE", "invalid device identity")
		}
	}
	if err = l.limited(ip, "login", time.Minute, 30); err != nil {
		return nil, err
	}
	if err = l.acquire(); err != nil {
		return nil, err
	}
	defer l.release()
	var aid int
	var encoded string
	err = l.manager.store.db.QueryRowContext(ctx, "SELECT account_id,password_hash FROM accounts WHERE m_id=?", account).Scan(&aid, &encoded)
	if err != nil && err != sql.ErrNoRows {
		return nil, err
	}
	hash := encoded
	if hash == "" {
		hash = launcherDummyHash
	}
	valid := launcherVerify(password, hash)
	if !valid || aid == 0 {
		return nil, fail(401, "AUTH_FAILED", "account or password is invalid")
	}
	if encoded == "" {
		return nil, fail(403, "PASSWORD_NOT_SET", "account password is not set")
	}
	if err = l.policy(ctx, l.manager.store.db, aid, ip, device, true); err != nil {
		return nil, err
	}
	update, err := getGameConfig(ctx, l.manager.store.db, "update")
	if err != nil {
		return nil, err
	}
	if update["force"] == true && r.Header.Get("X-Launcher-Version") != update["version"] {
		return nil, fail(426, "UPDATE_REQUIRED", "launcher update required")
	}
	meta, err := l.meta(ctx)
	if err != nil {
		return nil, err
	}
	if meta["server_ready"] != true {
		return nil, fail(503, "SERVER_NOT_READY", "game services are not ready")
	}
	var result map[string]any
	err = l.engine(ctx, "create", map[string]any{"account_id": aid, "account": account, "password_hash": encoded, "remote_ip": ip, "device_id": device, "advertise_host": l.host}, &result)
	if err != nil {
		return nil, err
	}
	result["stage3"] = meta["stage3"]
	return result, nil
}
