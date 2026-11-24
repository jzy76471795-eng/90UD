package manager

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"newadmin/internal/adminapi"
)

const recordCleanupPreviewValidity = 30 * time.Minute

type recordCleanupSource struct {
	table string
	id    string
}

func cleanupSource(kind string) (recordCleanupSource, bool) {
	// Only append-only balance history belongs here. Grants, redemption receipts,
	// mail, raid ledgers and security/operation audit have separate retention needs.
	switch kind {
	case "gold":
		return recordCleanupSource{"manager_gold_ledger", "id"}, true
	case "currency":
		return recordCleanupSource{"gmkit_cera_ledger", "id"}, true
	case "revive":
		return recordCleanupSource{"account_revive_coin_ledger", "ledger_id"}, true
	default:
		return recordCleanupSource{}, false
	}
}

func cleanupBefore(value string, now time.Time) (string, error) {
	local := time.FixedZone("Asia/Shanghai", 8*3600)
	const dateLayout = "2006-01-02"
	date, err := time.ParseInLocation(dateLayout, value, local)
	if err != nil || date.Format(dateLayout) != value || date.Year() < 1 || value > now.In(local).Format(dateLayout) {
		return "", fail(400, "invalid_cleanup_date", "请输入有效的清理日期，最晚可选今天")
	}
	return date.UTC().Format(recordStampLayout), nil
}

func (s *Service) cleanupProof(plan adminapi.RecordCleanupPlan) []byte {
	plan.Proof = ""
	raw, _ := json.Marshal(plan)
	mac := hmac.New(sha256.New, []byte(s.config.Token))
	mac.Write([]byte("gm-record-cleanup/v1\x00"))
	mac.Write(raw)
	return mac.Sum(nil)
}

func cleanupSources(targets []adminapi.RecordCleanupTarget) ([]recordCleanupSource, error) {
	if len(targets) < 1 || len(targets) > 3 {
		return nil, fail(400, "invalid_cleanup_kind", "请选择要清理的流水")
	}
	seen := make(map[string]bool, len(targets))
	sources := make([]recordCleanupSource, 0, len(targets))
	for _, target := range targets {
		source, allowed := cleanupSource(target.Kind)
		if !allowed || seen[target.Kind] {
			return nil, fail(400, "invalid_cleanup_kind", "流水类型不支持清理或重复选择")
		}
		seen[target.Kind] = true
		sources = append(sources, source)
	}
	return sources, nil
}

func (source recordCleanupSource) check(ctx context.Context, tx *sql.Tx) error {
	var standalone bool
	err := tx.QueryRowContext(ctx, `SELECT
		EXISTS(SELECT 1 FROM pragma_table_info(?) WHERE name=? AND pk=1 AND upper(type)='INTEGER')
		AND NOT EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND lower(tbl_name)=?)
		AND NOT EXISTS(SELECT 1 FROM sqlite_master m, pragma_foreign_key_list(m.name) f
			WHERE m.type='table' AND lower(f."table")=?)`, source.table, source.id, source.table, source.table).Scan(&standalone)
	if err != nil || !standalone {
		return fail(409, "cleanup_schema_changed", "流水表结构不可确认或与其他数据关联，未清理")
	}
	return nil
}

func (source recordCleanupSource) predicate() string {
	return source.id + "<=? AND gm_record_stamp_v1(created_at)<?"
}

func (source recordCleanupSource) preview(ctx context.Context, tx *sql.Tx, cutoff string, target *adminapi.RecordCleanupTarget) error {
	if err := source.check(ctx, tx); err != nil {
		return err
	}
	if err := tx.QueryRowContext(ctx, "SELECT COALESCE(MAX("+source.id+"),0) FROM "+source.table).Scan(&target.MaxID); err != nil {
		return recordQueryError(err)
	}
	if err := tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM "+source.table+" WHERE "+source.predicate(), target.MaxID, cutoff).Scan(&target.Count); err != nil {
		return recordQueryError(err)
	}
	return nil
}

