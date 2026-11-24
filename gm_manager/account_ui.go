//go:build windows

package main

import (
	"fmt"
	"strconv"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
)

type accountUI struct {
	window                                 *walk.MainWindow
	tabs                                   *walk.TabWidget
	status                                 *walk.Label
	accountScope, characterScope           *walk.ComboBox
	accountSearch                          *walk.LineEdit
	characterSearch, ownerSearch           *walk.LineEdit
	accountCount, characterCount           *walk.Label
	accountTable, characterTable           *walk.TableView
	accounts                               *accountTableModel
	accountInfo                            *walk.TextEdit
	characters                             *characterTableModel
	accountActions                         [4]*walk.Action
	characterActions                       [2]*walk.Action
	mail, points, tokens, gold             *grantForm
	accountPageNumber, characterPageNumber int
	accountTotal, characterTotal           int
	accountSequence, characterSequence     int
}

func newAccountUI() *accountUI {
	a := &accountUI{accounts: &accountTableModel{}, characters: &characterTableModel{}}
	a.mail = &grantForm{owner: a, title: "邮件发放", characterTarget: true, mail: true}
	a.mail.rewards = newRewardEditor(10, a.mail.inputsChanged)
	a.points = &grantForm{owner: a, title: "点卷发放"}
	a.tokens = &grantForm{owner: a, title: "代币发放"}
	a.gold = &grantForm{owner: a, title: "金币添加", characterTarget: true}
	return a
}

func (a *accountUI) page() decl.TabPage {
	return decl.TabPage{
		Title: "账号管理", Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}, Spacing: 8},
		Children: []decl.Widget{
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true, Spacing: 12},
				Children: []decl.Widget{
					decl.Label{AssignTo: &a.status, Text: "未连接数据源"},
					decl.HSpacer{},
				},
			},
			decl.TabWidget{
				AssignTo: &a.tabs, ContentMarginsZero: true,
				Pages: []decl.TabPage{a.accountPage(), a.characterPage(), a.mail.page(), a.points.page(), a.tokens.page(), a.gold.page()},
			},
		},
	}
}

func (a *accountUI) accountPage() decl.TabPage {
	return decl.TabPage{
		Title: "账号查询", Layout: decl.VBox{Margins: decl.Margins{10, 10, 10, 10}, Spacing: 8},
		Children: []decl.Widget{
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true, Spacing: 8},
				Children: []decl.Widget{
					decl.Label{Text: "查询范围："},
					decl.ComboBox{AssignTo: &a.accountScope, Model: []string{"所有账号", "在线账号", "封禁账号"}, CurrentIndex: 0, MinSize: decl.Size{Width: 120}, MaxSize: decl.Size{Width: 150}, OnCurrentIndexChanged: a.queryAccountsFirst},
					decl.Label{Text: "账号 / ID："},
					decl.LineEdit{AssignTo: &a.accountSearch, StretchFactor: 1},
					decl.PushButton{Text: "查询", OnClicked: func() {
						a.queryAccountsFirst()
						logUIAction("账号管理", "账号查询", a.accountSearch.Text(), a.accountCount.Text())
					}},
					decl.PushButton{Text: "重置", OnClicked: func() {
						a.accountSearch.SetText("")
						a.accountScope.SetCurrentIndex(0)
						a.queryAccounts()
					}},
				},
			},
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{AssignTo: &a.accountCount, Text: "共 0 个账号"}, decl.PushButton{Text: "上一页", OnClicked: func() {
				if a.accountPageNumber > 1 {
					a.accountPageNumber--
					a.queryAccounts()
				}
			}}, decl.PushButton{Text: "下一页", OnClicked: func() {
				if a.accountPageNumber*50 < a.accountTotal {
					a.accountPageNumber++
					a.queryAccounts()
				}
			}}, decl.HSpacer{}}},
			decl.TableView{
				AssignTo: &a.accountTable, Model: a.accounts, StretchFactor: 1,
				AlternatingRowBG: true, LastColumnStretched: true, NotSortableByHeaderClick: true,
				OnCurrentIndexChanged: a.accountSelectionChanged,
				OnMouseDown: func(x, y int, button walk.MouseButton) {
					if button == walk.RightButton {
						a.accountTable.SetCurrentIndex(a.accountTable.IndexAt(x, y))
						a.updateAccountActions()
					}
				},
				Columns: []decl.TableViewColumn{
					{Title: "账号ID", Width: 75}, {Title: "账号", Width: 115}, {Title: "在线状态", Width: 70},
					{Title: "点卷", Width: 75}, {Title: "代币", Width: 75}, {Title: "角色数量", Width: 75},
					{Title: "登录IP", Width: 130}, {Title: "登录机器码", Width: 180}, {Title: "封禁状态", Width: 130}, {Title: "最近登录", Width: 160},
				},
				ContextMenuItems: []decl.MenuItem{
					decl.Action{AssignTo: &a.accountActions[0], Text: "查看该账号角色", Enabled: false, OnTriggered: a.showAccountCharacters},
					decl.Action{AssignTo: &a.accountActions[1], Text: "点卷发放", Enabled: false, OnTriggered: func() { a.showAccountGrant(a.points, 3) }},
					decl.Action{AssignTo: &a.accountActions[2], Text: "代币发放", Enabled: false, OnTriggered: func() { a.showAccountGrant(a.tokens, 4) }},
					decl.Action{AssignTo: &a.accountActions[3], Text: "封禁 / 解除账号封禁", Enabled: false, OnTriggered: a.banSelectedAccount},
				},
			},
			decl.Label{Text: "右键账号可查看角色、跳转发放及封禁/解封账号。数据库封禁不踢当前连接，下次登录生效。"},
			decl.GroupBox{Title: "账号详情与封禁状态", MinSize: decl.Size{Height: 100}, MaxSize: decl.Size{Height: 120}, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, Children: []decl.Widget{decl.TextEdit{AssignTo: &a.accountInfo, ReadOnly: true, VScroll: true, Text: "选中账号查看完整的登录IP、机器码、封禁状态与最近登录时间。"}}},
		},
	}
}

