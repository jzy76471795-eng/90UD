//go:build windows

package manager

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
)

func (s *Service) runSystem(ctx context.Context, command string) error {
	args := []string{"/a"}
	if command == "reboot" {
		args = []string{"/r", "/t", "30", "/d", "p:4:1", "/c", "90US GM requested restart"}
	} else if command != "cancel_reboot" {
		return fail(400, "invalid_system_command", "系统命令无效")
	}
	cmd := exec.CommandContext(ctx, filepath.Join(os.Getenv("SystemRoot"), "System32", "shutdown.exe"), args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	if cmd.Run() != nil {
		return fail(502, "system_command_failed", "Windows没有确认重启/取消命令，请核对系统状态和权限")
	}
	return nil
}
