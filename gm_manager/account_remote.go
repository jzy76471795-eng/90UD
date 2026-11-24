//go:build windows

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"newadmin/internal/adminapi"
)

func (a *accountUI) queryAccountsFirst()   { a.accountPageNumber = 1; a.queryAccounts() }
func (a *accountUI) queryCharactersFirst() { a.characterPageNumber = 1; a.queryCharacters() }

func (a *accountUI) clearRemoteRows() {
	a.accountSequence++
	a.characterSequence++
	a.accounts.rows = nil
	a.characters.rows = nil
	a.accounts.PublishRowsReset()
	a.characters.PublishRowsReset()
	a.accountTotal = 0
	a.characterTotal = 0
}
func (a *accountUI) queryRemoteAccounts() {
	a.accountSequence++
	sequence := a.accountSequence
	if a.accountPageNumber < 1 {
		a.accountPageNumber = 1
	}
	scope := []string{"all", "online", "banned"}
	index := a.accountScope.CurrentIndex()
	if index < 0 || index >= len(scope) {
		index = 0
	}
	path := fmt.Sprintf("/manage/v1/accounts?scope=%s&keyword=%s&page=%d&page_size=50", scope[index], url.QueryEscape(a.accountSearch.Text()), a.accountPageNumber)
	a.accountCount.SetText("正在查询账号…")
	var page adminapi.Page[adminapi.Account]
	managerJob("账号管理", "账号查询", func(c *adminapi.Client) error { return c.Do(context.Background(), "GET", path, "", nil, &page) }, func(err error) {
		if sequence != a.accountSequence {
			return
		}
		a.accountTable.SetCurrentIndex(-1)
		a.accounts.rows = nil
		if err != nil {
			a.accountTotal = 0
			a.accountCount.SetText(err.Error())
		} else {
			for _, row := range page.Items {
				a.accounts.rows = append(a.accounts.rows, accountRow{ID: row.ID, Name: row.Name, Points: int(row.Points), Tokens: int(row.Tokens), CharacterCount: row.CharacterCount, Online: row.OnlineStatus == "online", OnlineStatus: row.OnlineStatus, LoginIP: row.LoginIP, MachineID: row.MachineID, LastLogin: row.LastLogin, Banned: row.Banned, BanKnown: row.BanKnown, AccountBlocked: row.AccountBlocked, IPBlocked: row.IPBlocked, MachineBlocked: row.MachineBlocked})
			}
			a.accountTotal = page.Total
			a.accountCount.SetText(fmt.Sprintf("共 %d 个账号，第 %d 页，本页 %d 条", page.Total, page.Page, len(page.Items)))
		}
		a.accounts.PublishRowsReset()
		a.accountSelectionChanged()
	})
}
func (a *accountUI) queryRemoteCharacters() {
	a.characterSequence++
	sequence := a.characterSequence
	if a.characterPageNumber < 1 {
		a.characterPageNumber = 1
	}
	scope := "all"
	if a.characterScope.CurrentIndex() == 1 {
		scope = "online"
	}
	path := fmt.Sprintf("/manage/v1/characters?scope=%s&account=%s&keyword=%s&page=%d&page_size=50", scope, url.QueryEscape(a.ownerSearch.Text()), url.QueryEscape(a.characterSearch.Text()), a.characterPageNumber)
	a.characterCount.SetText("正在查询角色…")
	var page adminapi.Page[adminapi.Character]
	managerJob("账号管理", "角色查询", func(c *adminapi.Client) error { return c.Do(context.Background(), "GET", path, "", nil, &page) }, func(err error) {
		if sequence != a.characterSequence {
			return
		}
		a.characterTable.SetCurrentIndex(-1)
		a.characters.rows = nil
		if err != nil {
			a.characterTotal = 0
			a.characterCount.SetText(err.Error())
		} else {
			for _, row := range page.Items {
				gold := 0
				if row.Gold != nil {
					gold = int(*row.Gold)
				}
				a.characters.rows = append(a.characters.rows, characterRow{ID: row.ID, AccountID: row.AccountID, Name: row.Name, AccountName: row.AccountName, Level: row.Level, Job: row.Job, Gold: gold, GoldKnown: row.Gold != nil, Online: row.OnlineStatus == "online", OnlineStatus: row.OnlineStatus})
			}
			a.characterTotal = page.Total
			a.characterCount.SetText(fmt.Sprintf("共 %d 个角色，第 %d 页，本页 %d 条", page.Total, page.Page, len(page.Items)))
		}
		a.characters.PublishRowsReset()
		a.updateCharacterActions()
	})
}

type pendingGrant struct {
	ID       string          `json:"id"`
	Endpoint string          `json:"endpoint"`
	Action   adminapi.Action `json:"action"`
}

