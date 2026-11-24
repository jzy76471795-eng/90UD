//go:build windows

package main

import (
	"fmt"
	"strings"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
)

const administratorMailTitle = "gm管理员"

type rewardItem struct {
	ID, Quantity int
}

type rewardBundle struct {
	Items                []rewardItem
	Points, Tokens, Gold int
}

func (r rewardBundle) clone() rewardBundle {
	r.Items = append([]rewardItem(nil), r.Items...)
	return r
}

func (r rewardBundle) validate(maxItems int) error {
	if maxItems > 0 && len(r.Items) > maxItems {
		return fmt.Errorf("一次最多添加 %d 件物品。", maxItems)
	}
	for _, item := range r.Items {
		if item.ID <= 0 || item.Quantity <= 0 {
			return fmt.Errorf("物品ID和数量必须大于0。")
		}
	}
	if r.Points < 0 || r.Tokens < 0 || r.Gold < 0 {
		return fmt.Errorf("资产数量不能小于0。")
	}
	return nil
}

func (r rewardBundle) empty() bool {
	return len(r.Items) == 0 && r.Points == 0 && r.Tokens == 0 && r.Gold == 0
}

func (r rewardBundle) description() string {
	lines := []string{fmt.Sprintf("物品：%d 件", len(r.Items))}
	for i, item := range r.Items {
		lines = append(lines, fmt.Sprintf("  %d. 物品ID：%d，数量：%d", i+1, item.ID, item.Quantity))
	}
	lines = append(lines,
		fmt.Sprintf("点卷：%d（直接到账号）", r.Points),
		fmt.Sprintf("代币：%d（直接到账号）", r.Tokens),
		fmt.Sprintf("金币：%d（直接到角色）", r.Gold))
	return strings.Join(lines, "\r\n")
}

type rewardItemModel struct {
	walk.TableModelBase
	items []rewardItem
}

func (m *rewardItemModel) RowCount() int { return len(m.items) }
func (m *rewardItemModel) Value(row, col int) interface{} {
	if row < 0 || row >= len(m.items) {
		return nil
	}
	switch col {
	case 0:
		return row + 1
	case 1:
		return m.items[row].ID
	case 2:
		return m.items[row].Quantity
	}
	return nil
}

type rewardEditor struct {
	maxItems                               int
	model                                  *rewardItemModel
	table                                  *walk.TableView
	itemID, quantity, points, tokens, gold *walk.NumberEdit
	count, message                         *walk.Label
	add                                    *walk.PushButton
	onChanged                              func()
}

func newRewardEditor(maxItems int, onChanged func()) *rewardEditor {
	return &rewardEditor{maxItems: maxItems, model: &rewardItemModel{}, onChanged: onChanged}
}

func (e *rewardEditor) widget() decl.GroupBox {
	return e.widgetWithHeader("", nil)
}

