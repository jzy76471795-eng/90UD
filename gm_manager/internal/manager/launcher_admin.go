package manager

import (
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/tls"
	"database/sql"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"newadmin/internal/adminapi"
	"regexp"
	"sort"
	"strings"
)

// These routes belong only to the bearer-protected management handler.
func (s *Service) launcherAdminPatches(w http.ResponseWriter, r *http.Request) {
	result, err := (&launcherService{manager: s}).patches(r.Context())
	if err != nil {
		writeError(w, err, "")
		return
	}
	write(w, 200, adminapi.Envelope{OK: true, Data: result})
}
func (s *Service) launcherAdminUpload(w http.ResponseWriter, r *http.Request) {
	var body struct {
		SHA     string `json:"sha256"`
		Content string `json:"content_base64"`
	}
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	data, err := base64.StdEncoding.DecodeString(body.Content)
	sum := sha256.Sum256(data)
	valid := err == nil && launcherHex(body.SHA, 64) && len(data) >= 256 && len(data) <= 768*1024 && hex.EncodeToString(sum[:]) == body.SHA
	if valid {
		offset := uint64(binary.LittleEndian.Uint32(data[0x3c:]))
		valid = string(data[:2]) == "MZ" && offset+24 <= uint64(len(data))
		if valid {
			valid = string(data[offset:offset+4]) == "PE\x00\x00" && binary.LittleEndian.Uint16(data[offset+4:]) == 0x14c && binary.LittleEndian.Uint16(data[offset+22:])&0x2000 != 0
		}
	}
	if !valid {
		writeError(w, fail(400, "invalid_patch", "需要摘要一致、768KiB以内的32位DLL"), "")
		return
	}
	s.mutate(w, r, body, "登录器补丁", "patches.upload", body.SHA, func(tx *sql.Tx) (any, error) {
		_, err := tx.ExecContext(r.Context(), "INSERT INTO gmkit_patch_blobs(sha256,data,bytes) VALUES(?,?,?) ON CONFLICT(sha256) DO UPDATE SET data=excluded.data,bytes=excluded.bytes", body.SHA, data, len(data))
		return map[string]any{"sha256": body.SHA, "bytes": len(data)}, err
	})
}

type launcherPatchItem struct {
	File     string `json:"file_name"`
	SHA      string `json:"sha256"`
	Bytes    int    `json:"bytes"`
	Enabled  bool   `json:"enabled"`
	Mismatch string `json:"on_mismatch"`
}

func validLauncherDLL(name string) bool {
	if !regexp.MustCompile(`^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,95}\.dll$`).MatchString(name) {
		return false
	}
	base := strings.Split(strings.ToLower(name), ".")[0]
	return !regexp.MustCompile(`^(con|prn|aux|nul|com[0-9]|lpt[0-9])$`).MatchString(base)
}
func (s *Service) launcherAdminPublish(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Revision int                 `json:"expected_revision"`
		Items    []launcherPatchItem `json:"items"`
	}
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	if body.Revision < 0 || len(body.Items) < 1 || len(body.Items) > 50 {
		writeError(w, fail(400, "invalid_patch_manifest", "补丁清单需要1至50项及有效版本"), "")
		return
	}
	seen := map[string]bool{}
	for _, item := range body.Items {
		if item.File != strings.ToLower(item.File) || !validLauncherDLL(item.File) || !launcherHex(item.SHA, 64) || item.Bytes < 256 || item.Bytes > 768*1024 || !item.Enabled || item.Mismatch != "block" || seen[item.File] {
			writeError(w, fail(400, "invalid_patch_manifest", "补丁名称、摘要、大小或启动策略无效"), "")
			return
		}
		seen[item.File] = true
	}
	s.mutate(w, r, body, "登录器补丁", "patches.publish", "current", func(tx *sql.Tx) (any, error) {
		revision := 0
		raw := "[]"
		err := tx.QueryRowContext(r.Context(), "SELECT revision,manifest FROM gmkit_patch_current WHERE id=1").Scan(&revision, &raw)
		if err != nil && err != sql.ErrNoRows {
			return nil, err
		}
		if revision != body.Revision {
			return nil, fail(409, "patch_revision_changed", "补丁清单已更新，请刷新")
		}
		var prior []launcherPatchItem
		if err = json.Unmarshal([]byte(raw), &prior); err != nil {
			return nil, err
		}
		for _, item := range prior {
			if !seen[item.File] {
				return nil, fail(409, "patch_removal_forbidden", "不能移除已分发补丁，可发布同名回退版本")
			}
		}
		for _, item := range body.Items {
			var size int
			if err = tx.QueryRowContext(r.Context(), "SELECT bytes FROM gmkit_patch_blobs WHERE sha256=?", item.SHA).Scan(&size); err != nil || size != item.Bytes {
				return nil, fail(409, "patch_blob_missing", "请先上传清单对应的DLL")
			}
		}
		sort.Slice(body.Items, func(i, j int) bool { return body.Items[i].File < body.Items[j].File })
		revision++
		_, err = tx.ExecContext(r.Context(), "INSERT INTO gmkit_patch_current(id,revision,manifest) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,manifest=excluded.manifest", revision, jsonText(body.Items))
		return map[string]any{"revision": revision, "items": body.Items}, err
	})
}
func (s *Service) launcherAdminSign(w http.ResponseWriter, r *http.Request) {
	var body struct {
		SHA string `json:"manifest_sha256"`
	}
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	if !launcherHex(body.SHA, 64) {
		writeError(w, fail(400, "invalid_digest", "需要资源清单SHA256"), "")
		return
	}
	pair, err := tls.LoadX509KeyPair(s.config.LauncherCertificate, s.config.LauncherKey)
	if err != nil {
		writeError(w, fail(503, "signing_unavailable", "登录器签名身份不可用"), "")
		return
	}
	key, ok := pair.PrivateKey.(*rsa.PrivateKey)
	if !ok || key.N.BitLen() < 2048 {
		writeError(w, fail(503, "signing_unavailable", "签名密钥类型无效"), "")
		return
	}
	s.mutate(w, r, body, "登录器资源", "resources.sign", body.SHA, func(tx *sql.Tx) (any, error) {
		raw, _ := hex.DecodeString(body.SHA)
		digest := sha256.Sum256(append([]byte("90US-UPDATE-V1\x00resources\x00"), raw...))
		signature, err := rsa.SignPSS(rand.Reader, key, crypto.SHA256, digest[:], &rsa.PSSOptions{SaltLength: 32, Hash: crypto.SHA256})
		return map[string]any{"signature": base64.StdEncoding.EncodeToString(signature)}, err
	})
}
