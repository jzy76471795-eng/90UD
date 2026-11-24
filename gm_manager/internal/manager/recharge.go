package manager

import (
	"context"
	"net/http"
	"net/url"
	"strings"
	"time"
)

func validateRecharge(value map[string]any) error {
	enabled, ok := value["enabled"].(bool)
	link, linkOK := value["url"].(string)
	if len(value) != 2 || !ok || !linkOK || len(link) > 2048 || link != strings.TrimSpace(link) || strings.ContainsAny(link, "\r\n\x00") {
		return fail(400, "invalid_config", "充值设置需要入口开关和有效网址")
	}
	if link == "" {
		if enabled {
			return fail(400, "invalid_config", "开启充值入口需要填写网址")
		}
		return nil
	}
	u, err := url.Parse(link)
	if err != nil || u.Hostname() == "" || u.User != nil || (u.Scheme != "http" && u.Scheme != "https") {
		return fail(400, "invalid_config", "充值地址必须为完整的http或https网址")
	}
	return nil
}

// Each visit reads the committed setting; no account data is included in URLs.
func (l *launcherService) serveRecharge(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Referrer-Policy", "no-referrer")
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if r.TLS == nil {
		w.WriteHeader(http.StatusUpgradeRequired)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	entry, err := readConfig(ctx, l.manager.store.db, "recharge")
	if err == nil && validateRecharge(entry.Value) == nil && entry.Value["enabled"] == true {
		http.Redirect(w, r, entry.Value["url"].(string), http.StatusFound)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
	w.WriteHeader(http.StatusServiceUnavailable)
	if r.Method != http.MethodHead {
		_, _ = w.Write([]byte("<!doctype html><meta charset=utf-8><title>充值入口</title><main style='font:18px sans-serif;margin:15vh auto;max-width:36em;padding:24px'><h1>充值暂未开放</h1><p>请稍后重试，或联系管理员。</p></main>"))
	}
}
