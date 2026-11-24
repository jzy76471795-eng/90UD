//go:build windows

package downloader

import "golang.org/x/sys/windows/registry"

func systemProxySettings() (server, bypass string) {
	key, err := registry.OpenKey(registry.CURRENT_USER, `Software\Microsoft\Windows\CurrentVersion\Internet Settings`, registry.QUERY_VALUE)
	if err != nil {
		return "", ""
	}
	defer key.Close()
	enabled, _, err := key.GetIntegerValue("ProxyEnable")
	if err != nil || enabled == 0 {
		return "", ""
	}
	server, _, _ = key.GetStringValue("ProxyServer")
	bypass, _, _ = key.GetStringValue("ProxyOverride")
	return
}
