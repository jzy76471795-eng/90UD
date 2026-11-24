package manager

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const launcherResourcePrefix = "/launcher/v1/resources/"

// Serve the signed client bundle for the active PVF pin. The digest also
// names the file so a release switch cannot serve stale bundle bytes.
func (l *launcherService) serveResourceUpdate(w http.ResponseWriter, r *http.Request) {
	failDownload := func(status int, code, message string) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": code, "message": message}})
	}
	name := strings.TrimPrefix(r.URL.Path, launcherResourcePrefix)
	digest, zipped := strings.CutSuffix(name, ".zip")
	currentAlias := name == "current.zip"
	if !currentAlias && (!zipped || !launcherHex(digest, 64)) {
		failDownload(404, "NOT_FOUND", "resource update not found")
		return
	}
	current, err := l.currentPVF(r.Context())
	if err != nil {
		failDownload(503, "PVF_REGISTRY_UNAVAILABLE", "PVF registry unavailable")
		return
	}
	if current == nil || !current.enabled || !launcherHex(current.digest, 64) ||
		(!currentAlias && current.digest != digest) {
		failDownload(404, "NOT_FOUND", "resource update is not active")
		return
	}
	name = current.digest + ".zip"
	select {
	case l.downloads <- struct{}{}:
		defer func() { <-l.downloads }()
	default:
		failDownload(503, "DOWNLOAD_BUSY", "resource update download limit reached")
		return
	}
	file, err := os.Open(filepath.Join(l.manager.config.ServerRoot, "server_proto", "resources", "launcher", name))
	if err != nil {
		failDownload(404, "NOT_FOUND", "resource update file unavailable")
		return
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() < 4 {
		failDownload(404, "NOT_FOUND", "resource update file invalid")
		return
	}
	w.Header().Set("Content-Type", "application/zip")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Content-Disposition", "attachment; filename=90US-update.zip")
	_ = http.NewResponseController(w).SetWriteDeadline(time.Now().Add(30 * time.Minute))
	http.ServeContent(w, r, name, info.ModTime(), file)
}
