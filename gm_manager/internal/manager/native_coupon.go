package manager

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"newadmin/internal/adminapi"
)

var nativeCouponPattern = regexp.MustCompile(`^[A-Z0-9]{16}$`)
var nativeCouponGroups = regexp.MustCompile(`^[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3}$`)

func nativeCouponCode(value string) (string, bool) {
	value = strings.ToUpper(strings.TrimSpace(value))
	if nativeCouponGroups.MatchString(value) {
		value = strings.ReplaceAll(value, "-", "")
	}
	return value, nativeCouponPattern.MatchString(value)
}
func nativeCouponRequestID(aid, cid int, code string) string {
	h := sha256.Sum256([]byte("native-coupon-v1\x00" + strconv.Itoa(aid) + "\x00" + strconv.Itoa(cid) + "\x00" + code))
	return hex.EncodeToString(h[:16])
}
func nativeCouponFailure(err error) int {
	var failure *apiFailure
	if errors.As(err, &failure) {
		if failure.Code == "CDK_ALREADY_REDEEMED" {
			return 1
		}
		if failure.Code == "COUPON_ATTEMPT_LIMIT" {
			return 2
		}
	}
	return 3
}

// Manager authentication and loopback are both required. The player listener
// never accepts account_id supplied by a player as identity authority.
func (s *Service) nativeCoupon(w http.ResponseWriter, r *http.Request) {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	ip := net.ParseIP(host)
	if err != nil || ip == nil || !ip.IsLoopback() || !s.config.DirectDatabase {
		writeError(w, fail(403, "FORBIDDEN", "game adapter is loopback only"), "")
		return
	}
	var body struct {
		AccountID   int    `json:"account_id"`
		CharacterID int    `json:"character_id"`
		Code        string `json:"code"`
	}
	if err = decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	code, valid := nativeCouponCode(body.Code)
	if !valid || body.AccountID < 1 || body.CharacterID < 1 {
		writeError(w, fail(400, "INVALID_REQUEST", "invalid native coupon request"), "")
		return
	}
	var owner int
	err = s.store.db.QueryRowContext(r.Context(), "SELECT account_id FROM characters WHERE character_id=? AND delete_flag=0", body.CharacterID).Scan(&owner)
	if err != nil || owner != body.AccountID {
		writeError(w, fail(403, "CHARACTER_FORBIDDEN", "character does not belong to account"), "")
		return
	}
	l := &launcherService{manager: s}
	rid := nativeCouponRequestID(body.AccountID, body.CharacterID, code)
	digest, err := launcherCDKDigest(code, body.CharacterID, body.AccountID)
	if err != nil {
		writeError(w, err, "")
		return
	}
	prior, err := launcherCDKPrior(r.Context(), s.store.db, "launcher-cdk-"+rid, digest)
	if err != nil {
		writeError(w, err, "")
		return
	}
	if prior != nil {
		result, err := s.nativeCouponReply(r.Context(), launcherCDKReply(prior))
		if err != nil {
			writeError(w, err, "")
			return
		}
		write(w, 200, adminapi.Envelope{OK: true, Data: result})
		return
	}
	if err = s.nativeCouponAttempt(r.Context(), body.AccountID, code); err != nil {
		var known *apiFailure
		if errors.As(err, &known) {
			write(w, 200, adminapi.Envelope{OK: true, Data: map[string]any{"coupon_error": nativeCouponFailure(err)}})
		} else {
			writeError(w, err, "")
		}
		return
	}
	raw, _ := json.Marshal(map[string]any{"code": code, "character_id": body.CharacterID, "request_id": rid})
	request, _ := http.NewRequestWithContext(r.Context(), http.MethodPost, "http://localhost/native-coupon", bytes.NewReader(raw))
	result, err := l.redeemWithRefresh(r.Context(), request, body.AccountID, false)
	if err != nil {
		var known *apiFailure
		if errors.As(err, &known) {
			write(w, 200, adminapi.Envelope{OK: true, Data: map[string]any{"coupon_error": nativeCouponFailure(err)}})
			return
		}
		writeError(w, err, "")
		return
	}
	result, err = s.nativeCouponReply(r.Context(), result)
	if err != nil {
		writeError(w, err, "")
		return
	}
	write(w, 200, adminapi.Envelope{OK: true, Data: result})
}

func (s *Service) nativeCouponReply(ctx context.Context, reply any) (any, error) {
	data, ok := reply.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("invalid committed Coupon reply")
	}
	result, ok := data["result"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("invalid Coupon result")
	}
	var deliveries int
	if err := s.store.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM gmkit_reward_deliveries WHERE redemption_id=? AND kind IN ('item','gold')", result["redemptionId"]).Scan(&deliveries); err != nil {
		return nil, err
	}
	data["native_delivery_count"] = deliveries
	return data, nil
}

// Count distinct codes per account and Shanghai day. Transport retries do not
// consume more attempts, and an existing durable success remains replayable.
func (s *Service) nativeCouponAttempt(ctx context.Context, aid int, code string) error {
	day := time.Now().In(time.FixedZone("Asia/Shanghai", 8*3600)).Format("2006-01-02")
	h := sha256.Sum256([]byte(code))
	key := hex.EncodeToString(h[:])
	return s.store.transaction(ctx, func(tx *sql.Tx) error {
		var count int
		if err := tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM manager_native_coupon_attempts WHERE day=? AND account_id=? AND code_hash=?", day, aid, key).Scan(&count); err != nil {
			return err
		}
		if count > 0 {
			return nil
		}
		if err := tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM manager_native_coupon_attempts WHERE day=? AND account_id=?", day, aid).Scan(&count); err != nil {
			return err
		}
		if count >= 50 {
			return fail(429, "COUPON_ATTEMPT_LIMIT", "daily native coupon attempt limit reached")
		}
		_, err := tx.ExecContext(ctx, "INSERT INTO manager_native_coupon_attempts(day,account_id,code_hash) VALUES(?,?,?)", day, aid, key)
		return err
	})
}
