package manager

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"modernc.org/sqlite"
	"newadmin/internal/adminapi"
)

const recordStampLayout = "2006-01-02T15:04:05.000000000Z"

func recordSQLText(value driver.Value) string {
	switch v := value.(type) {
	case nil:
		return ""
	case []byte:
		return dbText(v)
	case string:
		return dbText([]byte(v))
	default:
		return fmt.Sprint(v)
	}
}
func init() {
	sqlite.MustRegisterDeterministicScalarFunction("gm_record_text_v1", 1, func(_ *sqlite.FunctionContext, args []driver.Value) (driver.Value, error) {
		return recordSQLText(args[0]), nil
	})
	sqlite.MustRegisterDeterministicScalarFunction("gm_record_stamp_v1", 1, func(_ *sqlite.FunctionContext, args []driver.Value) (driver.Value, error) {
		at, ok := recordTime(recordSQLText(args[0]))
		if !ok {
			return nil, fmt.Errorf("GM_RECORD_TIME_INVALID")
		}
		return at.UTC().Format(recordStampLayout), nil
	})
	sqlite.MustRegisterDeterministicScalarFunction("gm_record_match_v1", 3, func(_ *sqlite.FunctionContext, args []driver.Value) (driver.Value, error) {
		if keywordMatch(recordSQLText(args[0]), recordSQLText(args[1]), recordSQLText(args[2])) {
			return int64(1), nil
		}
		return int64(0), nil
	})
}

type recordSQLSource struct{ query, note string }

var recordSQLFields = []string{"stamp", "kind", "account", "character", "direction", "content", "place", "result", "name", "code", "points", "item_id", "quantity", "reward_json", "clear_ms", "source_order", "sort_key", "variant"}

