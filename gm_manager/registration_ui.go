//go:build windows

package main

import (
	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
)

type registrationUI struct {
	configRead                      configurationReadState
	saveConfigButton                *walk.PushButton
	open, activation, rewardEnabled *walk.CheckBox
	ipLimit, quantity               *walk.NumberEdit
	name                            *walk.LineEdit
	rewards                         *rewardEditor
	codes                           *textTableModel
	codeTable                       *walk.TableView
	copyStatus                      *walk.Label
	generatedCodes                  []string
	codesRevision                   uint64
	output                          *walk.TextEdit
	revision                        int64
	generating                      bool
}

func registrationPage() decl.TabPage {
	r := &registrationUI{codes: &textTableModel{}}
	configurationLoads = append(configurationLoads, r.load)
	r.rewards = newRewardEditor(0, r.changed)
	rewardGroup := r.rewards.widgetWithHeader("注册奖励", []decl.Widget{decl.CheckBox{AssignTo: &r.rewardEnabled, Text: "启用注册奖励", OnCheckedChanged: r.changed}})
	return decl.TabPage{
		Title: "注册管理", Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 8},
		Children: []decl.Widget{
			decl.Label{Text: "直接管理游戏数据库中的注册策略和激活码。注册奖励对保存规则后新增账号在首次建角后发放一次。"},
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true, Spacing: 10},
				Children: []decl.Widget{
					decl.GroupBox{
						Title: "注册限制", StretchFactor: 1, Layout: decl.VBox{Margins: decl.Margins{10, 10, 10, 10}, Spacing: 8},
						Children: []decl.Widget{
							decl.CheckBox{AssignTo: &r.open, Text: "开放注册", Checked: true, OnCheckedChanged: r.changed},
							decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{Text: "同IP最多注册："}, decl.NumberEdit{AssignTo: &r.ipLimit, MinValue: 0, MaxValue: 10000, Value: 3.0, MinSize: decl.Size{Width: 90}, MaxSize: decl.Size{Width: 90}, OnValueChanged: r.changed}, decl.HSpacer{}}},
							decl.Label{Text: "数量为0表示不设置同IP注册上限。"}, decl.VSpacer{},
						},
					},
					decl.GroupBox{
						Title: "注册激活码", StretchFactor: 2, Layout: decl.VBox{Margins: decl.Margins{10, 10, 10, 10}, Spacing: 6},
						Children: []decl.Widget{
							decl.CheckBox{AssignTo: &r.activation, Text: "注册时要求激活码", OnCheckedChanged: r.changed},
							decl.Composite{
								Layout: decl.HBox{MarginsZero: true, Spacing: 8},
								Children: []decl.Widget{
									decl.Label{Text: "名称："}, decl.LineEdit{AssignTo: &r.name, MinSize: decl.Size{Width: 130}, MaxSize: decl.Size{Width: 130}, CueBanner: "激活码名称"},
									decl.Label{Text: "数量："}, decl.NumberEdit{AssignTo: &r.quantity, MinValue: 1, MaxValue: 1000, Value: 1.0, MinSize: decl.Size{Width: 70}, MaxSize: decl.Size{Width: 70}},
									decl.PushButton{Text: "生成激活码", OnClicked: r.generateManagedCodes}, decl.HSpacer{},
								},
							},
							decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{
								decl.PushButton{Text: "复制选中激活码", OnClicked: r.copySelectedCode},
								decl.PushButton{Text: "复制本批激活码", OnClicked: func() { copyCodes(r.generatedCodes, r.copyStatus) }},
								decl.Label{AssignTo: &r.copyStatus}, decl.HSpacer{},
							}},
							decl.TableView{
								AssignTo: &r.codeTable, Model: r.codes, MinSize: decl.Size{Height: 70}, MaxSize: decl.Size{Height: 85}, LastColumnStretched: true, NotSortableByHeaderClick: true,
								OnMouseDown: func(x, y int, button walk.MouseButton) { selectCodeOnRightClick(r.codeTable, x, y, button) },
								ContextMenuItems: []decl.MenuItem{
									decl.Action{Text: "复制激活码", OnTriggered: r.copySelectedCode},
									decl.Action{Text: "删除激活码", OnTriggered: r.deleteSelectedCode},
								},
								Columns: []decl.TableViewColumn{{Title: "名称", Width: 120}, {Title: "激活码", Width: 250}, {Title: "状态", Width: 130}},
							},
						},
					},
				},
			},
			rewardGroup,
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.PushButton{AssignTo: &r.saveConfigButton, Enabled: false, Text: "保存管理配置", OnClicked: r.save}, decl.PushButton{Text: "读取已保存配置", OnClicked: r.load}, decl.PushButton{Text: "查看发奖状态", OnClicked: func() { showJobStatus("registration", r.output) }}, decl.HSpacer{}}},
			decl.GroupBox{Title: "注册方案与保存结果", StretchFactor: 1, MinSize: decl.Size{Height: 90}, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, Children: []decl.Widget{decl.TextEdit{AssignTo: &r.output, ReadOnly: true, VScroll: true, Text: "连接后可读取或保存实际数据库策略、生成激活码。新账号奖励由Go服务直接写库。"}}},
		},
	}
}

func (r *registrationUI) copySelectedCode() {
	index := r.codeTable.CurrentIndex()
	if index < 0 || index >= len(r.codes.rows) {
		r.copyStatus.SetText("请先选择一行")
		return
	}
	copyCodes([]string{r.codes.rows[index][1]}, r.copyStatus)
}

func (r *registrationUI) deleteSelectedCode() {
	index := r.codeTable.CurrentIndex()
	if index < 0 || index >= len(r.codes.rows) {
		r.copyStatus.SetText("请先选择一行")
		return
	}
	code := r.codes.rows[index][1]
	deleteManagedCode("注册管理", "registration", code, r.copyStatus, func() {
		r.codesRevision++
		r.generatedCodes = withoutCode(r.generatedCodes, code)
		r.codeTable.SetCurrentIndex(-1)
		r.codes.removeCode(code)
	})
}

func (r *registrationUI) changed() {
	if r.output != nil {
		r.output.SetText("注册设置已修改，保存管理配置后生效。")
	}
}
