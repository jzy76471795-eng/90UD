//go:build windows

package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"

	"newadmin/internal/adminapi"
	"newadmin/internal/managerprofile"
)

func codeStateText(state string) string {
	switch state {
	case "active", "unused":
		return "可使用"
	case "used":
		return "已使用"
	case "revoked":
		return "已停用"
	case "managed_not_published":
		return "仅管理记录"
	}
	return state
}
func (s *serverUI) loadProfile() bool {
	profile, err := managerprofile.Load(localDataDirectory())
	if err != nil {
		if !os.IsNotExist(err) {
			s.output.SetText(fmt.Sprintf("连接配置读取失败：%s\r\n配置位置：%s", err, filepath.Join(localDataDirectory(), managerprofile.FileName)))
			s.status.SetText("配置需检查")
		}
		return false
	}
	s.address.SetText(profile.Address)
	s.certificate.SetText(profile.Certificate)
	s.token.SetText(profile.Token)
	return true
}
func (a *accountUI) databaseBan() {
	row, ok := a.selectedAccount()
	if !ok {
		return
	}
	name, label := "account.ban", "封禁"
	if row.AccountBlocked {
		name, label = "account.unban", "解除账号封禁"
	}
	if !walkConfirm(a.window, "确认"+label, "账号："+row.Name+"（ID "+strconv.Itoa(row.ID)+"）\r\n将直接更新数据库账号封禁记录；不踢当前连接。\r\n是否继续？") {
		return
	}
	var receipt adminapi.Receipt
	managerJob("账号管理", label, func(c *adminapi.Client) error {
		return c.Do(context.Background(), "POST", "/manage/v1/actions", adminapi.NewRequestID(), adminapi.Action{Name: name, Scope: "single", AccountID: row.ID, Confirm: true}, &receipt)
	}, func(err error) {
		if err != nil {
			a.accountInfo.SetText(err.Error())
			return
		}
		a.accountInfo.SetText(receipt.Message)
		logUIAction("账号管理", label, row.Name, receipt.Message)
		a.queryAccounts()
	})
}
