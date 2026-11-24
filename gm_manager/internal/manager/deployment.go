package manager

import (
	"net/http"
	"newadmin/internal/adminapi"
	"path/filepath"
	"strconv"
	"time"
)

func (s *Service) runtimeRoot() (string, error) {
	root := s.config.ManagerDirectory
	if !filepath.IsAbs(root) {
		return "", fail(503, "runtime_unconfigured", "未配置独立GM运行目录")
	}
	return filepath.Clean(root), nil
}
func (s *Service) deploymentStatus(w http.ResponseWriter, r *http.Request) {
	root, err := s.runtimeRoot()
	if err != nil {
		writeError(w, err, "")
		return
	}
	sha, size, err := fileSHA(filepath.Join(root, "gmserver.exe"))
	if err != nil {
		writeError(w, err, "")
		return
	}
	write(w, 200, adminapi.Envelope{OK: true, Data: map[string]any{"sha256": sha, "size": size, "directory": root, "mode": "winrm", "task": s.config.ManagerTaskName, "version": adminapi.Version}})
}
func serveHashedFile(w http.ResponseWriter, r *http.Request, path string) {
	_ = http.NewResponseController(w).SetWriteDeadline(time.Now().Add(15 * time.Minute))
	sha, size, err := fileSHA(path)
	if err != nil {
		writeError(w, fail(503, "file_unavailable", "运行文件不可读取"), "")
		return
	}
	w.Header().Set("X-Content-SHA256", sha)
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Length", strconv.FormatInt(size, 10))
	http.ServeFile(w, r, path)
}
func (s *Service) downloadBinary(w http.ResponseWriter, r *http.Request) {
	root, err := s.runtimeRoot()
	if err != nil {
		writeError(w, err, "")
		return
	}
	serveHashedFile(w, r, filepath.Join(root, "gmserver.exe"))
}
func (s *Service) downloadConfig(w http.ResponseWriter, r *http.Request) {
	root, err := s.runtimeRoot()
	if err != nil {
		writeError(w, err, "")
		return
	}
	serveHashedFile(w, r, filepath.Join(root, "go-manager.json"))
}
func (s *Service) downloadPVF(w http.ResponseWriter, r *http.Request) {
	serveHashedFile(w, r, filepath.Join(s.config.ServerRoot, "Script.pvf"))
}
func (s *Service) ShutdownRequested() <-chan struct{} { return s.shutdown }
func (s *Service) retiredUpdater(w http.ResponseWriter, r *http.Request) {
	writeError(w, fail(410, "updater_retired", "托管自更新已停用，请使用本机加密WinRM运维更新"), r.Header.Get("Idempotency-Key"))
}