func (f *grantForm) pendingPath() string {
	return filepath.Join(localDataDirectory(), "pending-"+f.title+".json")
}
func (f *grantForm) databaseAction() (adminapi.Action, error) {
	action := adminapi.Action{Name: "currency.credit", Scope: "single", Target: strings.TrimSpace(f.target.Text()), Confirm: true, Currency: "cera"}
	switch f.scope.CurrentIndex() {
	case 0:
		if action.Target == "" {
			return action, fmt.Errorf("请填写准确ID或名称")
		}
	case 1:
		action.Scope = "all"
	case 2:
		action.Scope = "online"
	default:
		return action, fmt.Errorf("请选择范围")
	}
	if f == f.owner.tokens {
		action.Currency = "token_cera"
	}
	if f.characterTarget {
		action.Currency = "gold"
	}
	if f.mail {
		action.Name = "mail.send"
		action.Currency = ""
		action.Body = f.body.Text()
		reward := f.rewards.bundle()
		if err := reward.validate(10); err != nil {
			return action, err
		}
		if reward.empty() && strings.TrimSpace(action.Body) == "" {
			return action, fmt.Errorf("请填写正文或奖励")
		}
		action.Reward = wireReward(reward)
	} else {
		action.Amount = int64(f.amount.Value())
	}
	return action, nil
}
func (f *grantForm) execute() {
	action, err := f.databaseAction()
	if err != nil {
		f.output.SetText(err.Error())
		return
	}
	if currentManager() == nil {
		f.output.SetText("请先连接管理服务。")
		return
	}
	if _, err = os.Stat(f.pendingPath()); err == nil {
		f.output.SetText("有待核对请求，请先查询原回执。")
		return
	}
	details := fmt.Sprintf("币种：%s，追加：%d", action.Currency, action.Amount)
	if f.mail {
		details = f.rewards.bundle().description() + "\r\n正文：" + action.Body
	}
	if !walkConfirm(f.owner.window, "确认数据库发放", fmt.Sprintf("范围：%s\r\n目标：%s\r\n%s\r\n此操作直接追加数据库资产，在线显示可能需要重新登录。\r\n是否提交？", f.scopeNames()[f.scope.CurrentIndex()], action.Target, details)) {
		return
	}
	f.send.SetEnabled(false)
	id := adminapi.NewRequestID()
	var receipt adminapi.Receipt
	managerJob("账号管理", f.title, func(c *adminapi.Client) error {
		if err := os.MkdirAll(localDataDirectory(), 0700); err != nil {
			return err
		}
		raw, _ := json.Marshal(pendingGrant{ID: id, Endpoint: c.Endpoint(), Action: action})
		if err := os.WriteFile(f.pendingPath(), raw, 0600); err != nil {
			return fmt.Errorf("无法保存本机请求编号，未发送")
		}
		return c.Do(context.Background(), "POST", "/manage/v1/actions", id, action, &receipt)
	}, func(err error) {
		f.send.SetEnabled(true)
		if err != nil {
			if failure, ok := err.(*adminapi.ClientError); ok && !failure.Unknown {
				_ = os.Remove(f.pendingPath())
			}
			f.output.SetText(err.Error() + "\r\n请求：" + id + "\r\n请查询原回执后再决定是否重新发放。")
			return
		}
		_ = os.Remove(f.pendingPath())
		f.output.SetText(fmt.Sprintf("请求：%s\r\n结果：%s\r\n%s", receipt.RequestID, receipt.State, receipt.Message))
		logUIAction("账号管理", f.title, action.Target, receipt.Message)
		f.owner.queryAccounts()
		f.owner.queryCharacters()
	})
}
func (f *grantForm) queryReceipt() {
	raw, err := os.ReadFile(f.pendingPath())
	if err != nil {
		f.output.SetText("没有待核对的本机发放请求。")
		return
	}
	var pending pendingGrant
	if json.Unmarshal(raw, &pending) != nil {
		f.output.SetText("本机请求记录格式无效，请保留文件核对。")
		return
	}
	var receipt adminapi.Receipt
	if currentManager() == nil || currentManager().Endpoint() != pending.Endpoint {
		f.output.SetText("请连接登记该请求时的原管理地址，再查询回执。\r\n原请求编号：" + pending.ID)
		return
	}
	managerJob("账号管理", "查询原回执", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "GET", "/manage/v1/receipts/"+url.PathEscape(pending.ID), "", nil, &receipt)
	}, func(err error) {
		if err != nil {
			if failure, ok := err.(*adminapi.ClientError); ok && !failure.Unknown && failure.Status == 404 {
				_ = os.Remove(f.pendingPath())
				f.output.SetText(err.Error() + "\r\n原请求没有已确认的成功回执，本机待核对标记已解除。")
				return
			}
			f.output.SetText(err.Error() + "\r\n原请求编号：" + pending.ID)
			return
		}
		if receipt.State == "confirmed" || receipt.State == "database_committed" {
			_ = os.Remove(f.pendingPath())
		}
		f.output.SetText(fmt.Sprintf("原请求：%s\r\n结果：%s\r\n%s", pending.ID, receipt.State, receipt.Message))
	})
}