func (a *accountUI) characterPage() decl.TabPage {
	return decl.TabPage{
		Title: "角色查询", Layout: decl.VBox{Margins: decl.Margins{10, 10, 10, 10}, Spacing: 8},
		Children: []decl.Widget{
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true, Spacing: 8},
				Children: []decl.Widget{
					decl.Label{Text: "查询范围："},
					decl.ComboBox{AssignTo: &a.characterScope, Model: []string{"所有角色", "在线角色"}, CurrentIndex: 0, MinSize: decl.Size{Width: 120}, MaxSize: decl.Size{Width: 150}, OnCurrentIndexChanged: a.queryCharactersFirst},
					decl.Label{Text: "所属账号："}, decl.LineEdit{AssignTo: &a.ownerSearch, StretchFactor: 1},
					decl.Label{Text: "角色 / ID："}, decl.LineEdit{AssignTo: &a.characterSearch, StretchFactor: 1},
					decl.PushButton{Text: "查询", OnClicked: func() {
						a.queryCharactersFirst()
						logUIAction("账号管理", "角色查询", a.ownerSearch.Text(), a.characterCount.Text())
					}},
					decl.PushButton{Text: "重置", OnClicked: func() {
						a.ownerSearch.SetText("")
						a.characterSearch.SetText("")
						a.characterScope.SetCurrentIndex(0)
						a.queryCharacters()
					}},
				},
			},
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{AssignTo: &a.characterCount, Text: "共 0 个角色"}, decl.PushButton{Text: "上一页", OnClicked: func() {
				if a.characterPageNumber > 1 {
					a.characterPageNumber--
					a.queryCharacters()
				}
			}}, decl.PushButton{Text: "下一页", OnClicked: func() {
				if a.characterPageNumber*50 < a.characterTotal {
					a.characterPageNumber++
					a.queryCharacters()
				}
			}}, decl.HSpacer{}}},
			decl.TableView{
				AssignTo: &a.characterTable, Model: a.characters, StretchFactor: 1,
				AlternatingRowBG: true, LastColumnStretched: true, NotSortableByHeaderClick: true,
				OnCurrentIndexChanged: a.updateCharacterActions,
				OnMouseDown: func(x, y int, button walk.MouseButton) {
					if button == walk.RightButton {
						a.characterTable.SetCurrentIndex(a.characterTable.IndexAt(x, y))
						a.updateCharacterActions()
					}
				},
				Columns: []decl.TableViewColumn{
					{Title: "角色ID", Width: 90}, {Title: "角色名", Width: 160}, {Title: "所属账号", Width: 160}, {Title: "账号ID", Width: 90},
					{Title: "等级", Width: 70}, {Title: "职业（原始ID）", Width: 140}, {Title: "在线状态", Width: 100}, {Title: "金币", Width: 150},
				},
				ContextMenuItems: []decl.MenuItem{
					decl.Action{AssignTo: &a.characterActions[0], Text: "邮件发放", Enabled: false, OnTriggered: func() { a.showCharacterGrant(a.mail, 2) }},
					decl.Action{AssignTo: &a.characterActions[1], Text: "金币添加", Enabled: false, OnTriggered: func() { a.showCharacterGrant(a.gold, 5) }},
				},
			},
			decl.Label{Text: "右键角色可跳转到邮件发放、金币添加；重置可查看所有角色。"},
		},
	}
}

