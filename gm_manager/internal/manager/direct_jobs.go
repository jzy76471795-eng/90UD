package manager

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"sort"
	"strconv"
	"strings"
	"time"

	"newadmin/internal/adminapi"
)

type jobRule struct {
	section  string
	value    map[string]any
	baseline int
	revision int64
}

func (s *Service) startJobs() {
	ctx, cancel := context.WithCancel(context.Background())
	s.cancel = cancel
	s.done = make(chan struct{})
	go func() {
		defer close(s.done)
		ticker := time.NewTicker(3 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-ticker.C:
				work, cancel := context.WithTimeout(ctx, 8*time.Second)
				err := s.runJobs(work, now)
				cancel()
				if err != nil && ctx.Err() == nil {
					log.Printf("数据库规则轮询未完成，未完成事务已回滚：%T", err)
				}
			}
		}
	}()
}

func (s *Service) runJobs(ctx context.Context, now time.Time) error {
	rows, err := s.store.db.QueryContext(ctx, "SELECT section,value_json,baseline_id,revision FROM manager_job_rules ORDER BY section")
	if err != nil {
		return err
	}
	rules := []jobRule{}
	needsOnline := false
	for rows.Next() {
		var rule jobRule
		var raw string
		if err = rows.Scan(&rule.section, &raw, &rule.baseline, &rule.revision); err != nil {
			rows.Close()
			return err
		}
		if err = json.Unmarshal([]byte(raw), &rule.value); err != nil {
			rows.Close()
			return err
		}
		rules = append(rules, rule)
		if strings.HasPrefix(rule.section, "welfare.") && rule.value["enabled"] == true {
			needsOnline = true
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	online := map[int]int{}
	var snapshot *gameSession
	if needsOnline {
		snap, e := s.game.session(ctx)
		if e == nil && snap.Complete {
			snapshot = &snap
			for _, row := range snap.Connections {
				if row.AccountID > 0 && row.CharacterID > 0 {
					if old := online[row.AccountID]; old == 0 || row.CharacterID < old {
						online[row.AccountID] = row.CharacterID
					}
				}
			}
		}
	}
	for _, rule := range rules {
		if rule.section == "welfare.online" {
			err = s.runFixedWelfare(ctx, rule, now, snapshot)
		} else {
			err = s.runRule(ctx, rule, now, online)
		}
		if err != nil {
			message := "规则执行失败，事务已回滚，请检查管理日志"
			if failure, ok := err.(*apiFailure); ok {
				message = failure.Message
			}
			_, _ = s.store.db.ExecContext(ctx, "INSERT INTO manager_job_failures(section,message,updated_at) VALUES(?,?,?) ON CONFLICT(section) DO UPDATE SET message=excluded.message,updated_at=excluded.updated_at", rule.section, message, metadataTime(now))
			return err
		}
		_, _ = s.store.db.ExecContext(ctx, "DELETE FROM manager_job_failures WHERE section=?", rule.section)
	}
	return nil
}

func jobAlready(ctx context.Context, tx *sql.Tx, section string, target int, period string) (bool, error) {
	var n int
	err := tx.QueryRowContext(ctx, "SELECT count(*) FROM manager_job_receipts WHERE section=? AND target_id=? AND period=?", section, target, period).Scan(&n)
	return n > 0, err
}
func jobSaved(ctx context.Context, tx *sql.Tx, section string, target int, period string, result any) error {
	_, err := tx.ExecContext(ctx, "INSERT INTO manager_job_receipts(section,target_id,period,result_json,created_at) VALUES(?,?,?,?,?)", section, target, period, jsonText(result), gameStamp())
	return err
}

func (s *Service) runRule(ctx context.Context, rule jobRule, now time.Time, online map[int]int) error {
	enabled := rule.value["enabled"] == true
	if rule.section == "registration" {
		enabled = rule.value["reward_enabled"] == true
	}
	if !enabled {
		return nil
	}
	return s.store.transaction(ctx, func(tx *sql.Tx) error {
		// A UI save can happen after the poll. Never apply a stale rule.
		var revision int64
		if err := tx.QueryRowContext(ctx, "SELECT revision FROM manager_job_rules WHERE section=?", rule.section).Scan(&revision); err != nil {
			return err
		}
		if revision != rule.revision {
			return nil
		}
		if strings.HasPrefix(rule.section, "announcement.") {
			seconds, _ := rule.value["interval"].(float64)
			if rule.section == "announcement.chat" {
				seconds *= 60
			}
			if seconds < 1 {
				return fail(400, "invalid_interval", "公告间隔无效")
			}
			// Run at most once after a restart; missed intervals are never replayed.
			period := fmt.Sprintf("%d:%d", revision, now.Unix()/int64(seconds))
			prior, err := jobAlready(ctx, tx, rule.section, 0, period)
			if err != nil || prior {
				return err
			}
			_, err = tx.ExecContext(ctx, "INSERT INTO gmkit_announcements(content,color,source,created_at,expires_at) VALUES(?,?,?,?,?)", rule.value["content"], "0xFF0000FF", "go-gm:"+rule.section+":"+period, now.In(chinaZone()).Format(time.RFC3339), now.Add(5*time.Minute).In(chinaZone()).Format(time.RFC3339))
			if err != nil {
				return err
			}
			return jobSaved(ctx, tx, rule.section, 0, period, map[string]any{"queued": true})
		}
		reward, err := decodeReward(rule.value["reward"])
		if err != nil {
			return err
		}
		if err = validateReward(reward, 100); err != nil {
			return err
		}
		targets := []directTarget{}
		if rule.section == "registration" {
			rows, e := tx.QueryContext(ctx, "SELECT a.account_id,MIN(c.character_id) FROM accounts a JOIN characters c ON c.account_id=a.account_id AND c.delete_flag=0 WHERE a.account_id>? AND NOT EXISTS(SELECT 1 FROM manager_job_receipts r WHERE r.section='registration' AND r.target_id=a.account_id AND r.period='registration') GROUP BY a.account_id ORDER BY a.account_id LIMIT 500", rule.baseline)
			if e != nil {
				return e
			}
			for rows.Next() {
				var t directTarget
				if e = rows.Scan(&t.account, &t.character); e != nil {
					rows.Close()
					return e
				}
				targets = append(targets, t)
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return err
			}
		} else {
			ids := []int{}
			for account := range online {
				ids = append(ids, account)
			}
			sort.Ints(ids)
			for _, account := range ids {
				character := online[account]
				var n int
				if err = tx.QueryRowContext(ctx, "SELECT count(*) FROM characters WHERE character_id=? AND account_id=? AND delete_flag=0", character, account).Scan(&n); err != nil {
					return err
				}
				if n == 1 {
					targets = append(targets, directTarget{account: account, character: character})
				}
			}
		}
		for _, target := range targets {
			period := "registration"
			switch rule.section {
			case "welfare.weekly":
				year, week := now.In(chinaZone()).ISOWeek()
				period = fmt.Sprintf("%04d-W%02d", year, week)
			case "welfare.monthly":
				period = now.In(chinaZone()).Format("2006-01")

			}
			prior, e := jobAlready(ctx, tx, rule.section, target.account, period)
			if e != nil {
				return e
			}
			if prior {
				continue
			}
			reference := "job:" + rule.section + ":" + strconv.Itoa(target.account) + ":" + period
			result, e := directReward(ctx, tx, target, reward, "GM福利", rule.section, reference)
			if e != nil {
				return e
			}
			if e = jobSaved(ctx, tx, rule.section, target.account, period, result); e != nil {
				return e
			}
			if _, e = tx.ExecContext(ctx, "INSERT INTO gmkit_audit(action,summary,actor,request_id,outcome,created_at) VALUES(?,?,?,?,?,?)", rule.section, strconv.Itoa(target.account), "go-gm-worker", reference, "database_committed", gameStamp()); e != nil {
				return e
			}
		}
		return nil
	})
}
func chinaZone() *time.Location { return time.FixedZone("Asia/Shanghai", 8*3600) }

func directReward(ctx context.Context, tx *sql.Tx, target directTarget, reward adminapi.Reward, title, body, reference string) (map[string]any, error) {
	for _, asset := range []struct {
		currency string
		amount   int64
	}{{"gold", reward.Gold}, {"cera", reward.Points}, {"token_cera", reward.Tokens}} {
		if asset.amount > 0 {
			if _, err := directCredit(ctx, tx, target, asset.currency, asset.amount, reference+":"+asset.currency); err != nil {
				return nil, err
			}
		}
	}
	result := map[string]any{"account_id": target.account, "character_id": target.character, "reward": reward}
	if len(reward.Items) > 0 {
		added, err := tx.ExecContext(ctx, "INSERT INTO gmkit_mails(character_id,title,body,item_id,item_name,quantity,gold,status,created_at) VALUES(?,?,?,0,'',0,0,'pending',?)", target.character, title, body, gameStamp())
		if err != nil {
			return nil, err
		}
		id, err := added.LastInsertId()
		if err != nil {
			return nil, err
		}
		for position, item := range reward.Items {
			if _, err = tx.ExecContext(ctx, "INSERT INTO gmkit_mail_items(mail_id,position,item_id,quantity,item_name,status) VALUES(?,?,?,?,?,'pending')", id, position, item.ID, item.Quantity, fmt.Sprintf("物品#%d", item.ID)); err != nil {
				return nil, err
			}
		}
		if _, err = tx.ExecContext(ctx, "INSERT INTO gmkit_native_mail_state(mail_id,read_state) VALUES(?,1)", id); err != nil {
			return nil, err
		}
		result["mail_id"] = id
	}
	return result, nil
}
