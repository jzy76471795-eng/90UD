package manager

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"newadmin/internal/adminapi"
)

type directTarget struct {
	account, character int
	name               string
}

func gameStamp() string {
	return time.Now().In(time.FixedZone("Asia/Shanghai", 8*3600)).Format(time.RFC3339)
}
func jsonText(v any) string { raw, _ := json.Marshal(v); return string(raw) }

func (s *Service) directTargets(ctx context.Context, tx *sql.Tx, body adminapi.Action, characters bool, online map[int]bool) ([]directTarget, error) {
	scope := body.Scope
	if scope == "" {
		scope = "single"
	}
	if scope != "single" && scope != "all" && scope != "online" {
		return nil, fail(400, "invalid_scope", "目标范围无效")
	}
	target := strings.TrimSpace(body.Target)
	nameOnly := false
	forcedID := 0
	if scope == "single" {
		if strings.HasPrefix(strings.ToLower(target), "id:") {
			var err error
			forcedID, err = strconv.Atoi(strings.TrimSpace(target[3:]))
			if err != nil || forcedID < 1 {
				return nil, fail(400, "invalid_target_id", "id: 后必须填写正整数")
			}
		} else if strings.HasPrefix(strings.ToLower(target), "name:") {
			target = strings.TrimSpace(target[5:])
			nameOnly = true
		}
	}
	query := "SELECT account_id,0,CAST(m_id AS BLOB) FROM accounts ORDER BY account_id"
	if characters {
		query = "SELECT account_id,character_id,CAST(name AS BLOB) FROM characters WHERE delete_flag=0 ORDER BY character_id"
	}
	rows, err := tx.QueryContext(ctx, query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []directTarget{}
	for rows.Next() {
		var row directTarget
		var raw []byte
		if err = rows.Scan(&row.account, &row.character, &raw); err != nil {
			return nil, err
		}
		row.name = dbText(raw)
		id := row.account
		if characters {
			id = row.character
		}
		if scope == "single" {
			wanted := body.AccountID
			if characters {
				wanted = body.CharacterID
			}
			if wanted == 0 && forcedID > 0 {
				wanted = forcedID
			}
			if wanted > 0 {
				if wanted != id {
					continue
				}
			} else if nameOnly && target != row.name || !nameOnly && target != strconv.Itoa(id) && target != row.name {
				continue
			}
		}
		if scope == "online" && !online[id] {
			continue
		}
		if characters && body.AccountID > 0 && row.account != body.AccountID {
			continue
		}
		result = append(result, row)
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	if scope == "single" && len(result) > 1 {
		return nil, fail(409, "ambiguous_target", "目标名称与其他名称/ID重合，请填写 id:数字 或 name:名称")
	}
	if len(result) == 0 {
		return nil, fail(404, "target_not_found", "没有匹配到真实目标")
	}
	if len(result) > 10000 {
		return nil, fail(400, "batch_too_large", "单次目标最多10000个，请缩小范围")
	}
	return result, nil
}

func directCredit(ctx context.Context, tx *sql.Tx, t directTarget, currency string, amount int64, reference string) (map[string]any, error) {
	column, table, idColumn, id, limit := currency, "accounts", "account_id", t.account, int64(99999999)
	switch currency {
	case "gold":
		column, table, idColumn, id, limit = "gold", "character_wallets", "character_id", t.character, 999999999
	case "cera", "token_cera", "happy_token_cera":
	default:
		return nil, fail(400, "invalid_currency", "币种无效")
	}
	if amount < 1 || amount > limit {
		return nil, fail(400, "invalid_amount", "追加数量超出范围")
	}
	var before int64
	var kind string
	if err := tx.QueryRowContext(ctx, "SELECT "+column+",typeof("+column+") FROM "+table+" WHERE "+idColumn+"=?", id).Scan(&before, &kind); err != nil {
		return nil, fail(409, "wallet_unavailable", "目标权威钱包不可用，未创建替代余额")
	}
	if kind != "integer" || before < 0 || before > limit-amount {
		return nil, fail(409, "balance_limit", "追加后余额超过范围或原余额无效")
	}
	after := before + amount
	statement := "UPDATE " + table + " SET " + column + "=? WHERE " + idColumn + "=? AND " + column + "=?"
	if currency == "gold" {
		statement = "UPDATE character_wallets SET gold=?,updated_at=CURRENT_TIMESTAMP WHERE character_id=? AND gold=?"
	}
	changed, err := tx.ExecContext(ctx, statement, after, id, before)
	if err != nil {
		return nil, err
	}
	n, _ := changed.RowsAffected()
	if n != 1 {
		return nil, fail(409, "balance_changed", "余额已变化，事务未提交")
	}
	var cid any
	if t.character > 0 {
		cid = t.character
	}
	_, err = tx.ExecContext(ctx, "INSERT INTO gmkit_value_credits(request_id,account_id,character_id,currency,amount,balance_before,balance_after,client_sync,created_at) VALUES(?,?,?,?,?,?,?,?,?)", reference, t.account, cid, currency, amount, before, after, "database_direct_next_login", gameStamp())
	return map[string]any{"account_id": t.account, "character_id": t.character, "currency": currency, "amount": amount, "balance_before": before, "balance_after": after}, err
}

func (s *Service) directAction(w http.ResponseWriter, r *http.Request, body adminapi.Action) {
	switch body.Name {
	case "currency.credit", "mail.send", "account.ban", "account.unban":
	default:
		writeError(w, fail(501, "unsupported_database_action", "该动作不是已接通的数据库操作"), r.Header.Get("Idempotency-Key"))
		return
	}
	if len([]rune(body.Body)) > 2000 || len([]rune(body.Target)) > 100 {
		writeError(w, fail(400, "invalid_action", "目标或内容过长"), "")
		return
	}
	if body.Name == "mail.send" {
		if err := validateReward(body.Reward, 10); err != nil {
			writeError(w, err, "")
			return
		}
	}
	characters := body.Name == "mail.send" || body.Name == "currency.credit" && body.Currency == "gold"
	online := map[int]bool{}
	if body.Scope == "online" {
		session, err := s.game.session(r.Context())
		if err != nil || !session.Complete {
			writeError(w, fail(503, "online_unknown", "只读在线快照不可用，未把全部目标当作在线目标"), "")
			return
		}
		for _, row := range session.Connections {
			if characters {
				online[row.CharacterID] = true
			} else {
				online[row.AccountID] = true
			}
		}
	}
	s.mutateAfter(w, r, body, "账号管理", body.Name, body.Target, func(tx *sql.Tx) (any, error) {
		targets, err := s.directTargets(r.Context(), tx, body, characters, online)
		if err != nil {
			return nil, err
		}
		results := []any{}
		accounts := map[int]bool{}
		id := r.Header.Get("Idempotency-Key")
		for _, target := range targets {
			reference := id + ":" + strconv.Itoa(target.account) + ":" + strconv.Itoa(target.character)
			switch body.Name {
			case "currency.credit":
				currency := body.Currency
				if currency == "tokens" {
					currency = "token_cera"
				}
				result, e := directCredit(r.Context(), tx, target, currency, body.Amount, reference)
				if e != nil {
					return nil, e
				}
				results = append(results, result)
			case "mail.send":
				reward := body.Reward
				if !accounts[target.account] {
					for currency, amount := range map[string]int64{"cera": reward.Points, "token_cera": reward.Tokens} {
						if amount > 0 {
							accountTarget := target
							accountTarget.character = 0
							if _, e := directCredit(r.Context(), tx, accountTarget, currency, amount, id+":"+strconv.Itoa(target.account)+":"+currency); e != nil {
								return nil, e
							}
						}
					}
					accounts[target.account] = true
				}
				mailGold := reward.Gold
				// Native bundle attachments use separate wire IDs. A separate gold
				// letter preserves the existing claim protocol for mixed rewards.
				if len(reward.Items) > 0 {
					mailGold = 0
				}
				added, e := tx.ExecContext(r.Context(), "INSERT INTO gmkit_mails(character_id,title,body,item_id,item_name,quantity,gold,status,created_at) VALUES(?,?,?,0,'',0,?,'pending',?)", target.character, "管理员邮件", body.Body, mailGold, gameStamp())
				if e != nil {
					return nil, e
				}
				mailID, e := added.LastInsertId()
				if e != nil {
					return nil, e
				}
				for position, item := range reward.Items {
					if _, e = tx.ExecContext(r.Context(), "INSERT INTO gmkit_mail_items(mail_id,position,item_id,quantity,item_name,status) VALUES(?,?,?,?,?,'pending')", mailID, position, item.ID, item.Quantity, fmt.Sprintf("物品#%d", item.ID)); e != nil {
						return nil, e
					}
				}
				if _, e = tx.ExecContext(r.Context(), "INSERT INTO gmkit_native_mail_state(mail_id,read_state) VALUES(?,1)", mailID); e != nil {
					return nil, e
				}
				results = append(results, map[string]any{"character_id": target.character, "account_id": target.account, "mail_id": mailID, "attachments": len(reward.Items)})
				if reward.Gold > 0 && len(reward.Items) > 0 {
					goldMail, e := launcherSendMail(r.Context(), tx, target.character, "管理员金币邮件", body.Body, 0, "", 0, reward.Gold)
					if e != nil {
						return nil, e
					}
					results = append(results, map[string]any{"character_id": target.character, "account_id": target.account, "mail_id": goldMail, "gold": reward.Gold})
				}
			case "account.ban":
				reason := strings.TrimSpace(body.Body)
				if reason == "" {
					reason = "GM后台封禁"
				}
				if len([]rune(reason)) > 300 {
					return nil, fail(400, "invalid_reason", "封禁原因最多300字")
				}
				if _, err = tx.ExecContext(r.Context(), "INSERT INTO gmkit_ban_rules(kind,value,account_id,reason,created_at,expires_at,revoked_at) VALUES('account',?,?,?,?,NULL,NULL) ON CONFLICT(kind,value) DO UPDATE SET account_id=excluded.account_id,reason=excluded.reason,created_at=excluded.created_at,expires_at=NULL,revoked_at=NULL", strconv.Itoa(target.account), target.account, reason, gameStamp()); err != nil {
					return nil, err
				}
				if _, err = tx.ExecContext(r.Context(), "INSERT INTO gmkit_bans(account_id,reason,created_at,expires_at) VALUES(?,?,?,NULL) ON CONFLICT(account_id) DO UPDATE SET reason=excluded.reason,created_at=excluded.created_at,expires_at=NULL", target.account, reason, gameStamp()); err != nil {
					return nil, err
				}
				results = append(results, map[string]any{"account_id": target.account, "banned": true})
			case "account.unban":
				if _, err = tx.ExecContext(r.Context(), "UPDATE gmkit_ban_rules SET revoked_at=? WHERE kind='account' AND value=?", gameStamp(), strconv.Itoa(target.account)); err != nil {
					return nil, err
				}
				if _, err = tx.ExecContext(r.Context(), "DELETE FROM gmkit_bans WHERE account_id=?", target.account); err != nil {
					return nil, err
				}
				results = append(results, map[string]any{"account_id": target.account, "banned": false})
			}
		}
		message := "数据库事务已提交；在线显示请刷新或重新登录"
		if body.Name == "mail.send" {
			message = "未读邮件已写入数据库；物品和金币由角色领取，点券类奖励直接计入账号。在线邮箱刷新失败不影响邮件保存。"
		}
		return adminapi.Receipt{RequestID: id, Action: body.Name, State: "database_committed", DatabaseCommitted: true, AppliedToGame: false, Message: message, Result: map[string]any{"targets": len(targets), "items": results}, CreatedAt: time.Now().UTC()}, nil
	}, func(value any) {
		if body.Name == "mail.send" {
			s.refreshCreatedMails(r.Context(), value)
		}
	})
}
