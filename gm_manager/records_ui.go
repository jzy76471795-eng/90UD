//go:build windows

package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
)

type gameRecord struct {
	when                                                        time.Time
	kind, account, character, direction, content, place, result string
	start, end                                                  time.Time
	clearMS                                                     int64
}

type recordsUI struct {
	kind, direction             *walk.ComboBox
	account, keyword, from, to  *walk.LineEdit
	count                       *walk.Label
	table                       *walk.TableView
	detail                      *walk.TextEdit
	model                       *textTableModel
	cleanup                     *walk.PushButton
	view                        []gameRecord
	pageNumber, total, sequence int
}

func recordsPage() decl.TabPage {
	r := &recordsUI{model: &textTableModel{}}
	return decl.TabPage{
		Title: "记录查询", Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 8},
		Children: []decl.Widget{
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{Text: "连接Go管理服务后查询游戏已有记录；未采集的类别会明确提示。"}, decl.HSpacer{}, decl.PushButton{AssignTo: &r.cleanup, Text: "清理历史流水", OnClicked: r.requestCleanup}}},
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true, Spacing: 8},
				Children: []decl.Widget{
					decl.Label{Text: "记录类型："}, decl.ComboBox{AssignTo: &r.kind, Model: []string{"全部类型", "邮件收发", "点卷进出", "代币进出", "金币进出", "刷图记录", "副本记录", "聊天记录", "副本最佳成绩", "团本凭据", "物品变动", "复活币流水", "CDK兑换"}, CurrentIndex: 0, MinSize: decl.Size{Width: 135}, MaxSize: decl.Size{Width: 155}, OnCurrentIndexChanged: r.queryFirst},
					decl.Label{Text: "方向："}, decl.ComboBox{AssignTo: &r.direction, Model: []string{"全部方向", "收入", "支出", "发送", "接收"}, CurrentIndex: 0, MinSize: decl.Size{Width: 100}, MaxSize: decl.Size{Width: 110}, OnCurrentIndexChanged: r.queryFirst},
					decl.Label{Text: "账号 / 角色："}, decl.LineEdit{AssignTo: &r.account, StretchFactor: 1},
					decl.PushButton{Text: "查询", OnClicked: r.queryAndLog}, decl.PushButton{Text: "重置", OnClicked: r.reset}, decl.PushButton{Text: "导出本页", OnClicked: r.export},
				},
			},
			queryDateRow(&r.from, &r.to),
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{Text: "关键词："}, decl.LineEdit{AssignTo: &r.keyword, StretchFactor: 1, CueBanner: "邮件内容、聊天内容、地图或副本名称"}}},
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{AssignTo: &r.count, Text: "尚未查询", EllipsisMode: decl.EllipsisEnd, StretchFactor: 1}, decl.PushButton{Text: "上一页", OnClicked: func() {
				if r.pageNumber > 1 {
					r.pageNumber--
					r.query()
				}
			}}, decl.PushButton{Text: "下一页", OnClicked: func() {
				if r.pageNumber*50 < r.total {
					r.pageNumber++
					r.query()
				}
			}}, decl.HSpacer{}}},
			decl.TableView{
				AssignTo: &r.table, Model: r.model, StretchFactor: 1, AlternatingRowBG: true, LastColumnStretched: true, NotSortableByHeaderClick: true, OnCurrentIndexChanged: r.showDetail,
				Columns: []decl.TableViewColumn{{Title: "时间", Width: 160}, {Title: "类型", Width: 100}, {Title: "账号", Width: 120}, {Title: "角色", Width: 110}, {Title: "方向", Width: 70}, {Title: "数量 / 内容", Width: 220}, {Title: "地图 / 副本", Width: 140}, {Title: "通关时间", Width: 110}, {Title: "结果", Width: 100}},
			},
			decl.GroupBox{Title: "记录详情", MinSize: decl.Size{Height: 120}, MaxSize: decl.Size{Height: 145}, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, Children: []decl.Widget{decl.TextEdit{AssignTo: &r.detail, ReadOnly: true, VScroll: true, Text: "选择记录查看完整信息；副本记录包含开始时间、结束时间及通关耗时。"}}},
		},
	}
}

func formatClearTime(milliseconds int64) string {
	if milliseconds <= 0 {
		return "—"
	}
	return fmt.Sprintf("%02d:%02d:%02d.%03d", milliseconds/3600000, milliseconds/60000%60, milliseconds/1000%60, milliseconds%1000)
}

func (r *recordsUI) query() {
	if r.table == nil || r.count == nil || r.keyword == nil || r.from == nil || r.to == nil {
		return
	}
	usesDirection := r.kind.CurrentIndex() <= 4
	r.direction.SetEnabled(usesDirection)
	if !usesDirection && r.direction.CurrentIndex() != 0 {
		r.direction.SetCurrentIndex(0)
	}
	r.queryRemote()
}

func (r *recordsUI) queryAndLog() {
	r.pageNumber = 1
	r.query()
	logUIAction("记录查询", "查询记录", r.kind.Text(), r.count.Text())
}

func (r *recordsUI) reset() {
	r.pageNumber = 1
	r.account.SetText("")
	r.keyword.SetText("")
	r.from.SetText("")
	r.to.SetText("")
	r.kind.SetCurrentIndex(0)
	r.direction.SetCurrentIndex(0)
	r.query()
}

func (r *recordsUI) showDetail() {
	if r.table == nil || r.detail == nil {
		return
	}
	index := r.table.CurrentIndex()
	if index < 0 || index >= len(r.view) {
		r.detail.SetText("选择记录查看完整信息；副本记录包含开始时间、结束时间及通关耗时。")
		return
	}
	e := r.view[index]
	lines := []string{"时间：" + e.when.Format("2006-01-02 15:04:05"), "类型：" + e.kind, "账号：" + e.account, "角色：" + e.character, "方向：" + e.direction, "数量 / 内容：" + e.content, "地图 / 副本：" + e.place, "结果：" + e.result}
	if !e.start.IsZero() {
		lines = append(lines, "开始时间："+e.start.Format("2006-01-02 15:04:05.000"), "结束时间："+e.end.Format("2006-01-02 15:04:05.000"), "通关耗时："+formatClearTime(e.clearMS))
	}
	r.detail.SetText(strings.Join(lines, "\r\n"))
}