func (s *Service) previewRecordCleanup(w http.ResponseWriter, r *http.Request) {
	if !s.config.DirectDatabase {
		writeError(w, fail(409, "cleanup_unavailable", "当前连接未启用游戏数据库直写"), "")
		return
	}
	plan := adminapi.RecordCleanupPlan{Before: r.URL.Query().Get("before")}
	cutoff, err := cleanupBefore(plan.Before, time.Now())
	if err != nil {
		writeError(w, err, "")
		return
	}
	for _, kind := range r.URL.Query()["kind"] {
		plan.Targets = append(plan.Targets, adminapi.RecordCleanupTarget{Kind: kind})
	}
	sources, err := cleanupSources(plan.Targets)
	if err != nil {
		writeError(w, err, "")
		return
	}
	db, err := s.reader.open()
	if err != nil {
		writeError(w, err, "")
		return
	}
	defer db.Close()
	tx, err := db.BeginTx(r.Context(), nil)
	if err != nil {
		writeError(w, recordQueryError(err), "")
		return
	}
	defer tx.Rollback()
	for i, source := range sources {
		if err = source.preview(r.Context(), tx, cutoff, &plan.Targets[i]); err != nil {
			writeError(w, err, "")
			return
		}
	}
	plan.ExpiresAt = time.Now().UTC().Add(recordCleanupPreviewValidity)
	plan.Proof = hex.EncodeToString(s.cleanupProof(plan))
	write(w, http.StatusOK, adminapi.Envelope{OK: true, Data: plan})
}

func (s *Service) cleanRecords(w http.ResponseWriter, r *http.Request) {
	if !s.config.DirectDatabase {
		writeError(w, fail(409, "cleanup_unavailable", "当前连接未启用游戏数据库直写"), "")
		return
	}
	var body adminapi.RecordCleanupRequest
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	proof, proofErr := hex.DecodeString(body.Plan.Proof)
	backup, backupErr := hex.DecodeString(body.BackupSHA256)
	if !body.Confirm || backupErr != nil || len(backup) != sha256.Size || proofErr != nil || !hmac.Equal(proof, s.cleanupProof(body.Plan)) {
		writeError(w, fail(400, "cleanup_confirmation_required", "请重新预览，确认清理范围并完成备份校验"), "")
		return
	}
	sources, err := cleanupSources(body.Plan.Targets)
	if err != nil {
		writeError(w, err, "")
		return
	}
	details := make([]string, 0, len(body.Plan.Targets))
	for _, target := range body.Plan.Targets {
		details = append(details, fmt.Sprintf("%s %d 条", adminapi.RecordCleanupLabel(target.Kind), target.Count))
	}
	targetDescription := body.Plan.Before + " 之前的流水（" + strings.Join(details, "，") + "）"
	s.mutate(w, r, body, "记录查询", "records.cleanup", targetDescription, func(tx *sql.Tx) (any, error) {
		now := time.Now()
		if !now.Before(body.Plan.ExpiresAt) {
			return nil, fail(409, "cleanup_preview_expired", "清理预览已过期，请重新预览")
		}
		cutoff, err := cleanupBefore(body.Plan.Before, now)
		if err != nil {
			return nil, err
		}
		return executeRecordCleanup(r.Context(), tx, sources, cutoff, body)
	})
}

func executeRecordCleanup(ctx context.Context, tx *sql.Tx, sources []recordCleanupSource, cutoff string, body adminapi.RecordCleanupRequest) (adminapi.RecordCleanupResult, error) {
	result := adminapi.RecordCleanupResult{Before: body.Plan.Before, BackupSHA256: body.BackupSHA256}
	var freeBefore, freeAfter, pageSize int64
	if err := tx.QueryRowContext(ctx, "PRAGMA freelist_count").Scan(&freeBefore); err != nil {
		return result, err
	}
	for i, source := range sources {
		target := body.Plan.Targets[i]
		if err := source.check(ctx, tx); err != nil {
			return result, err
		}
		deleted, err := tx.ExecContext(ctx, "DELETE FROM "+source.table+" WHERE "+source.predicate(), target.MaxID, cutoff)
		if err != nil {
			return result, recordQueryError(err)
		}
		count, err := deleted.RowsAffected()
		if err != nil {
			return result, err
		}
		if count != target.Count {
			return result, fail(409, "cleanup_preview_changed", "清理范围已变化，本次未删除记录，请重新预览")
		}
		result.Deleted = append(result.Deleted, target)
	}
	if err := tx.QueryRowContext(ctx, "PRAGMA freelist_count").Scan(&freeAfter); err != nil {
		return result, err
	}
	if err := tx.QueryRowContext(ctx, "PRAGMA page_size").Scan(&pageSize); err != nil {
		return result, err
	}
	result.Reusable = max(0, freeAfter-freeBefore) * pageSize
	return result, nil
}
