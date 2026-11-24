package manager

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"net/http"
	"newadmin/internal/artifacts"
	"os"
	"path/filepath"
	"strconv"
)

func (s *Service) downloadRuntime(w http.ResponseWriter, r *http.Request) {
	root, err := s.runtimeRoot()
	if err != nil {
		writeError(w, err, "")
		return
	}
	buffer := bytes.NewBuffer(nil)
	writer := zip.NewWriter(buffer)
	manifest := map[string]string{}
	for _, name := range []string{"gmserver.exe", "go-manager.json", "tls/server-cert.pem", "tls/server-key.pem"} {
		path := filepath.Join(root, filepath.FromSlash(name))
		info, e := os.Lstat(path)
		if e != nil || !info.Mode().IsRegular() || info.Size() > artifacts.MaxBinary {
			writer.Close()
			writeError(w, fail(503, "runtime_backup_failed", "GM运行文件不完整或不是普通文件，未允许更新"), "")
			return
		}
		raw, e := os.ReadFile(path)
		if e != nil {
			writer.Close()
			writeError(w, e, "")
			return
		}
		entry, e := writer.Create(name)
		if e != nil {
			writeError(w, e, "")
			return
		}
		if _, e = entry.Write(raw); e != nil {
			writeError(w, e, "")
			return
		}
		manifest[name] = artifacts.SHA(raw)
	}
	raw, _ := json.Marshal(manifest)
	entry, err := writer.Create("manifest.json")
	if err == nil {
		_, err = entry.Write(raw)
	}
	if err == nil {
		err = writer.Close()
	}
	if err != nil {
		writeError(w, err, "")
		return
	}
	payload := buffer.Bytes()
	w.Header().Set("Content-Type", "application/zip")
	w.Header().Set("Content-Length", strconv.Itoa(len(payload)))
	w.Header().Set("X-Content-SHA256", artifacts.SHA(payload))
	_, _ = w.Write(payload)
}