func (e *rewardEditor) widgetWithHeader(title string, fields []decl.Widget) decl.GroupBox {
	if title == "" {
		title = "物品与资产奖励"
		if e.maxItems > 0 {
			title = fmt.Sprintf("附件物品与资产（最多 %d 件物品）", e.maxItems)
		}
	}
	assetWidth := 130
	if len(fields) > 0 {
		assetWidth = 80
	}
	header := append([]decl.Widget(nil), fields...)
	header = append(header,
		decl.Label{Text: "点卷："}, decl.NumberEdit{AssignTo: &e.points, MinValue: 0, MaxValue: 2147483647, Value: 0.0, MinSize: decl.Size{Width: assetWidth}, MaxSize: decl.Size{Width: assetWidth}, OnValueChanged: e.changed},
		decl.Label{Text: "代币："}, decl.NumberEdit{AssignTo: &e.tokens, MinValue: 0, MaxValue: 2147483647, Value: 0.0, MinSize: decl.Size{Width: assetWidth}, MaxSize: decl.Size{Width: assetWidth}, OnValueChanged: e.changed},
		decl.Label{Text: "金币："}, decl.NumberEdit{AssignTo: &e.gold, MinValue: 0, MaxValue: 2147483647, Value: 0.0, MinSize: decl.Size{Width: assetWidth}, MaxSize: decl.Size{Width: assetWidth}, OnValueChanged: e.changed},
		decl.HSpacer{},
	)
	return decl.GroupBox{
		Title: title, MinSize: decl.Size{Height: 210}, MaxSize: decl.Size{Height: 240},
		Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}, Spacing: 6},
		Children: []decl.Widget{
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: header,
			},
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true, Spacing: 8},
				Children: []decl.Widget{
					decl.Label{Text: "物品ID："},
					decl.NumberEdit{AssignTo: &e.itemID, MinValue: 0, MaxValue: 2147483647, Value: 0.0, MinSize: decl.Size{Width: 130}},
					decl.Label{Text: "数量："},
					decl.NumberEdit{AssignTo: &e.quantity, MinValue: 1, MaxValue: 2147483647, Value: 1.0, MinSize: decl.Size{Width: 100}},
					decl.PushButton{AssignTo: &e.add, Text: "添加物品", OnClicked: e.addItem},
					decl.PushButton{Text: "删除选中", OnClicked: e.removeItem},
					decl.Label{AssignTo: &e.count, Text: "已添加 0 件"}, decl.HSpacer{},
				},
			},
			decl.TableView{
				AssignTo: &e.table, Model: e.model, StretchFactor: 1, MinSize: decl.Size{Height: 70},
				AlternatingRowBG: true, LastColumnStretched: true, NotSortableByHeaderClick: true,
				Columns: []decl.TableViewColumn{{Title: "序号", Width: 70}, {Title: "物品ID", Width: 250}, {Title: "物品数量", Width: 180}},
			},
			decl.Label{AssignTo: &e.message, Text: "点卷、代币直接到账号，金币直接到角色；数量为0表示不添加该资产。"},
		},
	}
}

func (e *rewardEditor) changed() {
	if e.onChanged != nil {
		e.onChanged()
	}
}

func (e *rewardEditor) addItem() {
	if e.maxItems > 0 && len(e.model.items) >= e.maxItems {
		e.message.SetText(fmt.Sprintf("已达上限：一次最多 %d 件物品。", e.maxItems))
		return
	}
	item := rewardItem{ID: int(e.itemID.Value()), Quantity: int(e.quantity.Value())}
	if item.ID <= 0 || item.Quantity <= 0 {
		e.message.SetText("请填写大于0的物品ID和数量，再点击添加物品。")
		return
	}
	e.model.items = append(e.model.items, item)
	e.resetTable()
	e.itemID.SetValue(0)
	e.quantity.SetValue(1)
	e.message.SetText("物品已添加到奖励列表，可继续添加其他物品。")
	e.changed()
}

func (e *rewardEditor) removeItem() {
	index := e.table.CurrentIndex()
	if index < 0 || index >= len(e.model.items) {
		e.message.SetText("请先选中要删除的物品。")
		return
	}
	e.model.items = append(e.model.items[:index], e.model.items[index+1:]...)
	e.resetTable()
	e.message.SetText("已删除选中的物品。")
	e.changed()
}

func (e *rewardEditor) resetTable() {
	e.table.SetCurrentIndex(-1)
	e.model.PublishRowsReset()
	text := fmt.Sprintf("已添加 %d 件", len(e.model.items))
	if e.maxItems > 0 {
		text = fmt.Sprintf("已添加 %d / %d 件", len(e.model.items), e.maxItems)
		e.add.SetEnabled(len(e.model.items) < e.maxItems)
	}
	e.count.SetText(text)
}

func (e *rewardEditor) bundle() rewardBundle {
	return rewardBundle{Items: append([]rewardItem(nil), e.model.items...), Points: int(e.points.Value()), Tokens: int(e.tokens.Value()), Gold: int(e.gold.Value())}
}
