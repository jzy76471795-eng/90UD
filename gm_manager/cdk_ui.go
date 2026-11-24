//go:build windows

package main

import (
	"fmt"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
)

type cdkRow struct {
	Name, Code string
	Reward     rewardBundle
	State      string
}

type cdkModel struct {
	walk.TableModelBase
	rows []cdkRow
}

func (m *cdkModel) RowCount() int { return len(m.rows) }
func (m *cdkModel) Value(row, col int) interface{} {
	if row < 0 || row >= len(m.rows) {
		return nil
	}
	r := m.rows[row]
	status := codeStateText(r.State)
	values := []interface{}{r.Name, r.Code, len(r.Reward.Items), r.Reward.Points, r.Reward.Tokens, r.Reward.Gold, status}
	if col < 0 || col >= len(values) {
		return nil
	}
	return values[col]
}

// Code creation and actual game redemption receipts are queried separately.
type redemptionRow struct {
	Time, Name, Code, Account, Character, Items string
	Points, Tokens, Gold                        int
	RewardUnknown                               bool
}

type redemptionModel struct {
	walk.TableModelBase
	rows []redemptionRow
}

func (m *redemptionModel) RowCount() int { return len(m.rows) }
func (m *redemptionModel) Value(row, col int) interface{} {
	if row < 0 || row >= len(m.rows) {
		return nil
	}
	r := m.rows[row]
	values := []interface{}{r.Time, r.Name, r.Code, r.Account, r.Character, r.Items, r.Points, r.Tokens, r.Gold}
	if r.RewardUnknown {
		values[5] = "未知"
		values[6] = "未知"
		values[7] = "未知"
		values[8] = "未知"
	}
	if col < 0 || col >= len(values) {
		return nil
	}
	return values[col]
}

type cdkUI struct {
	manual                       *walk.LineEdit
	search, recordSearch, player *walk.LineEdit

	status                      *walk.ComboBox
	output, detail              *walk.TextEdit
	count, recordCount          *walk.Label
	batchCopyStatus, copyStatus *walk.Label
	table                       *walk.TableView
	model                       *cdkModel
	recordModel                 *redemptionModel
	pageNumber, total, sequence int
	generating                  bool
	generatedCodes              []string
}

func newCDKUI() *cdkUI {
	return &cdkUI{model: &cdkModel{}, recordModel: &redemptionModel{}}
}

func (c *cdkUI) page() decl.TabPage {
	return decl.TabPage{
		Title: "CDK管理", Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}, Spacing: 8},
		Children: []decl.Widget{
			decl.Label{Text: "直接向游戏数据库生成随机CDK，每码一次。游戏兑换协议支持物品、点券和金币；代币请使用发放页面。"},
			decl.TabWidget{ContentMarginsZero: true, Pages: []decl.TabPage{c.generatePage(), c.queryPage(), c.recordPage()}},
		},
	}
}

func (c *cdkUI) generatePage() decl.TabPage {
	return decl.TabPage{
		Title: "生成CDK", Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 10},
		Children: []decl.Widget{
			decl.Label{Text: "名称 / 物品ID / 数量 / 点券 / 金币 / 制作数量"},
			decl.LineEdit{AssignTo: &c.manual, CueBanner: "礼包 / 3330 / 10 / 3339 / 20 / 100 / 5000 / 1", OnTextChanged: c.inputsChanged},
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true, Spacing: 8},
				Children: []decl.Widget{
					decl.PushButton{Text: "生成并保存", OnClicked: c.generateManaged},
					decl.PushButton{Text: "复制本批CDK", OnClicked: func() { copyCodes(c.generatedCodes, c.batchCopyStatus) }},
					decl.PushButton{Text: "清空结果", OnClicked: func() {
						c.generatedCodes = nil
						c.batchCopyStatus.SetText("")
						c.output.SetText("")
					}},
					decl.Label{AssignTo: &c.batchCopyStatus}, decl.HSpacer{},
				},
			},
			decl.GroupBox{
				Title: "生成结果", Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, StretchFactor: 1,
				Children: []decl.Widget{decl.TextEdit{AssignTo: &c.output, ReadOnly: true, VScroll: true, Text: "连接管理服务后，填写名称和奖励并点击“生成并保存”；已保存结果可在“CDK查询”中查看。"}},
			},
		},
	}
}

