//go:build windows

package main

import (
	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
)

type announcementForm struct {
	configRead       configurationReadState
	saveConfigButton *walk.PushButton
	kind             string
	enabled          *walk.CheckBox
	title            *walk.LineEdit
	interval         *walk.NumberEdit
	content, output  *walk.TextEdit
	revision         int64
}

func announcementPage() decl.TabPage {
	forms := []*announcementForm{{kind: "游戏导航栏公告"}, {kind: "游戏聊天框公告"}, {kind: "登陆器公告"}}
	pages := make([]decl.TabPage, len(forms))
	for i, form := range forms {
		configurationLoads = append(configurationLoads, form.load)
		pages[i] = form.page()
	}
	return decl.TabPage{
		Title: "公告管理", Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}, Spacing: 8},
		Children: []decl.Widget{
			decl.Label{Text: "保存后写入正式数据库；启用的轮播公告由Go服务排队发送，登录器公告在玩家下次刷新时读取。"},
			decl.TabWidget{ContentMarginsZero: true, Pages: pages},
		},
	}
}

func (f *announcementForm) page() decl.TabPage {
	controls := []decl.Widget{decl.CheckBox{AssignTo: &f.enabled, Text: "启用公告", OnCheckedChanged: f.changed}}
	if f.kind == "登陆器公告" {
		controls = append(controls, decl.Label{Text: "标题："}, decl.LineEdit{AssignTo: &f.title, MinSize: decl.Size{Width: 240}, MaxSize: decl.Size{Width: 240}, MaxLength: 100, CueBanner: "登陆器公告标题", OnTextChanged: f.changed})
	} else {
		label, value := "轮播间隔（秒）：", 30.0
		if f.kind == "游戏聊天框公告" {
			label, value = "发送间隔（分钟）：", 10
		}
		controls = append(controls, decl.Label{Text: label}, decl.NumberEdit{AssignTo: &f.interval, MinValue: 1, MaxValue: 86400, Value: value, MinSize: decl.Size{Width: 100}, MaxSize: decl.Size{Width: 100}, OnValueChanged: f.changed})
	}
	controls = append(controls, decl.HSpacer{})
	tabTitle := f.kind
	if f.kind == "游戏聊天框公告" {
		tabTitle = "聊天公告（浮层投递）"
	}
	return decl.TabPage{
		Title: tabTitle, Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 10},
		Children: []decl.Widget{
			decl.Label{Text: "当前玩家登录器显示为游戏浮层；原生聊天窗口投递尚需游戏协议接入。", Visible: f.kind == "游戏聊天框公告"},
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 10}, Children: controls},
			decl.GroupBox{Title: "公告内容", MinSize: decl.Size{Height: 150}, MaxSize: decl.Size{Height: 190}, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, Children: []decl.Widget{decl.TextEdit{AssignTo: &f.content, VScroll: true, MaxLength: 4000, OnTextChanged: f.changed}}},
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true, Spacing: 8},
				Children: []decl.Widget{
					decl.PushButton{AssignTo: &f.saveConfigButton, Enabled: false, Text: "保存并应用", OnClicked: f.save},
					decl.PushButton{Text: "立即发送一次", Visible: f.kind != "登陆器公告", OnClicked: f.sendNow},
					decl.PushButton{Text: "读取已保存配置", OnClicked: f.load}, decl.PushButton{Text: "查看轮播状态", Visible: f.kind != "登陆器公告", OnClicked: func() { showJobStatus(f.section(), f.output) }}, decl.HSpacer{},
				},
			},
			decl.GroupBox{Title: "公告操作结果", StretchFactor: 1, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, Children: []decl.Widget{decl.TextEdit{AssignTo: &f.output, ReadOnly: true, VScroll: true, Text: "填写公告内容后点击“保存并应用”，或读取已保存配置。"}}},
		},
	}
}

func (f *announcementForm) changed() {
	if f.output != nil {
		f.output.SetText("公告设置已修改，点击“保存并应用”后生效。")
	}
}
