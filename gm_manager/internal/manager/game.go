package manager

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"io"
	"math"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/text/encoding/simplifiedchinese"
	_ "modernc.org/sqlite"
	"newadmin/internal/adminapi"
)

type gameConnection struct {
	AccountID   int    `json:"account_id"`
	CharacterID int    `json:"character_id"`
	Epoch       int64  `json:"session_epoch"`
	RemoteIP    string `json:"remote_ip"`
}
type gameSession struct {
	Complete    bool             `json:"all_connections_complete"`
	Connections []gameConnection `json:"connections"`
}
type gameControl struct {
	endpointFile string
	client       *http.Client
}

func (g *gameControl) request(ctx context.Context, method, path string, body any, out any) error {
	if g.endpointFile == "" {
		return fail(503, "game_unconfigured", "尚未配置游戏控制接口")
	}
	info, err := os.Stat(g.endpointFile)
	if err != nil || info.Size() > 16384 {
		return fail(503, "game_unavailable", "游戏控制接口文件不可用")
	}
	raw, err := os.ReadFile(g.endpointFile)
	if err != nil {
		return fail(503, "game_unavailable", "游戏控制接口文件不可读")
	}
	var endpoint struct {
		Version int    `json:"api_version"`
		Host    string `json:"host"`
		Port    int    `json:"port"`
		Token   string `json:"token"`
	}
	if json.Unmarshal(raw, &endpoint) != nil || endpoint.Version != 1 || endpoint.Port < 1 || endpoint.Port > 65535 || endpoint.Token == "" || (endpoint.Host != "127.0.0.1" && endpoint.Host != "0.0.0.0" && endpoint.Host != "localhost") {
		return fail(503, "game_endpoint_invalid", "游戏控制接口配置无效")
	}
	var data []byte
	if body != nil {
		data, err = json.Marshal(body)
		if err != nil {
			return err
		}
	}
	req, err := http.NewRequestWithContext(ctx, method, "http://127.0.0.1:"+strconv.Itoa(endpoint.Port)+path, bytes.NewReader(data))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+endpoint.Token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := g.client.Do(req)
	if err != nil {
		return &apiFailure{Status: 503, Code: "game_unavailable", Message: "游戏接口未返回确认", Unknown: method != "GET"}
	}
	defer resp.Body.Close()
	raw, err = io.ReadAll(io.LimitReader(resp.Body, 2<<20+1))
	if err != nil || len(raw) > 2<<20 {
		return &apiFailure{Status: 502, Code: "game_response_invalid", Message: "游戏响应不完整", Unknown: method != "GET"}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return &apiFailure{Status: resp.StatusCode, Code: "game_rejected", Message: "游戏接口拒绝请求，请按请求编号核对", Unknown: method != "GET" && resp.StatusCode >= 500}
	}
	if err = json.Unmarshal(raw, out); err != nil {
		return &apiFailure{Status: 502, Code: "game_response_invalid", Message: "游戏响应格式无效", Unknown: method != "GET"}
	}
	return nil
}
func (g *gameControl) session(ctx context.Context) (gameSession, error) {
	var response struct {
		OK      bool        `json:"ok"`
		Session gameSession `json:"session"`
	}
	err := g.request(ctx, "GET", "/api/v1/session", nil, &response)
	if err == nil && !response.OK {
		err = fail(503, "game_unavailable", "游戏连接快照不可用")
	}
	return response.Session, err
}

type gameReader struct {
	database string
	game     *gameControl
}

