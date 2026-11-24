package manager

import (
	"context"
	"database/sql"
	"strconv"
	"time"

	"newadmin/internal/adminapi"
)

func (g *gameReader) readBansAndIdentity(ctx context.Context, db *sql.DB, accounts []adminapi.Account, liveIPs map[int]string) error {
	if cols := sqlColumns(ctx, db, "gmkit_identity"); cols["account_id"] && cols["remote_ip"] && cols["machine_id"] {
		rows, err := db.QueryContext(ctx, "SELECT account_id,remote_ip,machine_id FROM gmkit_identity")
		if err != nil {
			return err
		}
		identities := map[int][2]string{}
		for rows.Next() {
			var id int
			var ip, machine string
			if err = rows.Scan(&id, &ip, &machine); err != nil {
				rows.Close()
				return err
			}
			identities[id] = [2]string{ip, machine}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		for i := range accounts {
			if v, ok := identities[accounts[i].ID]; ok {
				if v[0] != "" {
					accounts[i].LoginIP = v[0]
				}
				accounts[i].MachineID = v[1]
			}
		}
	}
	for i := range accounts {
		if ip := liveIPs[accounts[i].ID]; ip != "" {
			accounts[i].LoginIP = ip
		}
	}
	cols := sqlColumns(ctx, db, "gmkit_ban_rules")
	if !cols["kind"] || !cols["value"] || !cols["revoked_at"] || !cols["expires_at"] {
		return nil
	}
	rows, err := db.QueryContext(ctx, "SELECT kind,value,expires_at FROM gmkit_ban_rules WHERE revoked_at IS NULL OR revoked_at=''")
	if err != nil {
		return err
	}
	blocked := map[string]bool{}
	for rows.Next() {
		var kind, value string
		var expiry sql.NullString
		if err = rows.Scan(&kind, &value, &expiry); err != nil {
			rows.Close()
			return err
		}
		if expiry.Valid && expiry.String != "" {
			if !banExpiryActive(expiry.String) {
				continue
			}
		}
		blocked[kind+":"+value] = true
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	if legacyCols := sqlColumns(ctx, db, "gmkit_bans"); legacyCols["account_id"] {
		expiryColumn := "NULL"
		if legacyCols["expires_at"] {
			expiryColumn = "expires_at"
		}
		legacy, err := db.QueryContext(ctx, "SELECT account_id,"+expiryColumn+" FROM gmkit_bans")
		if err != nil {
			return err
		}
		for legacy.Next() {
			var id int
			var expiry sql.NullString
			if err = legacy.Scan(&id, &expiry); err != nil {
				legacy.Close()
				return err
			}
			if !expiry.Valid || expiry.String == "" || banExpiryActive(expiry.String) {
				blocked["account:"+strconv.Itoa(id)] = true
			}
		}
		err = legacy.Err()
		legacy.Close()
		if err != nil {
			return err
		}
	}
	for i := range accounts {
		a := &accounts[i]
		a.BanKnown = true
		a.AccountBlocked = blocked["account:"+strconv.Itoa(a.ID)]
		a.IPBlocked = a.LoginIP != "" && blocked["ip:"+a.LoginIP]
		a.MachineBlocked = a.MachineID != "" && blocked["machine:"+a.MachineID]
		a.Banned = a.AccountBlocked || a.IPBlocked || a.MachineBlocked
	}
	return nil
}
func banExpiryActive(value string) bool {
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02 15:04:05", "2006-01-02T15:04:05"} {
		if expiry, err := time.ParseInLocation(layout, value, time.Local); err == nil {
			return expiry.After(time.Now())
		}
	}
	return true
}
func recordTime(value string) (time.Time, bool) {
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02 15:04:05", "2006-01-02T15:04:05"} {
		if result, err := time.ParseInLocation(layout, value, time.UTC); err == nil {
			return result, true
		}
	}
	return time.Time{}, false
}
