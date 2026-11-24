package downloader

import (
	"errors"
	"net"
	"net/http"
	"net/url"
	"os"
	"path"
	"strings"
)

func downloadProxy(req *http.Request) (*url.URL, error) {
	proxy, err := http.ProxyFromEnvironment(req)
	if err != nil || proxy != nil {
		return proxy, err
	}
	key := "HTTP_PROXY"
	if req.URL.Scheme == "https" {
		key = "HTTPS_PROXY"
	}
	if environmentValue(key) != "" || matchesNoProxy(req.URL, environmentValue("NO_PROXY")) {
		return nil, nil
	}
	server, bypass := systemProxySettings()
	return proxyFromSettings(req, server, bypass)
}

func environmentValue(key string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return os.Getenv(strings.ToLower(key))
}

func matchesNoProxy(u *url.URL, list string) bool {
	host := strings.ToLower(u.Hostname())
	ip := net.ParseIP(host)
	port := u.Port()
	if port == "" {
		port = "80"
		if u.Scheme == "https" {
			port = "443"
		}
	}
	for _, item := range strings.Split(strings.ToLower(list), ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		if item == "*" {
			return true
		}
		if _, block, err := net.ParseCIDR(item); err == nil {
			if ip != nil && block.Contains(ip) {
				return true
			}
			continue
		}
		if name, wantedPort, err := net.SplitHostPort(item); err == nil {
			if wantedPort != port {
				continue
			}
			item = name
		}
		item = strings.Trim(item, "[]")
		if candidate := net.ParseIP(item); candidate != nil {
			if ip != nil && candidate.Equal(ip) {
				return true
			}
			continue
		}
		item = strings.TrimPrefix(item, "*.")
		if strings.HasPrefix(item, ".") {
			if strings.HasSuffix(host, item) {
				return true
			}
		} else if host == item || strings.HasSuffix(host, "."+item) {
			return true
		}
	}
	return false
}

func proxyFromSettings(req *http.Request, server, bypass string) (*url.URL, error) {
	host := strings.ToLower(req.URL.Hostname())
	if server == "" || host == "localhost" {
		return nil, nil
	}
	if ip := net.ParseIP(host); ip != nil && ip.IsLoopback() {
		return nil, nil
	}
	for _, pattern := range strings.Split(strings.ToLower(bypass), ";") {
		pattern = strings.TrimSpace(pattern)
		if pattern == "" {
			continue
		}
		if pattern == "<local>" && !strings.Contains(host, ".") {
			return nil, nil
		}
		for _, candidate := range []string{host, strings.ToLower(req.URL.Host)} {
			if matched, _ := path.Match(pattern, candidate); matched {
				return nil, nil
			}
		}
	}
	selected, protocol := "", "http"
	if !strings.Contains(server, "=") {
		selected = strings.TrimSpace(server)
	} else {
		socks := ""
		for _, entry := range strings.Split(server, ";") {
			pair := strings.SplitN(entry, "=", 2)
			if len(pair) != 2 {
				continue
			}
			key, value := strings.ToLower(strings.TrimSpace(pair[0])), strings.TrimSpace(pair[1])
			if key == req.URL.Scheme {
				selected = value
			}
			if key == "socks" {
				socks = value
			}
		}
		if selected == "" && socks != "" {
			selected, protocol = socks, "socks5"
		}
	}
	if selected == "" {
		return nil, nil
	}
	if !strings.Contains(selected, "://") {
		selected = protocol + "://" + selected
	}
	proxy, err := url.Parse(selected)
	if err != nil || proxy.Hostname() == "" || proxy.Path != "" || proxy.RawQuery != "" || proxy.Fragment != "" || (proxy.Scheme != "http" && proxy.Scheme != "https" && proxy.Scheme != "socks5" && proxy.Scheme != "socks5h") {
		return nil, errors.New("系统代理地址无效，请检查系统网络代理")
	}
	return proxy, nil
}
