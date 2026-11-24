package manager

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"newadmin/internal/adminapi"
)

func getGameConfig(ctx context.Context, q rowQuerier, section string) (map[string]any, error) {
	var raw string
	err := q.QueryRowContext(ctx, "SELECT value FROM gmkit_config WHERE section=?", section).Scan(&raw)
	if err == sql.ErrNoRows {
		return map[string]any{}, nil
	}
	if err != nil {
		return nil, err
	}
	value := map[string]any{}
	err = json.Unmarshal([]byte(raw), &value)
	return value, err
}
func setGameConfig(ctx context.Context, tx *sql.Tx, section string, value any) error {
	_, err := tx.ExecContext(ctx, "INSERT INTO gmkit_config(section,value) VALUES(?,?) ON CONFLICT(section) DO UPDATE SET value=excluded.value", section, jsonText(value))
	return err
}
func nativeRewards(reward adminapi.Reward) []map[string]any {
	out := []map[string]any{}
	for _, item := range reward.Items {
		out = append(out, map[string]any{"kind": "item", "itemId": item.ID, "itemName": "", "quantity": item.Quantity})
	}
	if reward.Points > 0 {
		out = append(out, map[string]any{"kind": "points", "amount": reward.Points})
	}
	if reward.Gold > 0 {
		out = append(out, map[string]any{"kind": "gold", "amount": reward.Gold})
	}
	return out
}
func fromNativeRewards(raw string) adminapi.Reward {
	reward := adminapi.Reward{Items: []adminapi.Item{}}
	var values []struct {
		Kind     string `json:"kind"`
		ItemID   int64  `json:"itemId"`
		Quantity int64  `json:"quantity"`
		Amount   int64  `json:"amount"`
	}
	_ = json.Unmarshal([]byte(raw), &values)
	for _, row := range values {
		switch row.Kind {
		case "item":
			reward.Items = append(reward.Items, adminapi.Item{ID: row.ItemID, Quantity: row.Quantity})
		case "points":
			reward.Points += row.Amount
		case "gold":
			reward.Gold += row.Amount
		}
	}
	return reward
}
func initialDirectConfig(ctx context.Context, tx *sql.Tx, section string) (map[string]any, error) {
	zero := adminapi.Reward{Items: []adminapi.Item{}}
	switch section {
	case "recharge":
		return map[string]any{"enabled": false, "url": ""}, nil
	case "registration":
		policy, err := getGameConfig(ctx, tx, "registrationPolicy")
		if err != nil {
			return nil, err
		}
		bonus, err := getGameConfig(ctx, tx, "registrationBonus")
		if err != nil {
			return nil, err
		}
		closed, _ := policy["closed"].(bool)
		required, _ := policy["codeRequired"].(bool)
		limit, _ := policy["maxPerIp"].(float64)
		if limit < 0 {
			limit = 0
		}
		enabled, _ := bonus["enabled"].(bool)
		reward := fromNativeRewards(jsonText(bonus["rewards"]))
		if delivery, _ := bonus["delivery"].(string); delivery == "registration" {
			raw, _ := bonus["points"].(float64)
			reward.Points = int64(raw)
		}
		return map[string]any{"open": !closed, "activation_required": required, "ip_limit": limit, "reward_enabled": enabled, "reward": reward}, nil
	case "announcement.launcher":
		var title, body string
		err := tx.QueryRowContext(ctx, "SELECT title,body FROM gmkit_notices WHERE enabled=1 ORDER BY sort_order,id LIMIT 1").Scan(&title, &body)
		if err != nil && err != sql.ErrNoRows {
			return nil, err
		}
		return map[string]any{"enabled": err == nil, "title": title, "content": body}, nil
	case "announcement.navigation", "announcement.chat":
		return map[string]any{"enabled": false, "content": "", "interval": 30}, nil
	case "welfare.online":
		return map[string]any{"enabled": false, "times": "", "reward": zero}, nil
	case "welfare.weekly", "welfare.monthly":
		return map[string]any{"enabled": false, "reward": zero}, nil
	default:
		return publishingFromRegistry(ctx, tx)
	}
}
func initializeDirect(ctx context.Context, store *stateStore) error {
	return store.transaction(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS manager_job_rules(section TEXT PRIMARY KEY,value_json TEXT NOT NULL,baseline_id INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manager_native_coupon_attempts(day TEXT NOT NULL,account_id INTEGER NOT NULL,code_hash TEXT NOT NULL,PRIMARY KEY(day,account_id,code_hash));
CREATE TABLE IF NOT EXISTS manager_welfare_slots(period TEXT PRIMARY KEY,reward TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manager_welfare_queue(period TEXT NOT NULL,character_id INTEGER NOT NULL,account_id INTEGER NOT NULL,PRIMARY KEY(period,character_id));
CREATE TABLE IF NOT EXISTS manager_job_progress(section TEXT NOT NULL,target_id INTEGER NOT NULL,elapsed REAL NOT NULL DEFAULT 0,last_seen REAL NOT NULL DEFAULT 0,counter INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(section,target_id));
CREATE TABLE IF NOT EXISTS manager_job_receipts(section TEXT NOT NULL,target_id INTEGER NOT NULL,period TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(section,target_id,period));
CREATE TABLE IF NOT EXISTS manager_links(section TEXT PRIMARY KEY,identity INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS manager_job_failures(section TEXT PRIMARY KEY,message TEXT NOT NULL,updated_at TEXT NOT NULL);`)
		if err != nil {
			return err
		}
		if err = initializeGoldLedger(ctx, tx); err != nil {
			return err
		}
		for section := range sections {
			entry, err := readConfig(ctx, tx, section)
			if err != nil {
				return err
			}
			if entry.Revision > 0 {
				continue
			}
			value, err := initialDirectConfig(ctx, tx, section)
			if err != nil {
				return err
			}
			if _, err = tx.ExecContext(ctx, "INSERT INTO manager_config(section,revision,value_json,updated_at) VALUES(?,1,?,?)", section, jsonText(value), metadataTime(time.Now())); err != nil {
				return err
			}
		}
		return nil
	})
}

func (s *Service) saveDirectConfig(ctx context.Context, tx *sql.Tx, entry *adminapi.ConfigEntry) error {
	entry.DatabaseCommitted = true
	if err := setGameConfig(ctx, tx, "gm_manager."+entry.Section, entry.Value); err != nil {
		return err
	}
	switch entry.Section {
	case "registration":
		limit := int(entry.Value["ip_limit"].(float64))
		if limit == 0 {
			limit = -1
		}
		if err := setGameConfig(ctx, tx, "registrationPolicy", map[string]any{"closed": !entry.Value["open"].(bool), "codeRequired": entry.Value["activation_required"].(bool), "maxPerIp": limit}); err != nil {
			return err
		}
		server, err := getGameConfig(ctx, tx, "server")
		if err != nil {
			return err
		}
		server["registration"] = entry.Value["open"]
		server["maxPerIp"] = limit
		if err = setGameConfig(ctx, tx, "server", server); err != nil {
			return err
		}
		// New-account rewards are delivered by the Go database worker. Disable only
		// the legacy automatic bonus to avoid charging the same rule twice.
		if err = setGameConfig(ctx, tx, "registrationBonus", map[string]any{"enabled": false, "rewards": []any{}}); err != nil {
			return err
		}
	case "announcement.launcher":
		var identity int64
		err := tx.QueryRowContext(ctx, "SELECT identity FROM manager_links WHERE section=?", entry.Section).Scan(&identity)
		enabled := 0
		if entry.Value["enabled"] == true {
			enabled = 1
		}
		if err == sql.ErrNoRows {
			row, e := tx.ExecContext(ctx, "INSERT INTO gmkit_notices(title,body,date,enabled,sort_order) VALUES(?,?,?,?,0)", entry.Value["title"], entry.Value["content"], time.Now().In(time.FixedZone("Asia/Shanghai", 8*3600)).Format("2006-01-02"), enabled)
			if e != nil {
				return e
			}
			identity, e = row.LastInsertId()
			if e != nil {
				return e
			}
			_, err = tx.ExecContext(ctx, "INSERT INTO manager_links(section,identity) VALUES(?,?)", entry.Section, identity)
		} else if err == nil {
			_, err = tx.ExecContext(ctx, "UPDATE gmkit_notices SET title=?,body=?,enabled=? WHERE id=?", entry.Value["title"], entry.Value["content"], enabled, identity)
		}
		return err
	case "publishing":
		if err := setGameConfig(ctx, tx, "launcherDownload", entry.Value); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, "UPDATE gmkit_pvf_releases SET download_url=?,client_download_url=?,updated_at=? WHERE id=(SELECT release_id FROM gmkit_pvf_current WHERE slot=1)", entry.Value["update_url"], entry.Value["client_url"], time.Now().UTC().Format(time.RFC3339))
		return err
	}
	if strings.HasPrefix(entry.Section, "welfare.") || strings.HasPrefix(entry.Section, "announcement.") || entry.Section == "registration" {
		baseline := 0
		if err := tx.QueryRowContext(ctx, "SELECT COALESCE(MAX(account_id),0) FROM accounts").Scan(&baseline); err != nil {
			return err
		}
		if entry.Section == "registration" && entry.Value["reward_enabled"] == true {
			var old string
			var priorBaseline int
			if e := tx.QueryRowContext(ctx, "SELECT value_json,baseline_id FROM manager_job_rules WHERE section='registration'").Scan(&old, &priorBaseline); e == nil {
				var value map[string]any
				if json.Unmarshal([]byte(old), &value) == nil && value["reward_enabled"] == true {
					baseline = priorBaseline
				}
			}
		}
		if _, err := tx.ExecContext(ctx, "INSERT INTO manager_job_rules(section,value_json,baseline_id,revision,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(section) DO UPDATE SET value_json=excluded.value_json,baseline_id=excluded.baseline_id,revision=excluded.revision,updated_at=excluded.updated_at", entry.Section, jsonText(entry.Value), baseline, entry.Revision, metadataTime(entry.UpdatedAt)); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, "DELETE FROM manager_job_progress WHERE section=?", entry.Section); err != nil {
			return err
		}
	}
	return nil
}

func (s *Service) directCodes(w http.ResponseWriter, r *http.Request) {
	p, size, err := pageParams(r)
	if err != nil {
		writeError(w, err, "")
		return
	}
	items := []adminapi.Code{}
	kind := r.URL.Query().Get("kind")
	keyword := r.URL.Query().Get("keyword")
	state := r.URL.Query().Get("state")
	if kind == "" || kind == "cdk" {
		rows, e := s.store.db.QueryContext(r.Context(), "SELECT code,name,COALESCE(rewards_json,'[]'),enabled,used,max_uses,created_at FROM gmkit_cdks ORDER BY id DESC")
		if e != nil {
			writeError(w, e, "")
			return
		}
		for rows.Next() {
			var row adminapi.Code
			var reward, created string
			var enabled, used, maxUses int
			if e = rows.Scan(&row.Code, &row.Name, &reward, &enabled, &used, &maxUses, &created); e != nil {
				rows.Close()
				writeError(w, e, "")
				return
			}
			row.Kind = "cdk"
			row.Reward = fromNativeRewards(reward)
			row.State = "active"
			if enabled == 0 {
				row.State = "revoked"
			} else if maxUses > 0 && used >= maxUses {
				row.State = "used"
			}
			row.CreatedAt = parseGameTime(created)
			if keywordMatch(keyword, row.Code, row.Name) && (state == "" || state == row.State) {
				items = append(items, row)
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			writeError(w, err, "")
			return
		}
	}
	if kind == "" || kind == "registration" {
		rows, e := s.store.db.QueryContext(r.Context(), "SELECT r.code,COALESCE(m.name,'注册码'),r.state,r.created_at FROM gmkit_registration_codes r LEFT JOIN manager_codes m ON m.code=r.code ORDER BY r.created_at DESC,r.code")
		if e != nil {
			writeError(w, e, "")
			return
		}
		for rows.Next() {
			var row adminapi.Code
			var created string
			if e = rows.Scan(&row.Code, &row.Name, &row.State, &created); e != nil {
				rows.Close()
				writeError(w, e, "")
				return
			}
			row.Kind = "registration"
			row.Reward = adminapi.Reward{Items: []adminapi.Item{}}
			row.CreatedAt = parseGameTime(created)
			if keywordMatch(keyword, row.Code, row.Name) && (state == "" || state == row.State) {
				items = append(items, row)
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			writeError(w, err, "")
			return
		}
	}
	page := paginate(items, p, size)
	page.Source = "game_database"
	write(w, 200, adminapi.Envelope{OK: true, Data: page})
}
func parseGameTime(value string) time.Time {
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02 15:04:05"} {
		if t, err := time.ParseInLocation(layout, value, time.FixedZone("Asia/Shanghai", 8*3600)); err == nil {
			return t
		}
	}
	return time.Time{}
}

func (s *Service) directPreview(w http.ResponseWriter, r *http.Request) {
	if !s.config.DirectDatabase {
		writeError(w, fail(501, "direct_mode_required", "当前不是数据库直连模式"), "")
		return
	}
	var body adminapi.Action
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	characters := body.Name == "mail.send" || body.Currency == "gold"
	online := map[int]bool{}
	if body.Scope == "online" {
		snap, err := s.game.session(r.Context())
		if err != nil || !snap.Complete {
			writeError(w, fail(503, "online_unknown", "在线快照未知，未扩大发放范围"), "")
			return
		}
		for _, c := range snap.Connections {
			if characters {
				online[c.CharacterID] = true
			} else {
				online[c.AccountID] = true
			}
		}
	}
	db, err := s.reader.open()
	if err != nil {
		writeError(w, err, "")
		return
	}
	defer db.Close()
	tx, err := db.BeginTx(r.Context(), &sql.TxOptions{ReadOnly: true})
	if err != nil {
		writeError(w, err, "")
		return
	}
	defer tx.Rollback()
	targets, err := s.directTargets(r.Context(), tx, body, characters, online)
	if err != nil {
		writeError(w, err, "")
		return
	}
	lines := []string{}
	for _, target := range targets[:min(20, len(targets))] {
		id := target.account
		if characters {
			id = target.character
		}
		lines = append(lines, target.name+"（ID "+strconv.Itoa(id)+"，账号ID "+strconv.Itoa(target.account)+"）")
	}
	write(w, 200, adminapi.Envelope{OK: true, Data: map[string]any{"total": len(targets), "items": lines, "database_committed": false, "source": "game_database"}})
}
