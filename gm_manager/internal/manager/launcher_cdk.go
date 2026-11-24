package manager

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"unicode/utf16"
)

type launcherReward struct {
	Kind     string `json:"kind"`
	ItemID   int64  `json:"itemId,omitempty"`
	ItemName string `json:"itemName,omitempty"`
	Quantity int64  `json:"quantity,omitempty"`
	Amount   int64  `json:"amount,omitempty"`
}
type launcherCDK struct {
	id, enabled                         int
	name, raw, itemName                 string
	points, itemID, quantity, used, max int64
}

func launcherReadCDK(ctx context.Context, q launcherQuerier, code string) (launcherCDK, error) {
	var c launcherCDK
	var raw sql.NullString
	err := q.QueryRowContext(ctx, "SELECT id,name,points,item_id,item_name,quantity,max_uses,used,enabled,rewards_json FROM gmkit_cdks WHERE code=?", code).Scan(&c.id, &c.name, &c.points, &c.itemID, &c.itemName, &c.quantity, &c.max, &c.used, &c.enabled, &raw)
	c.raw = raw.String
	if err == sql.ErrNoRows {
		return c, fail(409, "CDK_UNAVAILABLE", "redemption code unavailable")
	}
	return c, err
}
func (c launcherCDK) rewards() ([]launcherReward, error) {
	out := []launcherReward{}
	if c.raw != "" {
		if err := json.Unmarshal([]byte(c.raw), &out); err != nil {
			return nil, err
		}
	} else {
		if c.points > 0 {
			out = append(out, launcherReward{Kind: "points", Amount: c.points})
		}
		if c.itemID > 0 {
			out = append(out, launcherReward{Kind: "item", ItemID: c.itemID, ItemName: c.itemName, Quantity: c.quantity})
		}
	}
	if err := launcherValidateRewards(out); err != nil {
		return nil, err
	}
	return out, nil
}
func launcherValidateRewards(items []launcherReward) error {
	if len(items) < 1 || len(items) > 100 {
		return fail(409, "CDK_REJECTED", "invalid reward list")
	}
	var points, gold int64
	for _, v := range items {
		switch v.Kind {
		case "points":
			if v.Amount < 1 || v.Amount > 99999999 {
				return fail(409, "CDK_REJECTED", "invalid point amount")
			}
			points += v.Amount
		case "gold":
			if v.Amount < 1 || v.Amount > 999999999 {
				return fail(409, "CDK_REJECTED", "invalid gold amount")
			}
			gold += v.Amount
		case "item":
			if v.ItemID < 1 || v.ItemID > 2000000000 || v.Quantity < 1 || v.Quantity > 9999 || len([]rune(v.ItemName)) > 100 {
				return fail(409, "CDK_REJECTED", "invalid item reward")
			}
		default:
			return fail(409, "CDK_REJECTED", "unknown reward")
		}
	}
	if points > 99999999 || gold > 999999999 {
		return fail(409, "CDK_REJECTED", "reward exceeds balance limit")
	}
	return nil
}

