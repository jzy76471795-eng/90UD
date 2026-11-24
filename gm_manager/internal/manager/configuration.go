package manager

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"strings"
	"time"

	"newadmin/internal/adminapi"
)

var sections = map[string]map[string]bool{
	"announcement.navigation": {"enabled": true, "content": true, "interval": true},
	"announcement.chat":       {"enabled": true, "content": true, "interval": true},
	"announcement.launcher":   {"enabled": true, "title": true, "content": true},
	"welfare.online":          {"enabled": true, "times": true, "reward": true},
	"welfare.weekly":          {"enabled": true, "reward": true}, "welfare.monthly": {"enabled": true, "reward": true},
	"registration": {"open": true, "activation_required": true, "ip_limit": true, "reward_enabled": true, "reward": true},
	"publishing":   {"update_url": true, "client_url": true},
	"recharge":     {"enabled": true, "url": true},
}

func validateReward(r adminapi.Reward, max int) error {
	if len(r.Items) > max {
		return fail(400, "invalid_reward", fmt.Sprintf("物品最多%d件", max))
	}
	if r.Points < 0 || r.Points > 99999999 || r.Tokens < 0 || r.Tokens > 99999999 || r.Gold < 0 || r.Gold > 999999999 {
		return fail(400, "invalid_reward", "资产数量超过服务端范围")
	}
	for _, item := range r.Items {
		if item.ID < 1 || item.ID > math.MaxUint32 || item.Quantity < 1 || item.Quantity > 9999 {
			return fail(400, "invalid_reward", "物品ID或数量无效")
		}
	}
	return nil
}
func (s *Service) getConfig(w http.ResponseWriter, r *http.Request) {
	section := r.PathValue("section")
	if _, ok := sections[section]; !ok {
		writeError(w, fail(404, "unknown_section", "未知配置项"), "")
		return
	}
	entry, err := readConfig(r.Context(), s.store.db, section)
	entry.DatabaseCommitted = s.config.DirectDatabase
	if s.config.DirectDatabase && section == "publishing" {
		entry.Value, err = publishingFromRegistry(r.Context(), s.store.db)
	}
	if err != nil {
		writeError(w, err, "")
		return
	}
	write(w, 200, adminapi.Envelope{OK: true, Data: entry})
}
func (s *Service) putConfig(w http.ResponseWriter, r *http.Request) {
	section := r.PathValue("section")
	allowed, ok := sections[section]
	if !ok {
		writeError(w, fail(404, "unknown_section", "未知配置项"), "")
		return
	}
	var body struct {
		Revision int64          `json:"revision"`
		Value    map[string]any `json:"value"`
	}
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	if body.Value == nil || s.config.DirectDatabase && len(body.Value) != len(allowed) {
		writeError(w, fail(400, "invalid_config", "配置必须为对象"), "")
		return
	}
	if section == "recharge" {
		if err := validateRecharge(body.Value); err != nil {
			writeError(w, err, "")
			return
		}
	}
	for key, value := range body.Value {
		if !allowed[key] {
			writeError(w, fail(400, "invalid_config", "配置包含未知字段"), "")
			return
		}
		if key == "enabled" || key == "open" || key == "activation_required" || key == "reward_enabled" {
			if _, ok := value.(bool); !ok {
				writeError(w, fail(400, "invalid_config", "开关必须为布尔值"), "")
				return
			}
		}
		if key == "times" {
			raw, ok := value.(string)
			canonical, err := welfareTimes(raw)
			if !ok || err != nil || canonical == "" && body.Value["enabled"] == true {
				writeError(w, fail(400, "invalid_times", "请按24小时制填写北京时间，例如 12:00,20:30"), "")
				return
			}
			body.Value[key] = canonical
		}
		if key == "interval" || key == "ip_limit" {
			n, ok := value.(float64)
			if !ok || n != math.Trunc(n) || n < 0 || n > 86400 || key == "interval" && n < 1 {
				writeError(w, fail(400, "invalid_config", "时间间隔或注册数量无效"), "")
				return
			}
		}
		if key == "content" || key == "title" {
			text, ok := value.(string)
			if !ok || len([]rune(text)) > 4000 {
				writeError(w, fail(400, "invalid_config", "公告内容无效"), "")
				return
			}
		}
		if key == "update_url" || key == "client_url" {
			text, ok := value.(string)
			if ok && text == "" {
				continue
			}
			u, err := url.Parse(text)
			if !ok || err != nil || u.Hostname() == "" || u.User != nil || (u.Scheme != "http" && u.Scheme != "https") {
				writeError(w, fail(400, "invalid_config", "下载地址无效"), "")
				return
			}
		}
		if key == "reward" {
			reward, err := decodeReward(value)
			if err != nil {
				writeError(w, err, "")
				return
			}
			if err = validateReward(reward, 100); err != nil {
				writeError(w, err, "")
				return
			}
		}
	}
	content, _ := body.Value["content"].(string)
	if (section == "announcement.navigation" || section == "announcement.chat") && len([]rune(content)) > 400 {
		writeError(w, fail(400, "invalid_announcement", "当前玩家登录器公告最多400字"), "")
		return
	}
	if enabled, _ := body.Value["enabled"].(bool); enabled && strings.HasPrefix(section, "announcement.") && strings.TrimSpace(content) == "" {
		writeError(w, fail(400, "invalid_config", "启用公告需要内容"), "")
		return
	}
	if enabled, _ := body.Value["enabled"].(bool); enabled && strings.HasPrefix(section, "welfare.") {
		reward, err := decodeReward(body.Value["reward"])
		if err != nil || len(reward.Items) == 0 && reward.Points+reward.Tokens+reward.Gold == 0 {
			writeError(w, fail(400, "invalid_reward", "启用福利至少需要一项奖励"), "")
			return
		}
	}
	if enabled, _ := body.Value["reward_enabled"].(bool); enabled && section == "registration" {
		reward, err := decodeReward(body.Value["reward"])
		if err != nil || len(reward.Items) == 0 && reward.Points+reward.Tokens+reward.Gold == 0 {
			writeError(w, fail(400, "invalid_reward", "启用注册奖励至少需要一项奖励"), "")
			return
		}
	}
	module := "PVF管理"
	if strings.HasPrefix(section, "announcement.") {
		module = "公告管理"
	} else if strings.HasPrefix(section, "welfare.") {
		module = "在线福利"
	} else if section == "registration" {
		module = "注册管理"
	} else if section == "recharge" {
		module = "充值设置"
	}
	s.mutate(w, r, body, module, section, section, func(tx *sql.Tx) (any, error) {
		prior, err := readConfig(r.Context(), tx, section)
		if err != nil {
			return nil, err
		}
		if prior.Revision != body.Revision {
			return nil, fail(409, "revision_conflict", "配置已变化，请先刷新再保存")
		}
		entry := adminapi.ConfigEntry{Section: section, Revision: prior.Revision + 1, Value: body.Value, UpdatedAt: time.Now().UTC(), AppliedToGame: false}
		if s.config.DirectDatabase {
			if err = s.saveDirectConfig(r.Context(), tx, &entry); err != nil {
				return nil, err
			}
		}
		raw, err := json.Marshal(entry.Value)
		if err != nil {
			return nil, err
		}
		_, err = tx.ExecContext(r.Context(), "INSERT INTO manager_config(section,revision,value_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(section) DO UPDATE SET revision=excluded.revision,value_json=excluded.value_json,updated_at=excluded.updated_at", section, entry.Revision, string(raw), metadataTime(entry.UpdatedAt))
		return entry, err
	})
}
func decodeReward(value any) (adminapi.Reward, error) {
	var r adminapi.Reward
	m, ok := value.(map[string]any)
	if !ok {
		return r, fail(400, "invalid_reward", "奖励格式无效")
	}
	for k := range m {
		if k != "items" && k != "points" && k != "tokens" && k != "gold" {
			return r, fail(400, "invalid_reward", "奖励字段无效")
		}
	}
	for name, target := range map[string]*int64{"points": &r.Points, "tokens": &r.Tokens, "gold": &r.Gold} {
		if value, ok := m[name]; ok {
			number, ok := value.(float64)
			if !ok || number != math.Trunc(number) || number < 0 || number > 2147483647 {
				return r, fail(400, "invalid_reward", "奖励数量无效")
			}
			*target = int64(number)
		}
	}
	if values, exists := m["items"]; exists && values != nil {
		items, ok := values.([]any)
		if !ok {
			return r, fail(400, "invalid_reward", "物品格式无效")
		}
		for _, value := range items {
			item, ok := value.(map[string]any)
			if !ok || len(item) != 2 {
				return r, fail(400, "invalid_reward", "物品字段无效")
			}
			id, ok1 := item["id"].(float64)
			quantity, ok2 := item["quantity"].(float64)
			if !ok1 || !ok2 || id != math.Trunc(id) || quantity != math.Trunc(quantity) || id < 1 || id > math.MaxUint32 || quantity < 1 || quantity > 9999 {
				return r, fail(400, "invalid_reward", "物品ID和数量无效")
			}
			r.Items = append(r.Items, adminapi.Item{ID: int64(id), Quantity: int64(quantity)})
		}
	}
	return r, nil
}
func (s *Service) createCodes(w http.ResponseWriter, r *http.Request) {
	var body adminapi.CodeRequest
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	body.Name = strings.TrimSpace(body.Name)
	if body.Name == "" || len([]rune(body.Name)) > 100 || body.Quantity < 1 || body.Quantity > 10000 || (body.Kind != "cdk" && body.Kind != "registration") {
		writeError(w, fail(400, "invalid_codes", "名称、类型或数量无效"), "")
		return
	}
	if err := validateReward(body.Reward, 100); err != nil {
		writeError(w, err, "")
		return
	}
	if s.config.DirectDatabase && body.Kind == "cdk" && body.Reward.Tokens > 0 {
		writeError(w, fail(400, "unsupported_cdk_tokens", "当前游戏兑换协议不支持代币CDK，请通过代币或邮件页面发放"), "")
		return
	}
	if body.Kind == "cdk" && len(body.Reward.Items) == 0 && body.Reward.Points+body.Reward.Tokens+body.Reward.Gold == 0 {
		writeError(w, fail(400, "invalid_reward", "CDK至少需要一项奖励"), "")
		return
	}
	estimatedReward, _ := json.Marshal(body.Reward)
	if int64(body.Quantity)*int64(len(estimatedReward)+len(body.Name)+256) > 16<<20 {
		writeError(w, fail(400, "batch_too_large", "本批奖励方案较大，请减少制作数量或分批生成"), "")
		return
	}
	module := "CDK管理"
	if body.Kind == "registration" {
		module = "注册管理"
	}
	s.mutate(w, r, body, module, "codes.generate", body.Name, func(tx *sql.Tx) (any, error) {
		created := make([]adminapi.Code, 0, body.Quantity)
		rewardJSON, err := json.Marshal(body.Reward)
		if err != nil {
			return nil, err
		}
		statement, err := tx.PrepareContext(r.Context(), "INSERT OR IGNORE INTO manager_codes(code,name,kind,reward_json,state,created_at) VALUES(?,?,?,?,?,?)")
		if err != nil {
			return nil, err
		}
		defer statement.Close()
		for len(created) < body.Quantity {
			size := 16
			if body.Kind == "cdk" {
				size = 8
			} // Native Coupon accepts four groups of four characters.
			data := make([]byte, size)
			if _, err := rand.Read(data); err != nil {
				return nil, err
			}
			code := strings.ToUpper(hex.EncodeToString(data))
			entry := adminapi.Code{Code: code, Name: body.Name, Kind: body.Kind, Reward: body.Reward, State: "managed_not_published", CreatedAt: time.Now().UTC()}
			if s.config.DirectDatabase {
				entry.State = "active"
				if body.Kind == "registration" {
					entry.State = "unused"
				}
			}
			result, err := statement.ExecContext(r.Context(), code, body.Name, body.Kind, string(rewardJSON), entry.State, metadataTime(entry.CreatedAt))
			if err != nil {
				return nil, err
			}
			count, _ := result.RowsAffected()
			if count == 0 {
				continue
			}
			if s.config.DirectDatabase {
				if body.Kind == "registration" {
					_, err = tx.ExecContext(r.Context(), "INSERT INTO gmkit_registration_codes(code,state,created_at) VALUES(?,'unused',?)", code, gameStamp())
				} else {
					_, err = tx.ExecContext(r.Context(), "INSERT INTO gmkit_cdks(code,name,points,item_id,item_name,quantity,max_uses,used,enabled,created_at,rewards_json) VALUES(?,?,?,0,'',0,1,0,1,?,?)", code, body.Name, body.Reward.Points, gameStamp(), jsonText(nativeRewards(body.Reward)))
				}
				if err != nil {
					return nil, err
				}
			}
			created = append(created, entry)
		}
		return created, nil
	})
}
func (s *Service) codes(w http.ResponseWriter, r *http.Request) {
	if s.config.DirectDatabase {
		s.directCodes(w, r)
		return
	}
	p, size, err := pageParams(r)
	if err != nil {
		writeError(w, err, "")
		return
	}
	q := r.URL.Query()
	where, args := "1=1", []any{}
	if kind := q.Get("kind"); kind != "" {
		where += " AND kind=?"
		args = append(args, kind)
	}
	if keyword := q.Get("keyword"); keyword != "" {
		where += " AND (instr(lower(name),lower(?))>0 OR instr(lower(code),lower(?))>0)"
		args = append(args, keyword, keyword)
	}
	var total int
	if err = s.store.db.QueryRowContext(r.Context(), "SELECT count(*) FROM manager_codes WHERE "+where, args...).Scan(&total); err != nil {
		writeError(w, err, "")
		return
	}
	pageArgs := append(append([]any{}, args...), size, (p-1)*size)
	rows, err := s.store.db.QueryContext(r.Context(), "SELECT code,name,kind,reward_json,state,created_at FROM manager_codes WHERE "+where+" ORDER BY created_at DESC,code LIMIT ? OFFSET ?", pageArgs...)
	if err != nil {
		writeError(w, err, "")
		return
	}
	defer rows.Close()
	items := []adminapi.Code{}
	for rows.Next() {
		var item adminapi.Code
		var reward, created string
		if err = rows.Scan(&item.Code, &item.Name, &item.Kind, &reward, &item.State, &created); err != nil {
			writeError(w, err, "")
			return
		}
		if err = json.Unmarshal([]byte(reward), &item.Reward); err != nil {
			writeError(w, err, "")
			return
		}
		item.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
		items = append(items, item)
	}
	if err = rows.Err(); err != nil {
		writeError(w, err, "")
		return
	}
	write(w, 200, adminapi.Envelope{OK: true, Data: adminapi.Page[adminapi.Code]{Items: items, Total: total, Page: p, PageSize: size}})
}
func (s *Service) audit(w http.ResponseWriter, r *http.Request) {
	p, size, err := pageParams(r)
	if err != nil {
		writeError(w, err, "")
		return
	}
	from, to, err := queryTimes(r)
	if err != nil {
		writeError(w, err, "")
		return
	}
	q := r.URL.Query()
	where, args := "1=1", []any{}
	if !from.IsZero() {
		where += " AND at>=?"
		args = append(args, metadataTime(from))
	}
	if !to.IsZero() {
		where += " AND at<=?"
		args = append(args, metadataTime(to))
	}
	if module := q.Get("module"); module != "" {
		where += " AND module=?"
		args = append(args, module)
	}
	if keyword := q.Get("keyword"); keyword != "" {
		where += " AND instr(lower(action||target||result||detail),lower(?))>0"
		args = append(args, keyword)
	}
	var total int
	if err = s.store.db.QueryRowContext(r.Context(), "SELECT count(*) FROM manager_audit WHERE "+where, args...).Scan(&total); err != nil {
		writeError(w, err, "")
		return
	}
	pageArgs := append(append([]any{}, args...), size, (p-1)*size)
	rows, err := s.store.db.QueryContext(r.Context(), "SELECT id,at,actor,module,action,target,result,request_id,detail FROM manager_audit WHERE "+where+" ORDER BY id DESC LIMIT ? OFFSET ?", pageArgs...)
	if err != nil {
		writeError(w, err, "")
		return
	}
	defer rows.Close()
	items := []adminapi.Audit{}
	for rows.Next() {
		var entry adminapi.Audit
		var at string
		if err = rows.Scan(&entry.ID, &at, &entry.Actor, &entry.Module, &entry.Action, &entry.Target, &entry.Result, &entry.RequestID, &entry.Detail); err != nil {
			writeError(w, err, "")
			return
		}
		entry.At, _ = time.Parse(time.RFC3339Nano, at)
		items = append(items, entry)
	}
	if err = rows.Err(); err != nil {
		writeError(w, err, "")
		return
	}
	write(w, 200, adminapi.Envelope{OK: true, Data: adminapi.Page[adminapi.Audit]{Items: items, Total: total, Page: p, PageSize: size}})
}
func (s *Service) receipt(w http.ResponseWriter, r *http.Request) {
	entry, ok, err := readRequest(r.Context(), s.store.db, r.PathValue("id"))
	if err != nil {
		writeError(w, err, r.PathValue("id"))
		return
	}
	if !ok {
		writeError(w, fail(404, "not_found", "没有此请求记录"), r.PathValue("id"))
		return
	}
	write(w, entry.Status, entry.Reply)
}
func queryTimes(r *http.Request) (time.Time, time.Time, error) {
	parse := func(key string, end bool) (time.Time, error) {
		v := r.URL.Query().Get(key)
		if v == "" {
			return time.Time{}, nil
		}
		for _, layout := range []string{time.RFC3339, "2006-01-02 15:04:05", "2006-01-02"} {
			t, err := time.ParseInLocation(layout, v, time.FixedZone("Asia/Shanghai", 8*3600))
			if err == nil {
				if layout == "2006-01-02" && end {
					t = t.AddDate(0, 0, 1).Add(-time.Nanosecond)
				}
				return t, nil
			}
		}
		return time.Time{}, fail(400, "invalid_time", "日期格式无效")
	}
	from, err := parse("from", false)
	if err != nil {
		return from, time.Time{}, err
	}
	to, err := parse("to", true)
	if err == nil && !from.IsZero() && !to.IsZero() && from.After(to) {
		err = fail(400, "invalid_time", "开始时间晚于结束时间")
	}
	return from, to, err
}
