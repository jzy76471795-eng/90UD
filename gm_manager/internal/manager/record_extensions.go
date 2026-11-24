package manager

import (
	"context"
	"database/sql"
)

func initializeGoldLedger(ctx context.Context, tx *sql.Tx) error {
	_, err := tx.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS manager_record_coverage(kind TEXT PRIMARY KEY,started_at TEXT NOT NULL,note TEXT NOT NULL,baseline_id INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO manager_record_coverage VALUES('gold',strftime('%Y-%m-%dT%H:%M:%SZ','now'),'钱包余额变动；消费场景由原业务决定',(SELECT COALESCE(MAX(rowid),0) FROM gmkit_value_credits));
CREATE TABLE IF NOT EXISTS manager_gold_ledger(id INTEGER PRIMARY KEY AUTOINCREMENT,account_id INTEGER NOT NULL,character_id INTEGER NOT NULL,balance_before INTEGER NOT NULL,balance_after INTEGER NOT NULL,delta INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS manager_gold_balance_changed AFTER UPDATE OF gold ON character_wallets WHEN NEW.gold!=OLD.gold BEGIN
INSERT INTO manager_gold_ledger(account_id,character_id,balance_before,balance_after,delta,created_at) SELECT account_id,NEW.character_id,OLD.gold,NEW.gold,NEW.gold-OLD.gold,strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM characters WHERE character_id=NEW.character_id;END;
CREATE INDEX IF NOT EXISTS manager_gold_ledger_character ON manager_gold_ledger(character_id,id DESC);`)
	return err
}
