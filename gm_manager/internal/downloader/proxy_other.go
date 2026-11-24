//go:build !windows

package downloader

func systemProxySettings() (string, string) { return "", "" }
