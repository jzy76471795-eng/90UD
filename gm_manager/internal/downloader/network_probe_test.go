package downloader

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptrace"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestOptionalRealDownloadAndResume(t *testing.T) {
	configPath := os.Getenv("GM_DOWNLOAD_REAL_CONFIG")
	if configPath == "" {
		t.Skip("set a public download configuration for a bounded real transfer")
	}
	raw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	var config Config
	if err := json.Unmarshal(raw, &config); err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	req, _ := http.NewRequest(http.MethodGet, config.URL, nil)
	selected, proxyErr := downloadProxy(req)
	if selected != nil {
		t.Log("proxy:", selected.Hostname(), selected.Port())
	} else {
		t.Log("proxy: direct", proxyErr)
	}
	var saved, total int64
	phases := []map[string]int64{}
	for i := 0; i < 2; i++ {
		base := saved
		ctx, cancel := context.WithTimeout(context.Background(), 55*time.Second)
		ctx = httptrace.WithClientTrace(ctx, &httptrace.ClientTrace{
			GetConn:      func(host string) { t.Log("request host:", host) },
			ConnectStart: func(network, address string) { t.Log("connecting:", network, address) },
			TLSHandshakeDone: func(state tls.ConnectionState, err error) {
				t.Log("TLS:", state.ServerName, state.NegotiatedProtocol, err)
			},
		})
		var first, latest int64
		_, err = (Engine{RetryDelay: time.Second}).Run(ctx, config, root, func(p Progress) {
			if p.Stage != "正在下载" {
				return
			}
			if first == 0 {
				first = p.Done
			}
			latest, total = p.Done, p.Total
			if p.Done >= base+(1<<20) {
				cancel()
			}
		})
		cancel()
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("phase %d did not receive the bounded transfer: %v", i+1, err)
		}
		parts, err := filepath.Glob(filepath.Join(root, "*.part"))
		if err != nil || len(parts) != 1 {
			t.Fatal("partial download missing", err)
		}
		info, err := os.Stat(parts[0])
		if err != nil {
			t.Fatal(err)
		}
		saved = info.Size()
		if saved <= base || latest < base+(1<<20) {
			t.Fatal("real download did not advance", saved, base, latest)
		}
		if i > 0 && first <= base {
			t.Fatal("remote server did not honor resume", first, base)
		}
		phases = append(phases, map[string]int64{"initial_bytes": base, "first_progress_bytes": first, "saved_bytes": saved})
	}
	report := map[string]any{"real_download_started": true, "real_resume_verified": true, "remote_file_bytes": total, "bounded_local_bytes": saved, "phases": phases, "whole_client_downloaded": false}
	if output := os.Getenv("GM_DOWNLOAD_PROBE_REPORT"); output != "" {
		full, err := filepath.Abs(output)
		if err != nil || !strings.HasPrefix(strings.ToLower(full), `e:\codex\`) {
			t.Fatal("probe report must stay in E:\\codex")
		}
		raw, _ := json.MarshalIndent(report, "", "  ")
		if err = os.WriteFile(full, raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	t.Logf("real transfer and resume verified: %d / %d bytes", saved, total)
}
