//go:build windows

package main

import (
	"context"
	"fmt"
	"net/url"
	"strings"
	"syscall"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
	"github.com/lxn/win"
	"newadmin/internal/adminapi"
)

type rechargeUI struct {
	configRead       configurationReadState
	saveConfigButton *walk.PushButton
	enabled          *walk.CheckBox
	url              *walk.LineEdit
	output           *walk.TextEdit
	revision         int64
}

func rechargePage() decl.TabPage {
	f := &rechargeUI{}
	configurationLoads = append(configurationLoads, f.load)
	return decl.TabPage{Title: "充值设置", Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 10}, Children: []decl.Widget{
		decl.Label{Text: "设置全服统一的点券充值页面。保存后，游戏充值入口读取最新设置。"},
		decl.CheckBox{AssignTo: &f.enabled, Text: "开启充值入口", OnCheckedChanged: f.changed},
		decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{
			decl.Label{Text: "充值页面链接："}, decl.LineEdit{AssignTo: &f.url, StretchFactor: 1, MaxLength: 2048, CueBanner: "https://你的充值网站/", OnTextChanged: f.changed},
		}},
		decl.Label{Text: "填写完整的 http:// 或 https:// 地址。关闭入口后，充值页面显示暂未开放。"},
		decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{
			decl.PushButton{Text: "读取已保存配置", OnClicked: f.load}, decl.PushButton{AssignTo: &f.saveConfigButton, Enabled: false, Text: "保存管理配置", OnClicked: f.save}, decl.PushButton{Text: "测试打开", OnClicked: f.testOpen}, decl.HSpacer{},
		}},
		decl.GroupBox{Title: "当前配置与保存结果", StretchFactor: 1, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, Children: []decl.Widget{
			decl.TextEdit{AssignTo: &f.output, ReadOnly: true, VScroll: true, Text: "连接管理服务后读取当前充值设置。"},
		}},
	}}
}

func (f *rechargeUI) changed() {
	if f.output != nil {
		f.output.SetText("设置已修改，保存后生效。")
	}
}
func (f *rechargeUI) load() {
	sequence := f.configRead.begin(f.saveConfigButton)
	f.revision = 0
	var entry adminapi.ConfigEntry
	managerJob("充值设置", "读取充值设置", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "GET", "/manage/v1/config/recharge", "", nil, &entry)
	}, func(err error) {
		if sequence != f.configRead.sequence {
			return
		}
		if err != nil {
			f.output.SetText(err.Error())
			return
		}
		if !f.configRead.accept(sequence, currentManager(), f.saveConfigButton) {
			return
		}
		f.revision = entry.Revision
		f.enabled.SetChecked(boolValue(entry.Value, "enabled"))
		f.url.SetText(stringValue(entry.Value, "url"))
		f.showEntry(entry, "已读取")
	})
}
func (f *rechargeUI) showEntry(entry adminapi.ConfigEntry, prefix string) {
	state := "关闭"
	if boolValue(entry.Value, "enabled") {
		state = "开启"
	}
	link := stringValue(entry.Value, "url")
	if link == "" {
		link = "尚未配置"
	}
	f.output.SetText(fmt.Sprintf("%s充值配置，版本：%d\r\n当前开关：%s\r\n当前生效链接：%s", prefix, entry.Revision, state, link))
}
func rechargeLink(raw string, required bool) (string, error) {
	link := strings.TrimSpace(raw)
	if link == "" && !required {
		return "", nil
	}
	u, err := url.Parse(link)
	if err != nil || u.Hostname() == "" || u.User != nil || (u.Scheme != "http" && u.Scheme != "https") || len(link) > 2048 || strings.ContainsAny(link, "\r\n\x00") {
		return "", fmt.Errorf("请输入完整的 http:// 或 https:// 充值网址。")
	}
	return link, nil
}
func (f *rechargeUI) save() {
	if err := f.configRead.check(currentManager()); err != nil {
		f.output.SetText(err.Error())
		return
	}
	link, err := rechargeLink(f.url.Text(), f.enabled.Checked())
	if err != nil {
		f.output.SetText(err.Error())
		return
	}
	value := map[string]any{"enabled": f.enabled.Checked(), "url": link}
	revision := f.revision
	var entry adminapi.ConfigEntry
	saveSequence := f.configRead.saveStarted(f.saveConfigButton)
	managerJob("充值设置", "保存充值设置", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "PUT", "/manage/v1/config/recharge", adminapi.NewRequestID(), map[string]any{"revision": revision, "value": value}, &entry)
	}, func(err error) {
		if !f.configRead.saveFinished(saveSequence, currentManager(), err, f.saveConfigButton) {
			f.output.SetText("保存期间连接或读取状态已变化，请到对应连接重新读取确认。")
			return
		}
		if err != nil {
			f.output.SetText(err.Error())
			return
		}
		f.revision = entry.Revision
		f.showEntry(entry, "已保存")
		logUIAction("充值设置", "保存充值设置", "全服充值入口", fmt.Sprintf("已提交数据库，版本%d", entry.Revision))
	})
}
func (f *rechargeUI) testOpen() {
	link, err := rechargeLink(f.url.Text(), true)
	if err != nil {
		f.output.SetText(err.Error())
		return
	}
	file, err := syscall.UTF16PtrFromString(link)
	if err != nil {
		f.output.SetText(err.Error())
		return
	}
	if !win.ShellExecute(0, nil, file, nil, nil, win.SW_SHOWNORMAL) {
		f.output.SetText("无法打开默认浏览器，请检查系统的浏览器设置。")
		return
	}
	f.output.SetText("已在默认浏览器测试打开当前填写的网址。")
}
