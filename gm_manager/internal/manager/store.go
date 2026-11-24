package manager

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"newadmin/internal/adminapi"
)

type savedRequest struct {
	Fingerprint string
	Status      int
	Reply       adminapi.Envelope
}
type stateStore struct {
	db   *sql.DB
	path string
}

const storedTime = "2006-01-02T15:04:05.000000000Z"

func metadataTime(t time.Time) string { return t.UTC().Format(storedTime) }
func openStore(directory string) (*stateStore, error) {
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, err
	}
	path := filepath.Join(directory, "management.db")
	return openStoreFile(path, false)
}

func openDirectStore(path string) (*stateStore, error) {
	if path == "" || !filepath.IsAbs(path) {
		return nil, fmt.Errorf("必须指定正式游戏数据库绝对路径")
	}
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() {
		return nil, fmt.Errorf("正式游戏数据库不存在或路径无效，未创建替代库")
	}
	return openStoreFile(path, true)
}

func openStoreFile(path string, direct bool) (*stateStore, error) {
	if info, err := os.Lstat(path); err == nil && !info.Mode().IsRegular() {
		return nil, fmt.Errorf("管理数据库路径必须是独立的普通文件")
	}
	uriPath := filepath.ToSlash(path)
	if !strings.HasPrefix(uriPath, "/") {
		uriPath = "/" + uriPath
	}
	u := url.URL{Scheme: "file", Path: uriPath}
	q := u.Query()
	q.Set("mode", "rwc")
	if direct {
		q.Set("mode", "rw")
	}
	q.Set("_txlock", "immediate")
	q.Add("_pragma", "synchronous(FULL)")
	q.Add("_pragma", "busy_timeout(5000)")
	q.Add("_pragma", "foreign_keys(ON)")
	u.RawQuery = q.Encode()
	db, err := sql.Open("sqlite", u.String())
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(8)
	tx, err := db.BeginTx(context.Background(), nil)
	if err != nil {
		db.Close()
		return nil, err
	}
	defer tx.Rollback()
	if direct {
		var required int
		if err = tx.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('accounts','characters','character_wallets','character_wallet_migrations','gmkit_config','gmkit_mails','gmkit_mail_items','gmkit_cdks','gmkit_ban_rules')").Scan(&required); err != nil || required != 9 {
			db.Close()
			return nil, fmt.Errorf("正式游戏数据库结构不匹配，未初始化空库")
		}
		var marker int
		if err = tx.QueryRow(`SELECT COUNT(*) FROM character_wallet_migrations m
WHERE m.migration_key='character_wallets_all_to_account_cargo_v1' AND m.policy='all_to_account_cargo' AND m.total_before=m.total_after
AND m.account_count=(SELECT count(*) FROM character_wallet_migration_accounts)
AND m.total_before=(SELECT COALESCE(sum(legacy_gold+cargo_before),0) FROM character_wallet_migration_accounts)
AND m.total_after=(SELECT COALESCE(sum(cargo_after),0) FROM character_wallet_migration_accounts)
AND NOT EXISTS(SELECT 1 FROM character_wallet_migration_accounts WHERE legacy_gold+cargo_before != cargo_after)
AND NOT EXISTS(SELECT 1 FROM account_wallets WHERE gold != 0)`).Scan(&marker); err != nil || marker != 1 {
			db.Close()
			return nil, fmt.Errorf("角色金币迁移状态无效，未操作数据库")
		}
	}
	var schemaExists int
	if err = tx.QueryRow("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='manager_schema'").Scan(&schemaExists); err != nil {
		db.Close()
		return nil, err
	}
	if schemaExists > 0 {
		var version int
		if err = tx.QueryRow("SELECT version FROM manager_schema").Scan(&version); err != nil || version != 1 {
			tx.Rollback()
			db.Close()
			return nil, fmt.Errorf("管理数据库版本不匹配，未执行迁移")
		}
		var required int
		if err = tx.QueryRow("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('manager_schema','manager_config','manager_codes','manager_requests','manager_audit')").Scan(&required); err != nil || required != 5 {
			tx.Rollback()
			db.Close()
			return nil, fmt.Errorf("管理数据库缺少既有表，未自动补建或重置数据")
		}
	} else {
		var existing int
		if err = tx.QueryRow("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").Scan(&existing); err != nil || existing > 0 && !direct {
			tx.Rollback()
			db.Close()
			return nil, fmt.Errorf("目标文件不是独立管理数据库，未修改")
		}
	}
	_, err = tx.Exec(`CREATE TABLE IF NOT EXISTS manager_schema(version INTEGER NOT NULL);
INSERT INTO manager_schema(version) SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM manager_schema);
CREATE TABLE IF NOT EXISTS manager_config(section TEXT PRIMARY KEY,revision INTEGER NOT NULL,value_json TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manager_codes(code TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,reward_json TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS manager_codes_kind_created ON manager_codes(kind,created_at DESC);
CREATE TABLE IF NOT EXISTS manager_requests(request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,status INTEGER NOT NULL,reply_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manager_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,actor TEXT NOT NULL,module TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,result TEXT NOT NULL,request_id TEXT NOT NULL,detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS manager_audit_time ON manager_audit(at DESC,id DESC);
CREATE INDEX IF NOT EXISTS manager_audit_module ON manager_audit(module,id DESC);`)
	if err != nil {
		tx.Rollback()
		db.Close()
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		db.Close()
		return nil, err
	}
	if _, err = db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		db.Close()
		return nil, err
	}
	return &stateStore{db: db, path: path}, nil
}