func recordSource(from, note string, order int, fields map[string]string) recordSQLSource {
	defaults := map[string]string{"stamp": "''", "kind": "''", "account": "''", "character": "''", "direction": "''", "content": "''", "place": "''", "result": "''", "name": "''", "code": "''", "points": "0", "item_id": "0", "quantity": "0", "reward_json": "''", "clear_ms": "0", "source_order": fmt.Sprint(order), "sort_key": "''", "variant": "0"}
	for k, v := range fields {
		defaults[k] = v
	}
	columns := make([]string, 0, len(recordSQLFields))
	for _, name := range recordSQLFields {
		columns = append(columns, defaults[name]+" AS "+name)
	}
	return recordSQLSource{query: "SELECT " + strings.Join(columns, ",") + " FROM " + from, note: note}
}
func recordText(expression string) string { return "gm_record_text_v1(" + expression + ")" }
func (s *Service) recordSources(ctx context.Context, db *sql.DB, kind string) ([]recordSQLSource, error) {
	var sources []recordSQLSource
	has := func(table, column string) bool { return sqlColumns(ctx, db, table)[column] }
	account, character := recordText("a.m_id"), recordText("c.name")
	extras := []struct {
		kind, table string
		order       int
	}{{"best", "character_dungeon_best_times", 0}, {"raid", "gmkit_raid_ledger", 1}, {"item", "item_audit_log", 2}, {"revive", "account_revive_coin_ledger", 3}}
	for _, extra := range extras {
		if kind != extra.kind && kind != "all" {
			continue
		}
		if len(sqlColumns(ctx, db, extra.table)) == 0 {
			continue
		}
		fields := map[string]string{"stamp": "r.created_at", "account": account, "character": character}
		var from string
		switch extra.kind {
		case "best":
			from = "character_dungeon_best_times r JOIN characters c ON c.character_id=r.character_id JOIN accounts a ON a.account_id=c.account_id"
			fields["stamp"] = "r.updated_at"
			fields["kind"] = "'副本最佳成绩'"
			fields["place"] = "printf('副本ID %d / 难度 %d',r.dungeon_id,r.difficulty)"
			fields["content"] = "printf('%d毫秒',r.best_time_ms)"
			fields["result"] = "'当前最佳成绩，非完整通关历史'"
			fields["clear_ms"] = "r.best_time_ms"
			fields["sort_key"] = "printf('%020d:%020d:%020d',r.character_id,r.dungeon_id,r.difficulty)"
		case "raid":
			from = "gmkit_raid_ledger r LEFT JOIN characters c ON c.character_id=r.character_id LEFT JOIN accounts a ON a.account_id=r.account_id"
			fields["kind"] = "'团本凭据'"
			fields["place"] = recordText("r.family") + "||' / '||" + recordText("r.instance_id")
			fields["content"] = recordText("r.event_name") + "||'；'||" + recordText("r.payload_json")
			fields["result"] = "'原业务已提交凭据'"
			fields["sort_key"] = "printf('%020d',r.id)"
		case "item":
			from = "item_audit_log r LEFT JOIN characters c ON c.character_id=r.character_id LEFT JOIN accounts a ON a.account_id=CASE WHEN r.owner_scope='account' THEN r.owner_id ELSE c.account_id END"
			fields["kind"] = "'物品变动'"
			fields["direction"] = "CASE WHEN r.delta_stack_count<0 THEN '支出' ELSE '收入' END"
			fields["content"] = "printf('%s；物品%d，数量%+d；%s'," + recordText("r.action_name") + ",r.item_template_id,r.delta_stack_count," + recordText("r.payload_json") + ")"
			fields["result"] = "'原业务记录'"
			fields["sort_key"] = "printf('%020d',r.audit_id)"
		case "revive":
			from = "account_revive_coin_ledger r LEFT JOIN characters c ON c.character_id=r.character_id LEFT JOIN accounts a ON a.account_id=r.account_id"
			fields["kind"] = "'复活币流水'"
			fields["direction"] = "CASE WHEN r.delta<0 THEN '支出' ELSE '收入' END"
			fields["content"] = "printf('%s；%+d（%d → %d）'," + recordText("r.reason") + ",r.delta,r.balance_before,r.balance_after)"
			fields["result"] = "'已提交'"
			fields["sort_key"] = "printf('%020d',r.ledger_id)"
		}
		sources = append(sources, recordSource(from, extra.table, extra.order, fields))
	}
	if kind == "all" || kind == "mail" {
		if has("gmkit_mails", "id") {
			sources = append(sources, recordSource("gmkit_mails m LEFT JOIN characters c ON c.character_id=m.character_id LEFT JOIN accounts a ON a.account_id=c.account_id", "gmkit_mails（当前邮件状态）", 4, map[string]string{"stamp": "m.created_at", "kind": "'邮件收发'", "account": account, "character": character, "direction": "'发送'", "content": recordText("m.title") + "||'：'||" + recordText("m.body"), "result": "'邮件记录状态：'||" + recordText("m.status"), "sort_key": "printf('%020d',m.id)"}))
		} else if kind == "mail" {
			return nil, fail(501, "record_not_collected", "游戏库没有邮件记录表")
		}
	}
	if kind == "all" || kind == "cera" || kind == "token" {
		if has("gmkit_cera_ledger", "id") {
			for variant, currency := range []string{"cera", "token"} {
				if kind != "all" && kind != currency {
					continue
				}
				title, before, after, delta := "点卷进出", "r.cera_before", "r.cera_after", "r.cera_delta"
				if currency == "token" {
					title, before, after, delta = "代币进出", "r.token_before", "r.token_after", "r.token_delta"
				}
				from := "gmkit_cera_ledger r LEFT JOIN accounts a ON a.account_id=r.account_id WHERE " + delta + "!=0"
				sources = append(sources, recordSource(from, "gmkit_cera_ledger（实际余额变动）", 5, map[string]string{"stamp": "r.created_at", "kind": "'" + title + "'", "account": account, "direction": "CASE WHEN " + delta + "<0 THEN '支出' ELSE '收入' END", "content": "printf('%+d（%d → %d）'," + delta + "," + before + "," + after + ")", "result": "'已提交；角色与消费原因未知'", "sort_key": "printf('%020d',r.id)", "variant": fmt.Sprint(variant)}))
			}
		} else if kind == "cera" || kind == "token" {
			return nil, fail(501, "record_not_collected", "游戏库未采集币种余额流水")
		}
	}
	if kind == "all" || kind == "gold" {
		if len(sqlColumns(ctx, db, "manager_gold_ledger")) > 0 {
			sources = append(sources, recordSource("manager_gold_ledger r LEFT JOIN accounts a ON a.account_id=r.account_id LEFT JOIN characters c ON c.character_id=r.character_id", "角色钱包余额流水（采集启用后）", 6, map[string]string{"stamp": "r.created_at", "kind": "'金币进出'", "account": account, "character": character, "direction": "CASE WHEN r.delta<0 THEN '支出' ELSE '收入' END", "content": "printf('%+d（%d → %d）',r.delta,r.balance_before,r.balance_after)", "result": "'实际角色钱包余额变动；场景未知'", "sort_key": "printf('%020d',r.id)"}))
		}
		if has("gmkit_value_credits", "request_id") {
			from := "gmkit_value_credits r LEFT JOIN accounts a ON a.account_id=r.account_id LEFT JOIN characters c ON c.character_id=r.character_id WHERE r.currency='gold'"
			if len(sqlColumns(ctx, db, "manager_record_coverage")) > 0 {
				from += " AND r.rowid<=(SELECT baseline_id FROM manager_record_coverage WHERE kind='gold')"
			}
			sources = append(sources, recordSource(from, "gmkit_value_credits（仅GM追加，非全部金币收支）", 7, map[string]string{"stamp": "r.created_at", "kind": "'金币进出'", "account": account, "character": character, "direction": "'收入'", "content": "printf('GM追加 %+d（%d → %d）',r.amount,r.balance_before,r.balance_after)", "result": "'仅GM追加凭据'", "sort_key": "printf('%020d',r.rowid)"}))
		} else if kind == "gold" {
			return nil, fail(501, "record_not_collected", "游戏库没有可核对的金币追加凭据")
		}
	}
	if kind == "all" || kind == "cdk" {
		if !has("gmkit_redemptions", "id") {
			return nil, fail(501, "record_not_collected", "游戏库没有CDK兑换记录")
		}
		rewards := "''"
		if has("gmkit_cdks", "rewards_json") {
			rewards = "COALESCE(d.rewards_json,'')"
		}
		sources = append(sources, recordSource("gmkit_redemptions r LEFT JOIN accounts a ON a.account_id=r.account_id LEFT JOIN characters c ON c.character_id=r.character_id JOIN gmkit_cdks d ON d.id=r.cdk_id", "gmkit_redemptions", 8, map[string]string{"stamp": "r.created_at", "kind": "'CDK兑换'", "account": account, "character": character, "name": recordText("d.name"), "code": recordText("d.code"), "points": "d.points", "item_id": "d.item_id", "quantity": "d.quantity", "reward_json": rewards, "content": recordText("d.name") + "||' / '||" + recordText("d.code"), "result": "'兑换凭据存在；显示奖励方案，分项发放状态需另核对'", "sort_key": "printf('%020d',r.id)"}))
	}
	if len(sources) == 0 {
		return nil, fail(501, "record_not_collected", "没有可核对的记录来源")
	}
	return sources, nil
}

