package manager

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"io"
	"net/http"
	"net/url"
	"newadmin/internal/adminapi"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

type pvfRelease struct {
	ID        int64  `json:"id"`
	Version   string `json:"pvf_version"`
	SHA       string `json:"file_sha256"`
	Key       string `json:"key_set"`
	SourceSHA string `json:"source_sha256,omitempty"`
	URL       string `json:"download_url"`
	ClientURL string `json:"client_download_url"`
	Enabled   bool   `json:"enabled"`
	Current   bool   `json:"current"`
	Confirm   bool   `json:"confirm,omitempty"`
}

func fileSHA(path string) (string, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	hash := sha256.New()
	size, err := io.Copy(hash, f)
	return hex.EncodeToString(hash.Sum(nil)), size, err
}
func (s *Service) pvfReleases(w http.ResponseWriter, r *http.Request) {
	rows, err := s.store.db.QueryContext(r.Context(), "SELECT r.id,r.pvf_version,r.file_sha256,r.key_set,r.download_url,r.client_download_url,r.enabled,COALESCE(c.slot,0) FROM gmkit_pvf_releases r LEFT JOIN gmkit_pvf_current c ON c.release_id=r.id ORDER BY r.id DESC")
	if err != nil {
		writeError(w, err, "")
		return
	}
	defer rows.Close()
	items := []pvfRelease{}
	for rows.Next() {
		var row pvfRelease
		var enabled, current int
		if err = rows.Scan(&row.ID, &row.Version, &row.SHA, &row.Key, &row.URL, &row.ClientURL, &enabled, &current); err != nil {
			writeError(w, err, "")
			return
		}
		row.Enabled = enabled == 1
		row.Current = current == 1
		items = append(items, row)
	}
	if err = rows.Err(); err != nil {
		writeError(w, err, "")
		return
	}
	sha, size, err := fileSHA(filepath.Join(s.config.ServerRoot, "Script.pvf"))
	if err != nil {
		writeError(w, fail(503, "pvf_unavailable", "正式服务端PVF不可读"), "")
		return
	}
	write(w, 200, adminapi.Envelope{OK: true, Data: map[string]any{"items": items, "source_sha256": sha, "source_size": size}})
}
func validPublicURL(value string) bool {
	if value == "" {
		return true
	}
	u, e := url.Parse(value)
	return e == nil && u.Scheme == "https" && u.Hostname() != "" && u.User == nil && u.Fragment == "" && !strings.ContainsAny(value, "\r\n\t ")
}
func (s *Service) publishPVF(w http.ResponseWriter, r *http.Request) {
	var body pvfRelease
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	if !s.config.DirectDatabase || !body.Confirm || !regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`).MatchString(body.Version) || !regexp.MustCompile(`^[a-fA-F0-9]{64}$`).MatchString(body.SHA) || !regexp.MustCompile(`^[a-fA-F0-9]{96}$`).MatchString(body.Key) || !validPublicURL(body.URL) || !validPublicURL(body.ClientURL) {
		writeError(w, fail(400, "invalid_pvf_release", "版本、SHA256、96位密钥或HTTPS下载地址无效"), "")
		return
	}
	body.SHA = strings.ToLower(body.SHA)
	body.Key = strings.ToLower(body.Key)
	if body.Enabled {
		actual, _, err := fileSHA(filepath.Join(s.config.ServerRoot, "Script.pvf"))
		if err != nil || actual != body.SourceSHA {
			writeError(w, fail(409, "pvf_source_mismatch", "本次客户端PVF的源文件与服务器正在使用的源文件不同，请先停服同步原始PVF"), "")
			return
		}
	}
	s.mutate(w, r, body, "PVF管理", "pvf.publish", body.Version, func(tx *sql.Tx) (any, error) {
		var id int64
		var oldSHA, oldKey string
		err := tx.QueryRowContext(r.Context(), "SELECT id,file_sha256,key_set FROM gmkit_pvf_releases WHERE pvf_version=?", body.Version).Scan(&id, &oldSHA, &oldKey)
		now := time.Now().UTC().Format(time.RFC3339)
		enabled := 0
		if body.Enabled {
			enabled = 1
		}
		if err == sql.ErrNoRows {
			res, e := tx.ExecContext(r.Context(), "INSERT INTO gmkit_pvf_releases(pvf_version,file_sha256,key_set,download_url,client_download_url,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", body.Version, body.SHA, body.Key, body.URL, body.ClientURL, enabled, now, now)
			if e != nil {
				return nil, e
			}
			id, err = res.LastInsertId()
		} else if err == nil {
			if oldSHA != body.SHA || oldKey != body.Key {
				return nil, fail(409, "pvf_version_conflict", "同一版本不能指向不同文件或密钥，请使用新版本号")
			}
			_, err = tx.ExecContext(r.Context(), "UPDATE gmkit_pvf_releases SET download_url=?,client_download_url=?,enabled=?,updated_at=? WHERE id=?", body.URL, body.ClientURL, enabled, now, id)
		}
		if err != nil {
			return nil, err
		}
		if body.Enabled {
			if _, err = tx.ExecContext(r.Context(), "INSERT INTO gmkit_pvf_current(slot,release_id) VALUES(1,?) ON CONFLICT(slot) DO UPDATE SET release_id=excluded.release_id", id); err != nil {
				return nil, err
			}
		}
		// Keep the native player's private registry and the GM editable fields in one transaction.
		value := map[string]any{"update_url": body.URL, "client_url": body.ClientURL}
		if err = setGameConfig(r.Context(), tx, "gm_manager.publishing", value); err != nil {
			return nil, err
		}
		prior, err := readConfig(r.Context(), tx, "publishing")
		if err != nil {
			return nil, err
		}
		_, err = tx.ExecContext(r.Context(), "UPDATE manager_config SET revision=?,value_json=?,updated_at=? WHERE section='publishing'", prior.Revision+1, jsonText(value), metadataTime(time.Now()))
		if err != nil {
			return nil, err
		}
		return adminapi.Receipt{RequestID: r.Header.Get("Idempotency-Key"), Action: "pvf.publish", State: "database_committed", DatabaseCommitted: true, Message: "版本与私有密钥已写入现有游戏注册库；玩家HTTPS接口读取本版本，未自动替换服务器PVF", Result: map[string]any{"id": id, "enabled": body.Enabled}, CreatedAt: time.Now().UTC()}, nil
	})
}
func publishingFromRegistry(ctx context.Context, q rowQuerier) (map[string]any, error) {
	var update, client string
	err := q.QueryRowContext(ctx, "SELECT r.download_url,r.client_download_url FROM gmkit_pvf_releases r JOIN gmkit_pvf_current c ON c.release_id=r.id WHERE c.slot=1").Scan(&update, &client)
	if err == sql.ErrNoRows {
		return map[string]any{"update_url": "", "client_url": ""}, nil
	}
	return map[string]any{"update_url": update, "client_url": client}, err
}
