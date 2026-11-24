//go:build windows

package main

import (
	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
)

type grantForm struct {
	owner                 *accountUI
	title                 string
	characterTarget, mail bool
	scope                 *walk.ComboBox
	target                *walk.LineEdit
	body, output          *walk.TextEdit
	amount                *walk.NumberEdit
	rewards               *rewardEditor
	hint                  *walk.Label
	send                  *walk.PushButton
}

func (f *grantForm) scopeNames() []string {
	if f.characterTarget {
		return []string{"指定角色", "所有角色", "所有在线角色"}
	}
	return []string{"指定账号", "所有账号", "在线账号"}
}

func (f *grantForm) targetKind() string {
	if f.characterTarget {
		return "角色"
	}
	return "账号"
}

func (f *grantForm) page() decl.TabPage {
	children := []decl.Widget{
		decl.Label{Text: "支持直接写入数据库：支持点券、代币、角色金币、多附件邮件及批量发放。在线资产显示可能需要重新登录。"},
		decl.Composite{
			Layout: decl.HBox{MarginsZero: true, Spacing: 8},
			Children: []decl.Widget{
				decl.Label{Text: "发放范围：", MinSize: decl.Size{Width: 80}},
				decl.ComboBox{AssignTo: &f.scope, Model: f.scopeNames(), CurrentIndex: 0, MinSize: decl.Size{Width: 180}, MaxSize: decl.Size{Width: 220}, OnCurrentIndexChanged: f.inputsChanged},
				decl.HSpacer{},
			},
		},
		decl.Composite{
			Layout: decl.HBox{MarginsZero: true, Spacing: 8},
			Children: []decl.Widget{
				decl.Label{Text: "目标" + f.targetKind() + "：", MinSize: decl.Size{Width: 80}},
				decl.LineEdit{AssignTo: &f.target, StretchFactor: 1, CueBanner: "填写" + f.targetKind() + "ID / 名称，或从查询列表右键跳转", OnTextChanged: f.inputsChanged},
			},
		},
		decl.Label{AssignTo: &f.hint, Text: "请先选择目标"},
	}
	if f.mail {
		children = append(children,
			decl.Composite{
				MinSize: decl.Size{Height: 50}, MaxSize: decl.Size{Height: 50},
				Layout: decl.HBox{MarginsZero: true, Spacing: 8},
				Children: []decl.Widget{
					decl.Label{Text: "邮件内容：", MinSize: decl.Size{Width: 80}},
					decl.TextEdit{AssignTo: &f.body, StretchFactor: 1, VScroll: true, OnTextChanged: f.inputsChanged},
				},
			},
			f.rewards.widget(),
		)
	} else {
		children = append(children, decl.Composite{
			Layout: decl.HBox{MarginsZero: true, Spacing: 8},
			Children: []decl.Widget{
				decl.Label{Text: "增加数量：", MinSize: decl.Size{Width: 80}},
				decl.NumberEdit{AssignTo: &f.amount, MinValue: 1, MaxValue: 2147483647, Value: 1.0, MinSize: decl.Size{Width: 200}, OnValueChanged: f.inputsChanged},
				decl.Label{Text: "每个目标分别增加该数量"}, decl.HSpacer{},
			},
		})
	}
	children = append(children,
		decl.Composite{
			Layout: decl.HBox{MarginsZero: true, Spacing: 8},
			Children: []decl.Widget{
				decl.PushButton{AssignTo: &f.send, Text: "确认并提交", OnClicked: f.execute},
				decl.PushButton{Text: "查询原回执", OnClicked: f.queryReceipt},
				decl.PushButton{Text: "清空结果", OnClicked: func() { f.output.SetText("") }},
				decl.HSpacer{},
			},
		},
		decl.GroupBox{
			Title: "发放结果", Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, StretchFactor: 1,
			Children: []decl.Widget{decl.TextEdit{AssignTo: &f.output, ReadOnly: true, VScroll: true, Text: "填写目标和奖励后，点击“确认并提交”执行发放。"}},
		},
	)
	return decl.TabPage{Title: f.title, Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 10}, Children: children}
}

func (f *grantForm) inputsChanged() {
	if f.scope == nil || f.target == nil || f.hint == nil {
		return
	}
	f.target.SetEnabled(f.scope.CurrentIndex() == 0)
	if f.output != nil {
		f.output.SetText("填写目标和奖励后，点击“确认并提交”执行发放。")
	}
	if currentManager() != nil {
		f.hint.SetText("可按ID或名称选择；数字名称有重合时用 id:数字 / name:名称，右键跳转会自动填写准确ID。")
	} else {
		f.hint.SetText("请先连接Go管理服务，再选择发放目标。")
	}
}
