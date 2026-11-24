//go:build windows

package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
)

type operationEntry struct {
	when                                   time.Time
	module, action, target, result, detail string
}

type operationLogUI struct {
	entries, view     []operationEntry
	model             *textTableModel
	module            *walk.ComboBox
	keyword, from, to *walk.LineEdit
	count             *walk.Label
	table             *walk.TableView
	detail            *walk.TextEdit
	remote            *walk.CheckBox
	pageNumber, total int
	sequence          int
}

var managementAudit = &operationLogUI{model: &textTableModel{}}

func logUIAction(module, action, target, detail string) {
	managementAudit.append(module, action, target, "界面操作", detail)
}

func (a *operationLogUI) append(module, action, target, result, detail string) {
	a.entries = append(a.entries, operationEntry{time.Now().In(chinaTime), module, action, target, result, detail})
	persistLocalAudit(a.entries[len(a.entries)-1])
	a.query()
}

func (a *operationLogUI) page() decl.TabPage {
	return decl.TabPage{
		Title: "操作日志", Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 8},
		Children: []decl.Widget{
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{Text: "本机操作日志按日保存；Go管理服务审计单独查询。"}, decl.HSpacer{}, decl.CheckBox{AssignTo: &a.remote, Text: "查询Go服务审计", OnCheckedChanged: a.query}}},
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true, Spacing: 8},
				Children: []decl.Widget{
					decl.Label{Text: "模块："},
					decl.ComboBox{AssignTo: &a.module, Model: []string{"全部模块", "服务器管理", "账号管理", "CDK管理", "公告管理", "在线福利", "注册管理", "PVF管理", "登录下载管理", "记录查询", "一键部署", "系统"}, CurrentIndex: 0, MinSize: decl.Size{Width: 150}, MaxSize: decl.Size{Width: 180}, OnCurrentIndexChanged: a.query},
					decl.Label{Text: "关键词："}, decl.LineEdit{AssignTo: &a.keyword, StretchFactor: 1, CueBanner: "操作内容、目标或结果"},
					decl.PushButton{Text: "查询 / 刷新", OnClicked: a.query}, decl.PushButton{Text: "导出本页", OnClicked: a.export},
					decl.PushButton{Text: "重置", OnClicked: func() {
						a.keyword.SetText("")
						a.from.SetText("")
						a.to.SetText("")
						a.module.SetCurrentIndex(0)
						a.query()
					}},
				},
			},
			queryDateRow(&a.from, &a.to),
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{AssignTo: &a.count, Text: "共0条操作日志"}, decl.PushButton{Text: "上一页", OnClicked: func() {
				if a.pageNumber > 1 {
					a.pageNumber--
					a.query()
				}
			}}, decl.PushButton{Text: "下一页", OnClicked: func() {
				if a.pageNumber*50 < a.total {
					a.pageNumber++
					a.query()
				}
			}}, decl.HSpacer{}}},
			decl.TableView{
				AssignTo: &a.table, Model: a.model, StretchFactor: 1, AlternatingRowBG: true, LastColumnStretched: true, NotSortableByHeaderClick: true, OnCurrentIndexChanged: a.showDetail,
				Columns: []decl.TableViewColumn{{Title: "时间", Width: 170}, {Title: "操作人", Width: 100}, {Title: "模块", Width: 120}, {Title: "操作", Width: 170}, {Title: "目标", Width: 190}, {Title: "结果", Width: 200}},
			},
			decl.GroupBox{Title: "操作详情", MinSize: decl.Size{Height: 110}, MaxSize: decl.Size{Height: 130}, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, Children: []decl.Widget{decl.TextEdit{AssignTo: &a.detail, ReadOnly: true, VScroll: true, Text: "选择一条日志查看详情。"}}},
		},
	}
}

func (a *operationLogUI) query() {
	a.sequence++
	if a.table == nil || a.keyword == nil || a.from == nil || a.to == nil || a.count == nil {
		return
	}
	if a.remote != nil && a.remote.Checked() {
		a.queryRemote()
		return
	}
	from, to, err := queryTimeRange(a.from.Text(), a.to.Text())
	rows := [][]string{}
	a.view = nil
	if err == nil {
		for i := len(a.entries) - 1; i >= 0; i-- {
			entry := a.entries[i]
			if !inQueryTimeRange(entry.when, from, to) || (a.module.CurrentIndex() > 0 && a.module.Text() != entry.module) || !matches(a.keyword.Text(), entry.action, entry.target, entry.result, entry.detail) {
				continue
			}
			a.view = append(a.view, entry)
			rows = append(rows, []string{entry.when.Format("2006-01-02 15:04:05"), "本机界面", entry.module, entry.action, entry.target, entry.result})
		}
	}
	a.total = len(rows)
	if a.pageNumber < 1 {
		a.pageNumber = 1
	}
	if a.pageNumber > 1 && (a.pageNumber-1)*50 >= a.total {
		a.pageNumber = max(1, (a.total+49)/50)
	}
	start := min((a.pageNumber-1)*50, len(rows))
	end := min(start+50, len(rows))
	rows = rows[start:end]
	a.view = a.view[start:end]
	a.table.SetCurrentIndex(-1)
	a.model.rows = rows
	a.model.PublishRowsReset()
	if err != nil {
		a.count.SetText(err.Error())
	} else {
		a.count.SetText(fmt.Sprintf("本机已加载 %d 条操作日志，第 %d 页", a.total, a.pageNumber))
	}
	a.showDetail()
}

func (a *operationLogUI) showDetail() {
	if a.table == nil || a.detail == nil {
		return
	}
	index := a.table.CurrentIndex()
	if index < 0 || index >= len(a.view) {
		a.detail.SetText("选择一条日志查看详情。")
		return
	}
	e := a.view[index]
	a.detail.SetText(strings.Join([]string{"时间：" + e.when.Format("2006-01-02 15:04:05"), "模块：" + e.module, "操作：" + e.action, "目标：" + e.target, "结果：" + e.result, "详情：" + e.detail}, "\r\n"))
}