func (a *accountUI) refresh() {
	if a.status == nil || a.gold.output == nil {
		return
	}
	a.accountPageNumber = 1
	a.characterPageNumber = 1
	if currentManager() != nil {
		a.status.SetText("Go管理服务已连接 · 查询游戏数据库；在线状态来自有效游戏连接")

	} else {
		a.status.SetText("未连接Go管理服务，请先连接。")
		for _, f := range []*grantForm{a.mail, a.points, a.tokens, a.gold} {
			f.target.SetText("")
		}
	}
	a.queryAccounts()
	a.queryCharacters()
	for _, f := range []*grantForm{a.mail, a.points, a.tokens, a.gold} {
		f.inputsChanged()
	}
}

func (a *accountUI) queryAccounts() {
	if a.accountTable == nil || a.accountSearch == nil || a.accountCount == nil {
		return
	}
	a.queryRemoteAccounts()
}

func (a *accountUI) queryCharacters() {
	if a.characterTable == nil || a.ownerSearch == nil || a.characterSearch == nil || a.characterCount == nil {
		return
	}
	a.queryRemoteCharacters()
}

func (a *accountUI) selectedAccount() (accountRow, bool) {
	if a.accountTable != nil {
		i := a.accountTable.CurrentIndex()
		if i >= 0 && i < len(a.accounts.rows) {
			return a.accounts.rows[i], true
		}
	}
	return accountRow{}, false
}

func (a *accountUI) selectedCharacter() (characterRow, bool) {
	if a.characterTable != nil {
		i := a.characterTable.CurrentIndex()
		if i >= 0 && i < len(a.characters.rows) {
			return a.characters.rows[i], true
		}
	}
	return characterRow{}, false
}

func (a *accountUI) updateAccountActions() {
	_, ok := a.selectedAccount()
	for _, action := range a.accountActions {
		if action != nil {
			action.SetEnabled(ok && currentManager() != nil)
		}
	}
}

func (a *accountUI) accountSelectionChanged() {
	a.updateAccountActions()
	if a.accountInfo == nil {
		return
	}
	row, ok := a.selectedAccount()
	if !ok {
		a.accountInfo.SetText("选中账号查看完整的登录IP、机器码、封禁状态与最近登录时间。")
		return
	}
	state := "未知"
	if row.BanKnown {
		state = accountBanText(row.Banned)
	}
	details := fmt.Sprintf("账号：%s（ID：%d）\r\n登录IP：%s；登录机器码：%s\r\n在线状态：%s；封禁状态：%s\r\n最近登录：%s", row.Name, row.ID, reportedIdentity(row.LoginIP), reportedIdentity(row.MachineID), remoteOnlineText(row.OnlineStatus), state, reportedIdentity(row.LastLogin))
	a.accountInfo.SetText(details)
}

func (a *accountUI) banSelectedAccount() {
	if currentManager() != nil {
		a.databaseBan()
	}
}

func (a *accountUI) updateCharacterActions() {
	_, ok := a.selectedCharacter()
	for _, action := range a.characterActions {
		if action != nil {
			action.SetEnabled(ok)
		}
	}
}

func (a *accountUI) showAccountCharacters() {
	if row, ok := a.selectedAccount(); ok {
		a.characterPageNumber = 1
		a.ownerSearch.SetText(strconv.Itoa(row.ID))
		a.characterSearch.SetText("")
		a.characterScope.SetCurrentIndex(0)
		a.queryCharacters()
		a.tabs.SetCurrentIndex(1)
		logUIAction("账号管理", "查看账号角色", row.Name, fmt.Sprintf("账号ID：%d", row.ID))
	}
}

func (a *accountUI) showAccountGrant(form *grantForm, page int) {
	if row, ok := a.selectedAccount(); ok {
		form.scope.SetCurrentIndex(0)
		target := strconv.Itoa(row.ID)
		if currentManager() != nil {
			target = "id:" + target
		}
		form.target.SetText(target)
		form.inputsChanged()
		a.tabs.SetCurrentIndex(page)
		logUIAction("账号管理", "跳转"+form.title, row.Name, fmt.Sprintf("账号ID：%d", row.ID))
	}
}

func (a *accountUI) showCharacterGrant(form *grantForm, page int) {
	if row, ok := a.selectedCharacter(); ok {
		form.scope.SetCurrentIndex(0)
		target := strconv.Itoa(row.ID)
		if currentManager() != nil {
			target = "id:" + target
		}
		form.target.SetText(target)
		form.inputsChanged()
		a.tabs.SetCurrentIndex(page)
		logUIAction("账号管理", "跳转"+form.title, row.Name, fmt.Sprintf("角色ID：%d", row.ID))
	}
}