func recordQueryError(err error) error {
	if strings.Contains(err.Error(), "GM_RECORD_TIME_INVALID") {
		return fail(503, "record_time_invalid", "记录含无效时间，未返回可能误导的结果")
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return fail(503, "record_query_timeout", "记录查询超时或已取消，请缩小筛选范围后重试")
	}
	return fail(503, "record_schema_mismatch", "记录来源结构不匹配或读取失败")
}
func (s *Service) records(w http.ResponseWriter, r *http.Request) {
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
	kind := r.URL.Query().Get("kind")
	if kind == "" {
		kind = "all"
	}
	if kind == "chat" || kind == "scene" || kind == "dungeon" {
		writeError(w, fail(501, "record_not_collected", "现有游戏库未证明采集了此类完整记录，不能回溯或使用示例替代"), "")
		return
	}
	switch kind {
	case "all", "mail", "cera", "token", "gold", "cdk", "best", "raid", "item", "revive":
	default:
		writeError(w, fail(400, "unknown_record", "记录类型无效"), "")
		return
	}
	db, err := s.reader.open()
	if err != nil {
		writeError(w, err, "")
		return
	}
	defer db.Close()
	sources, err := s.recordSources(r.Context(), db, kind)
	if err != nil {
		writeError(w, err, "")
		return
	}
	parts := make([]string, 0, len(sources))
	notes := []string{}
	seen := map[string]bool{}
	for _, source := range sources {
		parts = append(parts, source.query)
		if !seen[source.note] {
			notes = append(notes, source.note)
			seen[source.note] = true
		}
	}
	where := []string{"1=1"}
	args := []any{}
	q := r.URL.Query()
	if !from.IsZero() {
		where = append(where, "gm_record_stamp_v1(stamp)>=?")
		args = append(args, from.UTC().Format(recordStampLayout))
	}
	if !to.IsZero() {
		where = append(where, "gm_record_stamp_v1(stamp)<=?")
		args = append(args, to.UTC().Format(recordStampLayout))
	}
	if direction := q.Get("direction"); direction != "" {
		where = append(where, "direction=?")
		args = append(args, direction)
	}
	if account := q.Get("account"); strings.TrimSpace(account) != "" {
		where = append(where, "gm_record_match_v1(?,account,character)=1")
		args = append(args, account)
	}
	if keyword := q.Get("keyword"); strings.TrimSpace(keyword) != "" {
		where = append(where, "gm_record_match_v1(?,content,place)=1")
		args = append(args, keyword)
	}
	cte := "WITH records AS (" + strings.Join(parts, " UNION ALL ") + "),filtered AS (SELECT * FROM records WHERE " + strings.Join(where, " AND ") + ") "
	// The reader connection is opened with mode=ro. One snapshot keeps count and page consistent.
	tx, err := db.BeginTx(r.Context(), nil)
	if err != nil {
		writeError(w, recordQueryError(err), "")
		return
	}
	defer tx.Rollback()
	var total int
	if err = tx.QueryRowContext(r.Context(), cte+"SELECT COUNT(*) FROM filtered", args...).Scan(&total); err != nil {
		writeError(w, recordQueryError(err), "")
		return
	}
	pageArgs := append(append([]any{}, args...), size, int64(p-1)*int64(size))
	query := cte + "SELECT stamp,kind,account,character,direction,content,place,result,name,code,points,item_id,quantity,reward_json,clear_ms FROM filtered ORDER BY gm_record_stamp_v1(stamp) DESC,source_order ASC,sort_key DESC,variant ASC LIMIT ? OFFSET ?"
	rows, err := tx.QueryContext(r.Context(), query, pageArgs...)
	if err != nil {
		writeError(w, recordQueryError(err), "")
		return
	}
	items := []adminapi.Record{}
	for rows.Next() {
		var record adminapi.Record
		var stamp, rewardJSON string
		var points, itemID, quantity int64
		if err = rows.Scan(&stamp, &record.Kind, &record.Account, &record.Character, &record.Direction, &record.Content, &record.Place, &record.Result, &record.Name, &record.Code, &points, &itemID, &quantity, &rewardJSON, &record.ClearMS); err != nil {
			break
		}
		var ok bool
		record.At, ok = recordTime(stamp)
		if !ok {
			err = fmt.Errorf("GM_RECORD_TIME_INVALID")
			break
		}
		if record.Kind == "CDK兑换" {
			record.Reward = recordCDKReward(points, itemID, quantity, rewardJSON)
		}
		items = append(items, record)
	}
	if err == nil {
		err = rows.Err()
	}
	rows.Close()
	if err != nil {
		writeError(w, recordQueryError(err), "")
		return
	}
	page := adminapi.Page[adminapi.Record]{Items: items, Total: total, Page: p, PageSize: size, Source: strings.Join(notes, "；") + "；筛选后分页，无历史条数截断；仅覆盖已有记录"}
	write(w, 200, adminapi.Envelope{OK: true, Data: page})
}

func recordCDKReward(points, itemID, quantity int64, rewardJSON string) *adminapi.Reward {
	reward := &adminapi.Reward{Points: points}
	if itemID > 0 && quantity > 0 {
		reward.Items = []adminapi.Item{{ID: itemID, Quantity: quantity}}
	}
	if rewardJSON == "" || rewardJSON == "[]" {
		return reward
	}
	var native []struct {
		Kind     string `json:"kind"`
		ItemID   int64  `json:"itemId"`
		Quantity int64  `json:"quantity"`
		Amount   int64  `json:"amount"`
	}
	if json.Unmarshal([]byte(rewardJSON), &native) != nil {
		return nil
	}
	reward = &adminapi.Reward{}
	for _, entry := range native {
		switch entry.Kind {
		case "points":
			reward.Points += entry.Amount
		case "gold":
			reward.Gold += entry.Amount
		case "item":
			reward.Items = append(reward.Items, adminapi.Item{ID: entry.ItemID, Quantity: entry.Quantity})
		default:
			return nil
		}
	}
	return reward
}