func (g *gameReader) open() (*sql.DB, error) {
	if g.database == "" {
		return nil, fail(503, "database_unconfigured", "尚未配置真实游戏数据库")
	}
	info, err := os.Stat(g.database)
	if err != nil || info.IsDir() {
		return nil, fail(503, "database_unavailable", "游戏数据库不存在，未创建替代数据库")
	}
	path, err := filepath.Abs(g.database)
	if err != nil {
		return nil, err
	}
	uriPath := filepath.ToSlash(path)
	if !strings.HasPrefix(uriPath, "/") {
		uriPath = "/" + uriPath
	}
	u := url.URL{Scheme: "file", Path: uriPath}
	q := u.Query()
	q.Set("mode", "ro")
	q.Add("_pragma", "query_only(1)")
	q.Add("_pragma", "busy_timeout(1500)")
	u.RawQuery = q.Encode()
	db, err := sql.Open("sqlite", u.String())
	if err != nil {
		return nil, fail(503, "database_unavailable", "只读数据库连接失败")
	}
	db.SetMaxOpenConns(1)
	return db, nil
}
func sqlColumns(ctx context.Context, db *sql.DB, table string) map[string]bool {
	rows, err := db.QueryContext(ctx, "PRAGMA table_info(\""+table+"\")")
	if err != nil {
		return nil
	}
	defer rows.Close()
	columns := map[string]bool{}
	for rows.Next() {
		var cid, notnull, pk int
		var name, kind string
		var def any
		if rows.Scan(&cid, &name, &kind, &notnull, &def, &pk) == nil {
			columns[name] = true
		}
	}
	return columns
}
func dbText(raw []byte) string {
	if utf8.Valid(raw) {
		return string(raw)
	}
	decoded, err := simplifiedchinese.GB18030.NewDecoder().Bytes(raw)
	if err == nil {
		return string(decoded)
	}
	return strings.ToValidUTF8(string(raw), "�")
}
func (g *gameReader) read(ctx context.Context) ([]adminapi.Account, []adminapi.Character, error) {
	db, err := g.open()
	if err != nil {
		return nil, nil, err
	}
	defer db.Close()
	cols := sqlColumns(ctx, db, "accounts")
	for _, name := range []string{"account_id", "m_id", "cera", "token_cera"} {
		if !cols[name] {
			return nil, nil, fail(503, "schema_mismatch", "账号表与已核对结构不一致")
		}
	}
	optional := func(column string) string {
		if cols[column] {
			return "COALESCE(" + column + ",'')"
		}
		return "''"
	}
	rows, err := db.QueryContext(ctx, "SELECT account_id,m_id,cera,token_cera,"+optional("last_login_ip")+","+optional("last_login_at")+" FROM accounts ORDER BY account_id")
	if err != nil {
		return nil, nil, fail(503, "database_query_failed", "账号查询失败")
	}
	accounts := []adminapi.Account{}
	accountIndex := map[int]int{}
	for rows.Next() {
		var row adminapi.Account
		var name []byte
		if err = rows.Scan(&row.ID, &name, &row.Points, &row.Tokens, &row.LoginIP, &row.LastLogin); err != nil {
			rows.Close()
			return nil, nil, fail(503, "schema_mismatch", "账号数据结构不匹配")
		}
		row.Name = dbText(name)
		row.OnlineStatus = "unknown"
		accountIndex[row.ID] = len(accounts)
		accounts = append(accounts, row)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, nil, err
	}
	roles, err := db.QueryContext(ctx, `SELECT c.character_id,c.account_id,CAST(c.name AS BLOB),c.level,c.job,w.gold FROM characters c LEFT JOIN character_wallets w ON w.character_id=c.character_id WHERE c.delete_flag=0 ORDER BY c.account_id,c.character_id`)
	if err != nil {
		return nil, nil, fail(503, "schema_mismatch", "角色或角色钱包表结构不匹配")
	}
	characters := []adminapi.Character{}
	for roles.Next() {
		var row adminapi.Character
		var name []byte
		var job int
		var gold sql.NullInt64
		if err = roles.Scan(&row.ID, &row.AccountID, &name, &row.Level, &job, &gold); err != nil {
			roles.Close()
			return nil, nil, err
		}
		row.Name = dbText(name)
		row.Job = strconv.Itoa(job)
		row.OnlineStatus = "unknown"
		if gold.Valid {
			value := gold.Int64
			row.Gold = &value
		}
		if index, ok := accountIndex[row.AccountID]; ok {
			row.AccountName = accounts[index].Name
			accounts[index].CharacterCount++
		}
		characters = append(characters, row)
	}
	err = roles.Err()
	roles.Close()
	if err != nil {
		return nil, nil, err
	}
	liveIPs := map[int]string{}
	// Missing or incomplete connection snapshots remain explicitly unknown.
	if g.game != nil {
		session, e := g.game.session(ctx)
		if e == nil && session.Complete {
			onlineA, onlineC := map[int]bool{}, map[int]bool{}
			for _, c := range session.Connections {
				onlineA[c.AccountID] = true
				onlineC[c.CharacterID] = true
				if c.RemoteIP != "" {
					liveIPs[c.AccountID] = c.RemoteIP
				}
			}
			for i := range accounts {
				accounts[i].OnlineStatus = "offline"
				if onlineA[accounts[i].ID] {
					accounts[i].OnlineStatus = "online"
				}
			}
			for i := range characters {
				characters[i].OnlineStatus = "offline"
				if onlineC[characters[i].ID] {
					characters[i].OnlineStatus = "online"
				}
			}
		}
	}
	if err = g.readBansAndIdentity(ctx, db, accounts, liveIPs); err != nil {
		return nil, nil, err
	}
	return accounts, characters, nil
}
func (s *Service) accounts(w http.ResponseWriter, r *http.Request) {
	p, size, err := pageParams(r)
	if err != nil {
		writeError(w, err, "")
		return
	}
	accounts, _, err := s.reader.read(r.Context())
	if err != nil {
		writeError(w, err, "")
		return
	}
	q := r.URL.Query()
	scope := q.Get("scope")
	if scope != "" && scope != "all" && scope != "online" && scope != "banned" {
		writeError(w, fail(400, "invalid_scope", "账号范围无效"), "")
		return
	}
	filtered := []adminapi.Account{}
	for _, row := range accounts {
		if scope == "banned" && !row.BanKnown {
			writeError(w, fail(503, "ban_state_unknown", "游戏数据库缺少已核对的封禁记录表"), "")
			return
		}
		if scope == "banned" && !row.Banned {
			continue
		}
		if scope == "online" && row.OnlineStatus == "unknown" {
			writeError(w, fail(503, "online_unknown", "当前有效游戏连接快照不可用"), "")
			return
		}
		if scope == "online" && row.OnlineStatus != "online" {
			continue
		}
		if keywordMatch(q.Get("keyword"), row.Name, strconv.Itoa(row.ID)) {
			filtered = append(filtered, row)
		}
	}
	page := paginate(filtered, p, size)
	page.Source = "game_database_read_only"
	write(w, 200, adminapi.Envelope{OK: true, Data: page})
}
func (s *Service) characters(w http.ResponseWriter, r *http.Request) {
	p, size, err := pageParams(r)
	if err != nil {
		writeError(w, err, "")
		return
	}
	_, characters, err := s.reader.read(r.Context())
	if err != nil {
		writeError(w, err, "")
		return
	}
	q := r.URL.Query()
	scope := q.Get("scope")
	if scope != "" && scope != "all" && scope != "online" {
		writeError(w, fail(400, "invalid_scope", "角色范围无效"), "")
		return
	}
	filtered := []adminapi.Character{}
	for _, row := range characters {
		if scope == "online" && row.OnlineStatus == "unknown" {
			writeError(w, fail(503, "online_unknown", "当前有效游戏连接快照不可用"), "")
			return
		}
		if scope == "online" && row.OnlineStatus != "online" {
			continue
		}
		if owner := q.Get("account"); owner != "" && !strings.EqualFold(owner, row.AccountName) && owner != strconv.Itoa(row.AccountID) {
			continue
		}
		if keywordMatch(q.Get("keyword"), row.Name, strconv.Itoa(row.ID)) {
			filtered = append(filtered, row)
		}
	}
	page := paginate(filtered, p, size)
	page.Source = "character_wallets_read_only"
	write(w, 200, adminapi.Envelope{OK: true, Data: page})
}
func (s *Service) action(w http.ResponseWriter, r *http.Request) {
	var body adminapi.Action
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	id := r.Header.Get("Idempotency-Key")
	if len(id) < 1 || len(id) > 128 {
		writeError(w, fail(400, "request_id_required", "操作必须带请求编号"), id)
		return
	}
	if !body.Confirm {
		writeError(w, fail(400, "confirmation_required", "操作必须明确确认"), id)
		return
	}
	if s.config.DirectDatabase {
		s.directAction(w, r, body)
		return
	}
	if body.Name != "currency.credit" {
		writeError(w, fail(501, "action_adapter_pending", "此业务尚未完成Go适配，没有执行操作"), id)
		return
	}
	if body.Scope != "" && body.Scope != "single" {
		writeError(w, fail(501, "batch_pending", "批量业务适配尚未完成，未按单个目标执行"), id)
		return
	}
	if body.CharacterID < 1 || body.Amount < 1 || body.Amount > 999999999 || (body.Currency != "gold" && body.Currency != "cera") {
		writeError(w, fail(400, "invalid_credit", "必须指定角色、支持的币种和合法数量"), id)
		return
	}
	if body.Currency == "cera" && body.Amount > 99999999 {
		writeError(w, fail(400, "invalid_credit", "点券数量超过上限"), id)
		return
	}
	raw, _ := json.Marshal(body)
	sum := sha256.Sum256(raw)
	fingerprint := hex.EncodeToString(sum[:])
	s.requestMu.Lock()
	if s.active[id] {
		s.requestMu.Unlock()
		writeError(w, fail(409, "request_in_progress", "该请求仍在处理，请查询原回执"), id)
		return
	}
	s.active[id] = true
	s.requestMu.Unlock()
	defer func() { s.requestMu.Lock(); delete(s.active, id); s.requestMu.Unlock() }()
	prior, exists, err := readRequest(r.Context(), s.store.db, id)
	if err != nil {
		writeError(w, err, id)
		return
	}
	if exists {
		if prior.Fingerprint != fingerprint {
			writeError(w, fail(409, "request_conflict", "请求编号已用于其他内容"), id)
		} else {
			write(w, prior.Status, prior.Reply)
		}
		return
	}
	session, err := s.game.session(r.Context())
	if err != nil {
		writeError(w, err, id)
		return
	}
	if !session.Complete {
		writeError(w, fail(503, "online_unknown", "有效连接快照不完整"), id)
		return
	}
	var target *gameConnection
	for _, row := range session.Connections {
		if row.CharacterID == body.CharacterID {
			copy := row
			target = &copy
			break
		}
	}
	if target == nil || target.Epoch <= 0 {
		writeError(w, fail(409, "target_not_online", "目标角色没有有效在线会话；离线写入尚未适配"), id)
		return
	}
	if body.AccountID > 0 && body.AccountID != target.AccountID {
		writeError(w, fail(409, "target_mismatch", "角色不属于所选账号"), id)
		return
	}
	pending := adminapi.Envelope{OK: false, Error: &adminapi.Error{Code: "result_unknown", Message: "请求已登记，需核对游戏回执；不得自动重发", Unknown: true}, RequestID: id}
	if err = s.store.transaction(r.Context(), func(tx *sql.Tx) error {
		_, exists, err := readRequest(r.Context(), tx, id)
		if err != nil {
			return err
		}
		if exists {
			return fail(409, "request_conflict", "请求编号已被其他请求登记")
		}
		if err = saveRequest(r.Context(), tx, id, savedRequest{Fingerprint: fingerprint, Status: 409, Reply: pending}, false); err != nil {
			return err
		}
		return appendAudit(r.Context(), tx, "账号管理", body.Name, strconv.Itoa(body.CharacterID), "已登记待确认", id, body.Currency)
	}); err != nil {
		writeError(w, fail(503, "receipt_unavailable", "无法持久保存请求记录，未发送操作"), id)
		return
	}
	var response struct {
		OK        bool           `json:"ok"`
		RequestID string         `json:"request_id"`
		Result    map[string]any `json:"result"`
	}
	command := "currency.add_gold"
	if body.Currency == "cera" {
		command = "currency.add_cera"
	}
	err = s.game.request(r.Context(), "POST", "/api/v1/command", map[string]any{"request_id": id, "command": command, "params": map[string]any{"amount": body.Amount}, "expected_character_id": body.CharacterID, "expected_session_epoch": target.Epoch}, &response)
	reply, status := pending, 409
	if err != nil {
		if failure, ok := err.(*apiFailure); ok && !failure.Unknown && failure.Status >= 400 && failure.Status < 500 {
			status = failure.Status
			reply = adminapi.Envelope{OK: false, RequestID: id, Error: &adminapi.Error{Code: failure.Code, Message: failure.Message}}
		}
	} else {
		cid, _ := response.Result["character_id"].(float64)
		amount, _ := response.Result["amount"].(float64)
		balance, known := response.Result["balance"].(float64)
		accountMatches := true
		if body.Currency == "cera" {
			aid, ok := response.Result["account_id"].(float64)
			accountMatches = ok && int(aid) == target.AccountID
		}
		limit := float64(999999999)
		if body.Currency == "cera" {
			limit = 99999999
		}
		if response.OK && response.RequestID == id && int(cid) == body.CharacterID && int64(amount) == body.Amount && known && balance >= float64(body.Amount) && balance == math.Trunc(balance) && balance <= limit && accountMatches {
			status = 200
			reply = adminapi.Envelope{OK: true, RequestID: id, Data: adminapi.Receipt{RequestID: id, Action: body.Name, State: "confirmed", Message: "游戏服务端已确认提交", Result: response.Result, AppliedToGame: true, CreatedAt: time.Now().UTC()}}
		}
	}
	if err = s.store.transaction(context.Background(), func(tx *sql.Tx) error {
		if err := saveRequest(context.Background(), tx, id, savedRequest{Fingerprint: fingerprint, Status: status, Reply: reply}, true); err != nil {
			return err
		}
		result := "结果未确认"
		if status == 200 {
			result = "游戏已确认"
		}
		return appendAudit(context.Background(), tx, "账号管理", body.Name, strconv.Itoa(body.CharacterID), result, id, body.Currency)
	}); err != nil {
		write(w, 409, pending)
		return
	}
	write(w, status, reply)
}
