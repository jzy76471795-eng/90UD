//go:build windows

package main

import (
	"strings"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
)

type welfareForm struct {
	configRead       configurationReadState
	saveConfigButton *walk.PushButton
	kind             string
	enabled          *walk.CheckBox
	clockTimes       *walk.LineEdit
	rewards          *rewardEditor
	output           *walk.TextEdit
	revision         int64
}

func welfarePage() decl.TabPage {
	forms := []*welfareForm{{kind: "定时福利"}, {kind: "每周奖励"}, {kind: "每月奖励"}}
	pages := make([]decl.TabPage, len(forms))
	for i, form := range forms {
		configurationLoads = append(configurationLoads, form.load)
		form.rewards = newRewardEditor(0, form.changed)
		pages[i] = form.page()
	}
	return decl.TabPage{
		Title: "在线福利", Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}, Spacing: 8},
		Children: []decl.Widget{
			decl.Label{Text: "定时福利按北京时间发给当时在线的每个角色。每周和每月奖励按账号发放。"},
			decl.TabWidget{ContentMarginsZero: true, Pages: pages},
		},
	}
}

func (f *welfareForm) page() decl.TabPage {
	controls := []decl.Widget{decl.CheckBox{AssignTo: &f.enabled, Text: "启用" + f.kind, OnCheckedChanged: f.changed}}
	if f.kind == "定时福利" {
		controls = append(controls, decl.Label{Text: "北京时间"}, decl.LineEdit{AssignTo: &f.clockTimes, CueBanner: "例如 12:00,20:30", MinSize: decl.Size{Width: 240}, OnTextChanged: f.changed})
	} else {
		controls = append(controls, decl.Label{Text: "奖励周期：" + strings.TrimSuffix(f.kind, "奖励")}, decl.Label{Text: "在线账号每周期一次，物品寄给当前角色"})
	}
	controls = append(controls, decl.HSpacer{})
	return decl.TabPage{
		Title: f.kind, Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 10},
		Children: []decl.Widget{
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 10}, Children: controls},
			f.rewards.widget(),
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.PushButton{AssignTo: &f.saveConfigButton, Enabled: false, Text: "保存管理配置", OnClicked: f.save}, decl.PushButton{Text: "读取已保存配置", OnClicked: f.load}, decl.PushButton{Text: "查看执行状态", OnClicked: func() { showJobStatus(f.section(), f.output) }}, decl.HSpacer{}}},
			decl.GroupBox{Title: "福利配置与执行结果", StretchFactor: 1, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, Children: []decl.Widget{decl.TextEdit{AssignTo: &f.output, ReadOnly: true, VScroll: true, Text: "设置启用状态与奖励后，点击“保存管理配置”使规则生效。"}}},
		},
	}
}

func (f *welfareForm) changed() {
	if f.output != nil {
		f.output.SetText("福利设置已修改，保存管理配置后生效。")
	}
}
