package manager

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

func launcherHex(s string, n int) bool {
	if len(s) != n {
		return false
	}
	for _, r := range s {
		if !(r >= '0' && r <= '9' || r >= 'a' && r <= 'f') {
			return false
		}
	}
	return true
}
func (l *launcherService) sign(kind string, document any) (map[string]string, error) {
	if kind != "patches" && kind != "resources" {
		return nil, fmt.Errorf("invalid signature domain")
	}
	raw, err := json.Marshal(document)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(raw)
	message := append([]byte("90US-UPDATE-V1\x00"+kind+"\x00"), sum[:]...)
	digest := sha256.Sum256(message)
	signature, err := rsa.SignPSS(rand.Reader, l.key, crypto.SHA256, digest[:], &rsa.PSSOptions{SaltLength: 32, Hash: crypto.SHA256})
	if err != nil {
		return nil, err
	}
	return map[string]string{"payload": base64.StdEncoding.EncodeToString(raw), "signature": base64.StdEncoding.EncodeToString(signature)}, nil
}
func (l *launcherService) patches(ctx context.Context) (map[string]any, error) {
	revision := 0
	raw := "[]"
	err := l.manager.store.db.QueryRowContext(ctx, "SELECT revision,manifest FROM gmkit_patch_current WHERE id=1").Scan(&revision, &raw)
	if err != nil && err != sql.ErrNoRows {
		return nil, err
	}
	var items []map[string]any
	if json.Unmarshal([]byte(raw), &items) != nil || items == nil {
		return nil, fmt.Errorf("invalid published patch manifest")
	}
	return map[string]any{"revision": revision, "items": items}, nil
}
func (l *launcherService) meta(ctx context.Context) (map[string]any, error) {
	var result map[string]any
	if err := l.engine(ctx, "meta", map[string]any{"advertise_host": l.host}, &result); err != nil {
		return nil, err
	}
	stage, ok := result["stage3"].(map[string]any)
	if !ok {
		return nil, fail(503, "SERVER_NOT_READY", "game metadata unavailable")
	}
	patches, err := l.patches(ctx)
	if err != nil {
		return nil, err
	}
	signed, err := l.sign("patches", patches)
	if err != nil {
		return nil, err
	}
	patches["signed"] = signed
	stage["client_patches"] = patches
	latest, err := l.pvfLatest(ctx)
	if err != nil {
		return nil, err
	}
	result["pvf_release"] = latest
	if latest["enabled"] == true {
		data := latest["data"].(map[string]any)
		stage["pvf_profile"] = "keyset96"
		stage["pvf_sha256"] = data["file_sha256"]
		stage["pvf_version"] = data["pvf_version"]
	}
	return result, nil
}
func (l *launcherService) patchBlob(ctx context.Context, digest string) (any, error) {
	if !launcherHex(digest, 64) {
		return nil, fail(400, "PATCH_REJECTED", "invalid patch digest")
	}
	// Manifest membership and bytes must come from the same database snapshot.
	tx, err := l.manager.store.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	var manifest string
	err = tx.QueryRowContext(ctx, "SELECT manifest FROM gmkit_patch_current WHERE id=1").Scan(&manifest)
	if err == sql.ErrNoRows {
		return nil, fail(404, "PATCH_REJECTED", "patch is not published")
	}
	if err != nil {
		return nil, err
	}
	var items []struct {
		SHA string `json:"sha256"`
	}
	if json.Unmarshal([]byte(manifest), &items) != nil {
		return nil, fmt.Errorf("invalid published patch manifest")
	}
	found := false
	for _, item := range items {
		found = found || item.SHA == digest
	}
	if !found {
		return nil, fail(404, "PATCH_REJECTED", "patch is not published")
	}
	var data []byte
	var size int
	if err = tx.QueryRowContext(ctx, "SELECT data,bytes FROM gmkit_patch_blobs WHERE sha256=?", digest).Scan(&data, &size); err != nil {
		return nil, fail(503, "PATCH_REJECTED", "patch unavailable")
	}
	sum := sha256.Sum256(data)
	if size != len(data) || size > 768*1024 || hex.EncodeToString(sum[:]) != digest {
		return nil, fail(503, "PATCH_REJECTED", "patch integrity check failed")
	}
	return map[string]any{"sha256": digest, "bytes": size, "content_base64": base64.StdEncoding.EncodeToString(data)}, nil
}

type launcherPVF struct {
	version, digest, key, url, clientURL string
	enabled                              bool
}

