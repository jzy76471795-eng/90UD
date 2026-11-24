//go:build windows

package main

import (
	"dnf90manager/internal/launcherruntime"
	"strings"
	"testing"
)

func TestRuntimeEnvironmentPath(t *testing.T) {
	files := []launcherruntime.File{{Path: "shared/Microsoft.WindowsDesktop.App/6.0.36/PresentationNative_cor3.dll"}}
	short := "E:\\game\\rt"
	long := short + "\\" + strings.Repeat("盘", 177)
	unc := "\\\\pc\\share\\" + strings.Repeat("x", 177)
	supplementary := short + "\\" + strings.Repeat("🌍", 90)
	const prefix = "\\\\?\\"
	for from, want := range map[string]string{short: short, long: prefix + long, unc: prefix + "UNC\\" + unc[2:], supplementary: prefix + supplementary, prefix + long: prefix + long} {
		if got := runtimeEnvironmentPath(from, files); got != want {
			t.Errorf("runtimeEnvironmentPath(%q) = %q, want %q", from, got, want)
		}
	}
}
