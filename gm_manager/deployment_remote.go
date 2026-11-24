//go:build windows

package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"golang.org/x/sys/windows"
	_ "modernc.org/sqlite"
	"net/url"
	"newadmin/internal/adminapi"
	"os"
	"path/filepath"
	"time"
)

type managerBackup struct {
	RuntimeSHA  string `json:"runtime_sha256"`
	Directory   string `json:"directory"`
	BinarySHA   string `json:"binary_sha256"`
	DatabaseSHA string `json:"database_sha256"`
	ConfigSHA   string `json:"config_sha256"`
	Endpoint    string `json:"endpoint"`
}

func privateDirectory(path string) error {
	if err := os.MkdirAll(path, 0700); err != nil {
		return err
	}
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		return err
	}
	sd, err := windows.SecurityDescriptorFromString("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;" + user.User.Sid.String() + ")")
	if err != nil {
		return err
	}
	acl, _, err := sd.DACL()
	if err != nil {
		return err
	}
	return windows.SetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, nil, nil, acl, nil)
}
func checkBackupDatabase(path string) error {
	uri := url.URL{Scheme: "file", Path: "/" + filepath.ToSlash(path)}
	q := uri.Query()
	q.Set("mode", "ro")
	q.Add("_pragma", "query_only(1)")
	uri.RawQuery = q.Encode()
	db, err := sql.Open("sqlite", uri.String())
	if err != nil {
		return err
	}
	defer db.Close()
	var integrity string
	if err = db.QueryRow("PRAGMA integrity_check").Scan(&integrity); err != nil || integrity != "ok" {
		return fmt.Errorf("本机数据库备份完整性检查失败")
	}
	rows, err := db.Query("PRAGMA foreign_key_check")
	if err != nil {
		return err
	}
	defer rows.Close()
	if rows.Next() {
		return fmt.Errorf("本机数据库备份外键检查失败")
	}
	return rows.Err()
}
func backupManager(ctx context.Context, c *adminapi.Client) (managerBackup, error) {
	result := managerBackup{Endpoint: c.Endpoint(), Directory: filepath.Join(localDataDirectory(), "本机备份", time.Now().In(chinaTime).Format("20060102_150405")+"_"+adminapi.NewRequestID())}
	if err := privateDirectory(result.Directory); err != nil {
		return result, err
	}
	var err error
	if result.BinarySHA, err = c.Download(ctx, "/manage/v1/deployment/binary", filepath.Join(result.Directory, "gmserver.exe")); err != nil {
		return result, err
	}
	if result.ConfigSHA, err = c.Download(ctx, "/manage/v1/deployment/config", filepath.Join(result.Directory, "go-manager.json")); err != nil {
		return result, err
	}
	runtimePath := filepath.Join(result.Directory, "gm-runtime.zip")
	if result.RuntimeSHA, err = c.Download(ctx, "/manage/v1/deployment/runtime", runtimePath); err != nil {
		return result, err
	}
	if err = checkRuntimeArchive(runtimePath); err != nil {
		return result, err
	}
	database := filepath.Join(result.Directory, "players.db")
	if result.DatabaseSHA, err = c.Download(ctx, "/manage/v1/database/snapshot", database); err != nil {
		return result, err
	}
	if err = checkBackupDatabase(database); err != nil {
		return result, err
	}
	raw, _ := json.MarshalIndent(result, "", "  ")
	err = os.WriteFile(filepath.Join(result.Directory, "备份校验.json"), raw, 0600)
	return result, err
}
func (d *deploymentUI) backup() {
	d.button.SetEnabled(false)
	var backup managerBackup
	managerJob("一键部署", "下载本机一致备份", func(c *adminapi.Client) error {
		var err error
		backup, err = backupManager(context.Background(), c)
		return err
	}, func(err error) {
		d.button.SetEnabled(true)
		if err != nil {
			d.output.SetText(err.Error())
			return
		}
		d.output.SetText("旧GM程序、配置和正式数据库已下载到本机并校验：\r\n" + backup.Directory + "\r\n服务器没有持久备份文件。")
	})
}
func (d *deploymentUI) deploy() {
	source := d.source.Text()
	newSHA, err := sha256File(source)
	if err != nil {
		d.output.SetText("新GM程序不可读。")
		return
	}
	if !walkConfirm(applicationWindow, "确认更新独立GM服务", "候选程序："+source+"\r\nSHA256："+newSHA+"\r\n先下载本机备份并校验，再重启独立GM进程。游戏不会重启。\r\n是否执行？") {
		return
	}
	if _, err = os.Stat(d.pendingPath()); err == nil {
		d.output.SetText("有待核对的部署请求，请先查询原部署回执。")
		return
	}
	d.button.SetEnabled(false)
	id := adminapi.NewRequestID()
	var receipt adminapi.Receipt
	managerJob("一键部署", "更新独立GM", func(c *adminapi.Client) error {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
		defer cancel()

		backup, err := backupManager(ctx, c)
		if err != nil {
			return err
		}
		raw, _ := json.Marshal(map[string]string{"id": id, "endpoint": c.Endpoint(), "backup": backup.Directory})
		if err = os.WriteFile(d.pendingPath(), raw, 0600); err != nil {
			return err
		}
		if err = deployUsingWinRM(ctx, c, source, newSHA, id, backup, &receipt); err != nil {
			return err
		}
		return waitDeployment(ctx, c, id, &receipt)
	}, func(err error) {
		d.button.SetEnabled(true)
		if err != nil {
			d.output.SetText(err.Error() + "\r\n请求：" + id + "\r\n请查询原部署回执。")
			return
		}
		_ = os.Remove(d.pendingPath())
		d.output.SetText(receipt.Message + "\r\n请求：" + id)
		logUIAction("一键部署", "更新GM", newSHA, receipt.Message)
	})
}
func waitDeployment(ctx context.Context, c *adminapi.Client, id string, receipt *adminapi.Receipt) error {
	deadline := time.Now().Add(90 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Second):
		}
		if err := c.Do(ctx, "GET", "/manage/v1/receipts/"+id, "", nil, receipt); err != nil {
			continue
		}
		if receipt.State == "confirmed" {
			return nil
		}
		if receipt.State == "failed" {
			return fmt.Errorf("%s", receipt.Message)
		}
	}
	return fmt.Errorf("更新结果未确认，请保留请求编号查询")
}
func (d *deploymentUI) pendingPath() string {
	return filepath.Join(localDataDirectory(), "pending-deployment.json")
}
func (d *deploymentUI) receipt() {
	raw, err := os.ReadFile(d.pendingPath())
	if err != nil {
		d.output.SetText("没有本机待核对部署请求。")
		return
	}
	var pending map[string]string
	if json.Unmarshal(raw, &pending) != nil {
		d.output.SetText("请求记录无效，请保留文件核对。")
		return
	}
	if currentManager() == nil || currentManager().Endpoint() != pending["endpoint"] {
		d.output.SetText("请连接原管理地址：" + pending["endpoint"])
		return
	}
	var receipt adminapi.Receipt
	managerJob("一键部署", "查询原部署回执", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "GET", "/manage/v1/receipts/"+pending["id"], "", nil, &receipt)
	}, func(err error) {
		if err != nil {
			if failure, ok := err.(*adminapi.ClientError); ok && failure.Status == 404 && !failure.Unknown {
				_ = os.Remove(d.pendingPath())
				d.output.SetText("原部署请求未登记成功，本机待核对标记已解除；核对程序状态后可重新部署。")
				return
			}
			d.output.SetText(err.Error())
			return
		}
		if receipt.State == "confirmed" || receipt.State == "failed" {
			_ = os.Remove(d.pendingPath())
		}
		d.output.SetText(receipt.Message + "\r\n状态：" + receipt.State + "\r\n本机备份：" + pending["backup"])
	})
}