func (c *cdkUI) queryPage() decl.TabPage {
	return decl.TabPage{
		Title: "CDK查询", Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 8},
		Children: []decl.Widget{
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true, Spacing: 8},
				Children: []decl.Widget{
					decl.Label{Text: "名称 / CDK："}, decl.LineEdit{AssignTo: &c.search, StretchFactor: 1},
					decl.Label{Text: "状态："}, decl.ComboBox{AssignTo: &c.status, Model: []string{"所有状态", "未兑换", "已兑换", "已停用"}, CurrentIndex: 0, MinSize: decl.Size{Width: 120}, MaxSize: decl.Size{Width: 150}, OnCurrentIndexChanged: c.query},
					decl.PushButton{Text: "查询", OnClicked: c.query},
					decl.PushButton{Text: "重置", OnClicked: func() { c.search.SetText(""); c.status.SetCurrentIndex(0); c.query() }},
				},
			},
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{AssignTo: &c.count, Text: "尚未查询"}, decl.PushButton{Text: "上一页", OnClicked: func() {
				if c.pageNumber > 1 {
					c.pageNumber--
					c.query()
				}
			}}, decl.PushButton{Text: "下一页", OnClicked: func() {
				if c.pageNumber*50 < c.total {
					c.pageNumber++
					c.query()
				}
			}}, decl.PushButton{Text: "导出本页", OnClicked: c.exportPage}, decl.PushButton{Text: "停用选中码", OnClicked: func() { c.setCodeEnabled(false) }}, decl.PushButton{Text: "恢复选中码", OnClicked: func() { c.setCodeEnabled(true) }}, decl.HSpacer{}}},
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{
				decl.PushButton{Text: "复制选中CDK", OnClicked: c.copySelectedCode},
				decl.PushButton{Text: "复制本页CDK", OnClicked: c.copyPageCodes},
				decl.Label{AssignTo: &c.copyStatus}, decl.HSpacer{},
			}},
			decl.TableView{
				AssignTo: &c.table, Model: c.model, StretchFactor: 1,
				AlternatingRowBG: true, LastColumnStretched: true, NotSortableByHeaderClick: true, OnCurrentIndexChanged: c.showDetail,
				OnMouseDown: func(x, y int, button walk.MouseButton) { selectCodeOnRightClick(c.table, x, y, button) },
				ContextMenuItems: []decl.MenuItem{
					decl.Action{Text: "复制CDK", OnTriggered: c.copySelectedCode},
					decl.Action{Text: "删除CDK", OnTriggered: c.deleteSelectedCode},
				},
				Columns: []decl.TableViewColumn{
					{Title: "名称", Width: 180}, {Title: "CDK", Width: 280}, {Title: "物品件数", Width: 85},
					{Title: "点卷", Width: 100}, {Title: "代币", Width: 100}, {Title: "金币", Width: 130}, {Title: "状态", Width: 150},
				},
			},
			decl.GroupBox{
				Title: "选中CDK的完整奖励", Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, MinSize: decl.Size{Height: 120}, MaxSize: decl.Size{Height: 140},
				Children: []decl.Widget{decl.TextEdit{AssignTo: &c.detail, ReadOnly: true, VScroll: true, Text: "选择一行查看完整物品及资产。"}},
			},
		},
	}
}

func (c *cdkUI) recordPage() decl.TabPage {
	return decl.TabPage{
		Title: "兑换记录", Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 8},
		Children: []decl.Widget{
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true, Spacing: 8},
				Children: []decl.Widget{
					decl.Label{Text: "名称 / CDK："}, decl.LineEdit{AssignTo: &c.recordSearch, StretchFactor: 1},
					decl.Label{Text: "账号 / 角色："}, decl.LineEdit{AssignTo: &c.player, StretchFactor: 1},
					decl.PushButton{Text: "查询", OnClicked: c.queryRecords},
					decl.PushButton{Text: "重置", OnClicked: func() { c.recordSearch.SetText(""); c.player.SetText(""); c.queryRecords() }},
				},
			},
			decl.Label{AssignTo: &c.recordCount, Text: "共 0 条兑换记录（未连接真实数据源）"},
			decl.TableView{
				Model: c.recordModel, StretchFactor: 1, AlternatingRowBG: true, LastColumnStretched: true, NotSortableByHeaderClick: true,
				Columns: []decl.TableViewColumn{
					{Title: "兑换时间", Width: 150}, {Title: "名称", Width: 120}, {Title: "CDK", Width: 200},
					{Title: "账号", Width: 120}, {Title: "角色", Width: 120}, {Title: "物品奖励", Width: 130},
					{Title: "点卷方案", Width: 90}, {Title: "代币方案", Width: 90}, {Title: "金币方案", Width: 100},
				},
			},
			decl.Label{Text: "生成管理码不会产生游戏兑换记录；此处读取游戏库已有兑换凭据，奖励列显示方案。"},
		},
	}
}

func (c *cdkUI) inputsChanged() {
	if c.output != nil && len(c.generatedCodes) == 0 {
		c.output.SetText("奖励方案已更新。点击“生成并保存”创建新一批管理码；已保存的管理码不会随表单变化。")
	}
}

func (c *cdkUI) copySelectedCode() {
	index := c.table.CurrentIndex()
	if index < 0 || index >= len(c.model.rows) {
		c.copyStatus.SetText("请先选择一行")
		return
	}
	copyCodes([]string{c.model.rows[index].Code}, c.copyStatus)
}

func (c *cdkUI) copyPageCodes() {
	codes := make([]string, 0, len(c.model.rows))
	for _, row := range c.model.rows {
		codes = append(codes, row.Code)
	}
	copyCodes(codes, c.copyStatus)
}

func (c *cdkUI) deleteSelectedCode() {
	index := c.table.CurrentIndex()
	if index < 0 || index >= len(c.model.rows) {
		c.copyStatus.SetText("请先选择一行")
		return
	}
	code := c.model.rows[index].Code
	deleteManagedCode("CDK管理", "cdk", code, c.copyStatus, func() {
		c.generatedCodes = withoutCode(c.generatedCodes, code)
		c.batchCopyStatus.SetText("")
		if len(c.model.rows) == 1 && c.model.rows[0].Code == code && c.pageNumber > 1 {
			c.pageNumber--
		}
		c.query()
	})
}

func (c *cdkUI) query() {
	if c.table == nil || c.search == nil || c.status == nil || c.count == nil {
		return
	}
	c.queryManaged()
}

func (c *cdkUI) showDetail() {
	if c.detail == nil || c.table == nil {
		return
	}
	index := c.table.CurrentIndex()
	if index < 0 || index >= len(c.model.rows) {
		c.detail.SetText("选择一行查看完整物品及资产。")
		return
	}
	r := c.model.rows[index]
	c.detail.SetText(fmt.Sprintf("名称：%s\r\nCDK：%s\r\n%s", r.Name, r.Code, r.Reward.description()))
}

func (c *cdkUI) queryRecords() {
	if c.recordSearch == nil || c.player == nil || c.recordCount == nil {
		return
	}
	c.queryRemoteRedemptions()
}