// Preserve the Python atomic receipt digest, including ensure_ascii and
// separators, so retries issued before cutover remain replayable afterward.
func launcherPythonJSON(value any) ([]byte, error) {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(value); err != nil {
		return nil, err
	}
	var out strings.Builder
	for _, r := range strings.TrimSuffix(b.String(), "\n") {
		if r < 128 {
			out.WriteRune(r)
		} else if r <= 0xffff {
			fmt.Fprintf(&out, "\\u%04x", r)
		} else {
			a, b := utf16.EncodeRune(r)
			fmt.Fprintf(&out, "\\u%04x\\u%04x", a, b)
		}
	}
	return []byte(out.String()), nil
}
func launcherCDKDigest(code string, cid, aid int) (string, error) {
	raw, err := launcherPythonJSON([]any{"cdk.redeem", map[string]any{"code": code, "characterId": cid}, "player:" + strconv.Itoa(aid)})
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), err
}
func launcherCDKPrior(ctx context.Context, q launcherQuerier, rid, digest string) (map[string]any, error) {
	var action, stored, raw string
	err := q.QueryRowContext(ctx, "SELECT action,fingerprint,response FROM gmkit_atomic_receipts WHERE id=?", rid).Scan(&action, &stored, &raw)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if action != "cdk.redeem" || stored != digest {
		return nil, fail(409, "CDK_REJECTED", "request ID used for another operation")
	}
	var reply map[string]any
	if err = json.Unmarshal([]byte(raw), &reply); err != nil {
		return nil, err
	}
	if _, ok := reply["result"].(map[string]any); !ok {
		return nil, fmt.Errorf("invalid CDK receipt")
	}
	return reply, nil
}
func (l *launcherService) redeem(ctx context.Context, r *http.Request, aid int) (any, error) {
	return l.redeemWithRefresh(ctx, r, aid, true)
}
func (l *launcherService) redeemWithRefresh(ctx context.Context, r *http.Request, aid int, notify bool) (any, error) {
	body, err := launcherBody(r, []string{"code", "character_id", "request_id"})
	if err != nil {
		return nil, err
	}
	code, err := launcherString(body["code"], 100)
	code = strings.TrimSpace(code)
	if normalized, valid := nativeCouponCode(code); valid && strings.Contains(code, "-") {
		code = normalized
	}
	if err != nil || code == "" || strings.ContainsRune(code, 0) {
		return nil, fail(400, "INVALID_REQUEST", "invalid redemption code")
	}
	cid64, err := launcherInteger(body["character_id"], 1, 2147483647)
	if err != nil {
		return nil, err
	}
	cid := int(cid64)
	rid, err := launcherString(body["request_id"], 32)
	if err != nil || !launcherHex(rid, 32) {
		return nil, fail(400, "INVALID_REQUEST", "invalid request ID")
	}
	rid = "launcher-cdk-" + rid
	digest, err := launcherCDKDigest(code, cid, aid)
	if err != nil {
		return nil, err
	}
	saved, err := launcherCDKPrior(ctx, l.manager.store.db, rid, digest)
	if err != nil {
		return nil, err
	}
	if saved != nil {
		return launcherCDKReply(saved), nil
	}
	c, err := launcherReadCDK(ctx, l.manager.store.db, strings.ToUpper(code))
	if err != nil {
		return nil, err
	}
	rewards, err := c.rewards()
	if err != nil {
		return nil, err
	}
	needsMetadata := false
	for _, v := range rewards {
		needsMetadata = needsMetadata || v.Kind == "item"
	}
	prepared := rewards
	if needsMetadata {
		if err = l.engine(ctx, "prepare_rewards", map[string]any{"rewards": rewards}, &prepared); err != nil {
			return nil, err
		}
		if err = launcherValidateRewards(prepared); err != nil {
			return nil, err
		}
	}
	// Verify that native metadata preparation only split equipment quantities.
	if !launcherSameRewards(rewards, prepared) {
		return nil, fail(502, "CDK_REJECTED", "prepared rewards mismatch")
	}
	var reply map[string]any
	err = l.manager.store.transaction(ctx, func(tx *sql.Tx) error {
		old, e := launcherCDKPrior(ctx, tx, rid, digest)
		if e != nil {
			return e
		}
		if old != nil {
			reply = old
			return nil
		}
		// Legacy pending/unknown operations without an atomic receipt need an audit;
		// do not silently reissue them after cutover.
		var oldCount int
		if e = tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM gmkit_requests WHERE id=?", rid).Scan(&oldCount); e != nil {
			return e
		}
		if oldCount > 0 {
			return fail(409, "CDK_REJECTED", "previous request requires reconciliation")
		}
		var owner int
		e = tx.QueryRowContext(ctx, "SELECT account_id FROM characters WHERE character_id=? AND delete_flag=0", cid).Scan(&owner)
		if e == sql.ErrNoRows || e == nil && owner != aid {
			return fail(403, "CHARACTER_FORBIDDEN", "character does not belong to this account")
		}
		if e != nil {
			return e
		}
		if e = l.policy(ctx, tx, aid, "", "", true); e != nil {
			return e
		}
		current, e := launcherReadCDK(ctx, tx, strings.ToUpper(code))
		if e != nil {
			return e
		}
		// A GM may edit the code while item metadata is being read.
		if current.id != c.id || current.raw != c.raw || current.points != c.points || current.itemID != c.itemID || current.quantity != c.quantity || current.itemName != c.itemName {
			return fail(409, "CDK_REJECTED", "reward changed; retry with a new request")
		}
		var used int
		if e = tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM gmkit_redemptions WHERE cdk_id=? AND account_id=?", c.id, aid).Scan(&used); e != nil {
			return e
		}
		if used > 0 {
			return fail(409, "CDK_ALREADY_REDEEMED", "account has already redeemed this code")
		}
		if current.enabled != 1 || current.max > 0 && current.used >= current.max {
			return fail(409, "CDK_UNAVAILABLE", "redemption code unavailable")
		}
		insert, e := tx.ExecContext(ctx, "INSERT INTO gmkit_redemptions(cdk_id,account_id,character_id,created_at) VALUES(?,?,?,?)", c.id, aid, cid, gameStamp())
		if e != nil {
			return e
		}
		redemptionID, e := insert.LastInsertId()
		if e != nil {
			return e
		}
		points := int64(0)
		for _, reward := range prepared {
			if reward.Kind == "points" {
				points += reward.Amount
			}
		}
		if points > 0 {
			if _, e = directCredit(ctx, tx, directTarget{account: aid, character: cid}, "cera", points, rid+":points"); e != nil {
				return e
			}
		}
		title := []rune(current.name)
		if len(title) > 78 {
			title = title[:78]
		}
		mails := []int64{}
		for index, reward := range prepared {
			var mid any
			state := "credited"
			if reward.Kind == "item" || reward.Kind == "gold" {
				gold := int64(0)
				if reward.Kind == "gold" {
					gold = reward.Amount
				}
				id, e := launcherSendMail(ctx, tx, cid, string(title)+"奖励", "", reward.ItemID, reward.ItemName, reward.Quantity, gold)
				if e != nil {
					return e
				}
				mid = id
				mails = append(mails, id)
				state = "pending_claim"
			}
			if _, e = tx.ExecContext(ctx, "INSERT INTO gmkit_reward_deliveries(redemption_id,reward_index,kind,mail_id,status,result) VALUES(?,?,?,?,?,?)", redemptionID, index, reward.Kind, mid, state, jsonText(reward)); e != nil {
				return e
			}
		}
		if points > 0 && len(mails) == 0 {
			id, e := launcherSendMail(ctx, tx, cid, string(title)+"兑换", "CDK 兑换成功，点券 "+strconv.FormatInt(points, 10)+" 已计入账号余额。", 0, "", 0, 0)
			if e != nil {
				return e
			}
			mails = append(mails, id)
		}
		if _, e = tx.ExecContext(ctx, "UPDATE gmkit_cdks SET used=used+1 WHERE id=?", c.id); e != nil {
			return e
		}
		var first any
		if len(mails) > 0 {
			first = mails[0]
		}
		result := map[string]any{"redemptionId": redemptionID, "points": points, "mailId": first, "mailIds": mails}
		reply = map[string]any{"message": "兑换奖励已提交", "result": result}
		if _, e = tx.ExecContext(ctx, "INSERT INTO gmkit_atomic_receipts(id,action,fingerprint,response) VALUES(?,?,?,?)", rid, "cdk.redeem", digest, jsonText(reply)); e != nil {
			return e
		}
		_, e = tx.ExecContext(ctx, "INSERT INTO gmkit_audit(action,summary,actor,request_id,outcome,created_at) VALUES(?,?,?,?,?,?)", "cdk.redeem", "复合奖励兑换已原子提交", "player:"+strconv.Itoa(aid), rid, "database_committed", gameStamp())
		return e
	})
	if err != nil {
		return nil, err
	}
	// Refresh is a best-effort notification after the durable award transaction.
	// Its failure cannot cause another award on retry.
	if notify {
		var notified map[string]any
		_ = l.engine(ctx, "refresh_mailbox", map[string]any{"account_id": aid, "character_id": cid, "request_id": rid + "-refresh"}, &notified)
	}
	return launcherCDKReply(reply), nil
}
func launcherSameRewards(a, b []launcherReward) bool {
	totals := func(items []launcherReward) map[string]int64 {
		m := map[string]int64{}
		for _, v := range items {
			key := v.Kind
			amount := v.Amount
			if v.Kind == "item" {
				key += ":" + strconv.FormatInt(v.ItemID, 10)
				amount = v.Quantity
			}
			m[key] += amount
		}
		return m
	}
	x, y := totals(a), totals(b)
	if len(x) != len(y) {
		return false
	}
	for k, v := range x {
		if y[k] != v {
			return false
		}
	}
	return true
}
func launcherSendMail(ctx context.Context, tx *sql.Tx, cid int, title, body string, item int64, name string, quantity, gold int64) (int64, error) {
	result, err := tx.ExecContext(ctx, "INSERT INTO gmkit_mails(character_id,title,body,item_id,item_name,quantity,gold,created_at) VALUES(?,?,?,?,?,?,?,?)", cid, title, body, item, name, quantity, gold, gameStamp())
	if err != nil {
		return 0, err
	}
	id, err := result.LastInsertId()
	if err != nil {
		return 0, err
	}
	if id >= 0x40000000 {
		return 0, fail(409, "CDK_REJECTED", "mail ID limit reached")
	}
	_, err = tx.ExecContext(ctx, "INSERT INTO gmkit_native_mail_state(mail_id,read_state) VALUES(?,1)", id)
	return id, err
}
func launcherCDKReply(saved map[string]any) map[string]any {
	return map[string]any{"message": "兑换成功，奖励邮件已寄至所选角色的游戏内邮箱；点券奖励直接计入账号。", "result": saved["result"]}
}
