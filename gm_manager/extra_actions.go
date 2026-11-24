//go:build windows

package main

import (
	"context"
	"fmt"
	"newadmin/internal/adminapi"
)

func (c *cdkUI) setCodeEnabled(enabled bool) {
	if currentManager() == nil {
		c.output.SetText("请先连接管理服务。")
		return
	}
	index := c.table.CurrentIndex()
	if index < 0 || index >= len(c.model.rows) {
		c.output.SetText("请先选择兑换码。")
		return
	}
	row := c.model.rows[index]
	name := "停用"
	if enabled {
		name = "恢复"
	}
	if !walkConfirm(applicationWindow, name+"兑换码", name+" "+row.Code+"？已兑换记录会保留。") {
		return
	}
	var receipt adminapi.Receipt
	managerJob("CDK管理", name, func(client *adminapi.Client) error {
		return client.Do(context.Background(), "POST", "/manage/v1/codes/state", adminapi.NewRequestID(), map[string]any{"kind": "cdk", "code": row.Code, "enabled": enabled, "confirm": true}, &receipt)
	}, func(err error) {
		if err != nil {
			c.output.SetText(err.Error())
			return
		}
		c.output.SetText(receipt.Message)
		c.queryManaged()
	})
}
func (f *announcementForm) sendNow() {
	if f.title != nil {
		f.output.SetText("登录器公告使用保存并应用，玩家刷新时读取。")
		return
	}
	content := f.content.Text()
	if !walkConfirm(applicationWindow, "立即发送公告", fmt.Sprintf("将向当前玩家登录器公告队列发送：\r\n%s", content)) {
		return
	}
	var receipt adminapi.Receipt
	managerJob("公告管理", "立即发送", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "POST", "/manage/v1/announcements", adminapi.NewRequestID(), map[string]any{"section": f.section(), "content": content, "confirm": true}, &receipt)
	}, func(err error) {
		if err != nil {
			f.output.SetText(err.Error())
			return
		}
		f.output.SetText(receipt.Message)
	})
}
