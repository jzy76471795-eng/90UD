//go:build windows

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"newadmin/internal/adminapi"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
)

func deployUsingWinRM(ctx context.Context, c *adminapi.Client, source, newSHA, id string, backup managerBackup, out *adminapi.Receipt) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	base := filepath.Dir(exe)
	python := filepath.Join(base, "运维运行时", "python", "python.exe")
	helper := filepath.Join(base, "运维工具", "gm_deploy.py")
	if _, err = os.Stat(python); err != nil {
		return fmt.Errorf("缺少本机运维运行时，请使用完整交付目录")
	}
	if _, err = os.Stat(helper); err != nil {
		return fmt.Errorf("缺少本机加密WinRM运维工具")
	}
	target, err := url.Parse(c.Endpoint())
	if err != nil {
		return err
	}
	raw, _ := json.Marshal(map[string]string{"candidate": source, "backup": backup.Directory, "host": target.Hostname(), "id": id, "old_sha256": backup.BinarySHA, "new_sha256": newSHA})
	command := exec.CommandContext(ctx, python, "-B", "-X", "utf8", helper)
	command.Dir = base
	command.Stdin = bytes.NewReader(raw)
	command.Stderr = nil
	command.Env = append(os.Environ(), "PYTHONDONTWRITEBYTECODE=1", "TEMP="+filepath.Join(localDataDirectory(), "tmp"), "TMP="+filepath.Join(localDataDirectory(), "tmp"))
	_ = os.MkdirAll(filepath.Join(localDataDirectory(), "tmp"), 0700)
	command.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	response, runErr := command.Output()
	var reply struct {
		OK      bool             `json:"ok"`
		Receipt adminapi.Receipt `json:"receipt"`
		Error   string           `json:"error"`
	}
	if json.Unmarshal(response, &reply) != nil || runErr != nil || !reply.OK {
		return fmt.Errorf("加密WinRM部署未完成，请查询原部署回执（%s）", id)
	}
	*out = reply.Receipt
	out.Message = "GM程序部署完成；TLS和实际进程已核对，游戏服务未重启。"
	return nil
}
