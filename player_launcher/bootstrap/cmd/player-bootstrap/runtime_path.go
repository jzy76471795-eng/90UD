//go:build windows

package main

import (
	"dnf90manager/internal/launcherruntime"
	"path/filepath"
	"strings"
	"unicode/utf16"
)

// WPF probes additional native library names before creating the first window.
// Give its loader a long-path-capable name when a verified member approaches MAX_PATH.
func runtimeEnvironmentPath(root string, files []launcherruntime.File) string {
	const extended = "\\\\?\\"
	if strings.HasPrefix(root, extended) {
		return root
	}
	for _, file := range files {
		path := filepath.Join(root, filepath.FromSlash(file.Path))
		if len(utf16.Encode([]rune(path))) < 240 {
			continue
		}
		if strings.HasPrefix(root, "\\\\") {
			return extended + "UNC\\" + root[2:]
		}
		return extended + root
	}
	return root
}