func (l *launcherService) currentPVF(ctx context.Context) (*launcherPVF, error) {
	v := new(launcherPVF)
	var enabled int
	err := l.manager.store.db.QueryRowContext(ctx, `SELECT r.pvf_version,r.file_sha256,r.key_set,r.download_url,r.client_download_url,r.enabled FROM gmkit_pvf_current c LEFT JOIN gmkit_pvf_releases r ON r.id=c.release_id WHERE c.slot=1`).Scan(&v.version, &v.digest, &v.key, &v.url, &v.clientURL, &enabled)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, fail(503, "PVF_REGISTRY_UNAVAILABLE", "PVF registry unavailable")
	}
	v.enabled = enabled == 1
	return v, nil
}
func (l *launcherService) pvfLatest(ctx context.Context) (map[string]any, error) {
	v, err := l.currentPVF(ctx)
	if err != nil {
		return nil, err
	}
	if v == nil {
		return map[string]any{"enabled": false, "data": nil}, nil
	}
	return map[string]any{"enabled": v.enabled, "data": map[string]any{"pvf_version": v.version, "file_sha256": v.digest, "download_url": v.url, "client_download_url": v.clientURL}}, nil
}
func (l *launcherService) pvfKey(ctx context.Context, q url.Values) (any, error) {
	values := q["file_sha256"]
	if len(q) != 1 || len(values) != 1 || !launcherHex(strings.ToLower(values[0]), 64) {
		return nil, fail(400, "INVALID_REQUEST", "one file_sha256 required")
	}
	v, err := l.currentPVF(ctx)
	if err != nil {
		return nil, err
	}
	if v == nil || !v.enabled {
		return map[string]any{"enabled": false, "data": nil}, nil
	}
	if v.digest != strings.ToLower(values[0]) {
		return nil, fail(404, "PVF_UPDATE_REQUIRED", "PVF update required")
	}
	return map[string]any{"enabled": true, "data": map[string]any{"pvf_version": v.version, "file_sha256": v.digest, "key_set": v.key, "pvf_profile": "keyset96"}}, nil
}
func (l *launcherService) notices(ctx context.Context) (any, error) {
	rows, err := l.manager.store.db.QueryContext(ctx, "SELECT id,title,COALESCE(body,''),date,sort_order FROM gmkit_notices WHERE enabled=1 ORDER BY sort_order,id DESC")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var id, order int
		var title, body, date string
		if err = rows.Scan(&id, &title, &body, &date, &order); err != nil {
			return nil, err
		}
		items = append(items, map[string]any{"id": id, "title": title, "body": body, "notice_date": date, "enabled": true, "sort_order": order})
	}
	return map[string]any{"notices": items}, rows.Err()
}
func (l *launcherService) characters(ctx context.Context, aid int) (any, error) {
	rows, err := l.manager.store.db.QueryContext(ctx, "SELECT character_id,CAST(name AS BLOB),level FROM characters WHERE account_id=? AND delete_flag=0 ORDER BY character_id", aid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var id, level int
		var name []byte
		if err = rows.Scan(&id, &name, &level); err != nil {
			return nil, err
		}
		items = append(items, map[string]any{"id": id, "name": dbText(name), "level": level})
	}
	return map[string]any{"characters": items}, rows.Err()
}
func (l *launcherService) redemptionHistory(ctx context.Context, aid int) (any, error) {
	rows, err := l.manager.store.db.QueryContext(ctx, `SELECT r.id,c.name,c.code,r.character_id,r.created_at FROM gmkit_redemptions r JOIN gmkit_cdks c ON c.id=r.cdk_id WHERE r.account_id=? ORDER BY r.id DESC LIMIT 100`, aid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var id, cid int
		var name, code, created string
		if err = rows.Scan(&id, &name, &code, &cid, &created); err != nil {
			return nil, err
		}
		items = append(items, map[string]any{"id": id, "name": name, "code": code, "character_id": cid, "created_at": created})
	}
	return map[string]any{"redemptions": items}, rows.Err()
}
func (l *launcherService) announcements(ctx context.Context, r *http.Request) (any, error) {
	since := int64(-1)
	if v, ok := r.URL.Query()["since"]; ok {
		var err error
		if len(v) != 1 {
			return nil, fail(400, "INVALID_REQUEST", "invalid cursor")
		}
		since, err = strconv.ParseInt(v[0], 10, 64)
		if err != nil || since < -1 {
			return nil, fail(400, "INVALID_REQUEST", "invalid cursor")
		}
	}
	var head int64
	if err := l.manager.store.db.QueryRowContext(ctx, "SELECT COALESCE(MAX(id),0) FROM gmkit_announcements").Scan(&head); err != nil {
		return nil, err
	}
	if since == -1 {
		return map[string]any{"cursor": head, "announcement": nil}, nil
	}
	var id int64
	var content, color, created string
	err := l.manager.store.db.QueryRowContext(ctx, "SELECT id,content,color,created_at FROM gmkit_announcements WHERE id>? AND expires_at>? ORDER BY id LIMIT 1", since, gameStamp()).Scan(&id, &content, &color, &created)
	if err == sql.ErrNoRows {
		if since > head {
			head = since
		}
		return map[string]any{"cursor": head, "announcement": nil}, nil
	}
	if err != nil {
		return nil, err
	}
	return map[string]any{"cursor": id, "announcement": map[string]any{"id": id, "content": content, "color": color, "created_at": created}}, nil
}
func (l *launcherService) acknowledge(ctx context.Context, r *http.Request, aid int) (any, error) {
	body, err := launcherBody(r, []string{"id"})
	if err != nil {
		return nil, err
	}
	id, err := launcherInteger(body["id"], 1, 1<<63-1)
	if err != nil {
		return nil, err
	}
	err = l.manager.store.transaction(ctx, func(tx *sql.Tx) error {
		var n int
		if e := tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM gmkit_announcements WHERE id=?", id).Scan(&n); e != nil {
			return e
		}
		if n != 1 {
			return fail(404, "NOT_FOUND", "announcement does not exist")
		}
		_, e := tx.ExecContext(ctx, "INSERT OR IGNORE INTO gmkit_announcement_receipts(announcement_id,account_id,received_at) VALUES(?,?,?)", id, aid, gameStamp())
		return e
	})
	return map[string]bool{"received": true}, err
}
