package manager

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"newadmin/internal/adminapi"
	"sort"
	"strings"
	"time"
)

func welfareTimes(raw string) (string, error) {
	raw = strings.TrimSpace(strings.ReplaceAll(raw, "，", ","))
	if raw == "" {
		return "", nil
	}
	fields := strings.Split(raw, ",")
	if len(fields) > 1440 {
		return "", fmt.Errorf("发放时刻过多")
	}
	seen := map[string]bool{}
	for i, v := range fields {
		t, e := time.Parse("15:04", strings.TrimSpace(v))
		if e != nil {
			return "", e
		}
		fields[i] = t.Format("15:04")
		if seen[fields[i]] {
			return "", fmt.Errorf("发放钟点重复")
		}
		seen[fields[i]] = true
	}
	sort.Strings(fields)
	return strings.Join(fields, ","), nil
}

// Freeze the first complete clock-edge snapshot before attempting delivery.
// Missed times are not replayed using people who signed in afterwards.
func (s *Service) runFixedWelfare(ctx context.Context, rule jobRule, now time.Time, snap *gameSession) error {
	local := now.In(chinaZone())
	times, _ := rule.value["times"].(string)
	minute := local.Format("15:04")
	period := local.Format("2006-01-02 15:04")
	if rule.value["enabled"] == true && snap != nil && snap.Complete && local.Second() < 5 && strings.Contains(","+times+",", ","+minute+",") {
		err := s.store.transaction(ctx, func(tx *sql.Tx) error {
			result, e := tx.ExecContext(ctx, "INSERT OR IGNORE INTO manager_welfare_slots(period,reward) SELECT ?,? WHERE EXISTS (SELECT 1 FROM manager_job_rules WHERE section=? AND revision=?)", period, jsonText(rule.value["reward"]), rule.section, rule.revision)
			if e != nil {
				return e
			}
			n, e := result.RowsAffected()
			if e != nil || n == 0 {
				return e
			}
			for _, row := range snap.Connections {
				if row.CharacterID <= 0 || row.AccountID <= 0 {
					continue
				}
				_, e = tx.ExecContext(ctx, "INSERT OR IGNORE INTO manager_welfare_queue(period,character_id,account_id) SELECT ?,character_id,account_id FROM characters WHERE character_id=? AND account_id=? AND delete_flag=0", period, row.CharacterID, row.AccountID)
				if e != nil {
					return e
				}
			}
			return nil
		})
		if err != nil {
			return err
		}
	}
	// Frozen grants survive restarts and configuration edits.
	return s.store.transaction(ctx, func(tx *sql.Tx) error {
		rows, e := tx.QueryContext(ctx, "SELECT q.period,q.character_id,q.account_id,s.reward FROM manager_welfare_queue q JOIN manager_welfare_slots s ON s.period=q.period ORDER BY q.period,q.character_id LIMIT 100")
		if e != nil {
			return e
		}
		type grant struct {
			period, raw string
			target      directTarget
		}
		var pending []grant
		for rows.Next() {
			var g grant
			if e = rows.Scan(&g.period, &g.target.character, &g.target.account, &g.raw); e != nil {
				rows.Close()
				return e
			}
			pending = append(pending, g)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return e
		}
		for _, g := range pending {
			var reward adminapi.Reward
			if e = json.Unmarshal([]byte(g.raw), &reward); e != nil {
				return e
			}
			ref := fmt.Sprintf("job:welfare.online:%d:%s", g.target.character, g.period)
			result, e := directReward(ctx, tx, g.target, reward, "GM定时福利", g.period, ref)
			if e != nil {
				return e
			}
			if e = jobSaved(ctx, tx, rule.section, g.target.character, g.period, result); e != nil {
				return e
			}
			if _, e = tx.ExecContext(ctx, "DELETE FROM manager_welfare_queue WHERE period=? AND character_id=?", g.period, g.target.character); e != nil {
				return e
			}
		}
		return nil
	})
}