type rowQuerier interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func readRequest(ctx context.Context, q rowQuerier, id string) (savedRequest, bool, error) {
	var saved savedRequest
	var raw string
	err := q.QueryRowContext(ctx, "SELECT fingerprint,status,reply_json FROM manager_requests WHERE request_id=?", id).Scan(&saved.Fingerprint, &saved.Status, &raw)
	if err == sql.ErrNoRows {
		return saved, false, nil
	}
	if err != nil {
		return saved, false, err
	}
	if err = json.Unmarshal([]byte(raw), &saved.Reply); err != nil {
		return saved, false, err
	}
	return saved, true, nil
}
func saveRequest(ctx context.Context, tx *sql.Tx, id string, saved savedRequest, replace bool) error {
	raw, err := json.Marshal(saved.Reply)
	if err != nil {
		return err
	}
	statement := "INSERT INTO manager_requests(request_id,fingerprint,status,reply_json) VALUES(?,?,?,?)"
	if replace {
		statement += " ON CONFLICT(request_id) DO UPDATE SET status=excluded.status,reply_json=excluded.reply_json WHERE manager_requests.fingerprint=excluded.fingerprint"
	}
	result, err := tx.ExecContext(ctx, statement, id, saved.Fingerprint, saved.Status, string(raw))
	if err == nil {
		count, _ := result.RowsAffected()
		if count != 1 {
			return fail(409, "request_conflict", "请求编号已用于其他内容")
		}
	}
	return err
}
func (s *stateStore) transaction(ctx context.Context, fn func(*sql.Tx) error) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err = fn(tx); err != nil {
		return err
	}
	if err = tx.Commit(); err != nil {
		return &apiFailure{Status: 503, Code: "commit_unknown", Message: "提交结果暂时未知，请使用原请求编号查询回执", Unknown: true}
	}
	return nil
}
func appendAudit(ctx context.Context, tx *sql.Tx, module, action, target, result, id, detail string) error {
	_, err := tx.ExecContext(ctx, "INSERT INTO manager_audit(at,actor,module,action,target,result,request_id,detail) VALUES(?,?,?,?,?,?,?,?)", metadataTime(time.Now()), "administrator", module, action, target, result, id, detail)
	return err
}
func readConfig(ctx context.Context, q rowQuerier, section string) (adminapi.ConfigEntry, error) {
	entry := adminapi.ConfigEntry{Section: section, Value: map[string]any{}}
	var raw, updated string
	err := q.QueryRowContext(ctx, "SELECT revision,value_json,updated_at FROM manager_config WHERE section=?", section).Scan(&entry.Revision, &raw, &updated)
	if err == sql.ErrNoRows {
		return entry, nil
	}
	if err != nil {
		return entry, err
	}
	err = json.Unmarshal([]byte(raw), &entry.Value)
	entry.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
	return entry, err
}
func containsPattern(value string) string {
	return "%" + strings.NewReplacer("\\", "\\\\", "%", "\\%", "_", "\\_").Replace(value) + "%"
}

// Snapshot is used only by isolated persistence tests. Request handlers query
// indexed tables and bounded pages instead of copying all management history.
func (s *stateStore) snapshot() struct{ Codes []adminapi.Code } {
	result := struct{ Codes []adminapi.Code }{}
	rows, err := s.db.Query("SELECT code,name,kind,reward_json,state,created_at FROM manager_codes")
	if err != nil {
		return result
	}
	defer rows.Close()
	for rows.Next() {
		var c adminapi.Code
		var raw, created string
		if rows.Scan(&c.Code, &c.Name, &c.Kind, &raw, &c.State, &created) == nil {
			_ = json.Unmarshal([]byte(raw), &c.Reward)
			c.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
			result.Codes = append(result.Codes, c)
		}
	}
	return result
}
