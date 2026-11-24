package manager

import (
	"context"
	"database/sql/driver"
	"fmt"
	"io"
	"modernc.org/sqlite"
	"net/http"
	"newadmin/internal/artifacts"
	"strconv"
	"time"
)

func (s *Service) snapshotBytes(ctx context.Context) ([]byte, error) {
	conn, err := s.store.db.Conn(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	var raw []byte
	err = conn.Raw(func(source any) error {
		backup, err := source.(interface {
			NewBackup(string) (*sqlite.Backup, error)
		}).NewBackup(":memory:")
		if err != nil {
			return err
		}
		if _, err = backup.Step(-1); err != nil {
			backup.Finish()
			return err
		}
		memory, err := backup.Commit()
		if err != nil {
			return err
		}
		defer memory.Close()
		for _, check := range []string{"PRAGMA integrity_check", "PRAGMA foreign_key_check"} {
			rows, err := memory.(driver.QueryerContext).QueryContext(ctx, check, nil)
			if err != nil {
				return err
			}
			values := make([]driver.Value, len(rows.Columns()))
			next := rows.Next(values)
			rows.Close()
			if check == "PRAGMA integrity_check" {
				if next != nil || len(values) != 1 || values[0] != "ok" {
					return fmt.Errorf("数据库完整性检查未通过")
				}
			} else if next != io.EOF {
				return fmt.Errorf("数据库外键检查未通过")
			}
		}
		raw, err = memory.(interface{ Serialize() ([]byte, error) }).Serialize()
		return err
	})
	if err != nil {
		return nil, err
	}
	if len(raw) < 100 || len(raw) > 512<<20 {
		return nil, fmt.Errorf("数据库备份大小无效")
	}
	raw[18], raw[19] = 1, 1
	return raw, nil
}
func (s *Service) downloadSnapshot(w http.ResponseWriter, r *http.Request) {
	_ = http.NewResponseController(w).SetWriteDeadline(time.Now().Add(2 * time.Minute))
	raw, err := s.snapshotBytes(r.Context())
	if err != nil {
		writeError(w, fail(503, "backup_failed", "远程内存一致备份或完整性检查失败，未生成远程备份文件"), "")
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Length", strconv.Itoa(len(raw)))
	w.Header().Set("X-Content-SHA256", artifacts.SHA(raw))
	_, _ = w.Write(raw)
}
