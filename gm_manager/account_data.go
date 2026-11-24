//go:build windows

package main

import (
	"strings"

	"github.com/lxn/walk"
)

type accountRow struct {
	ID, Points, Tokens, CharacterCount        int
	Name                                      string
	Online                                    bool
	LoginIP, MachineID, LastLogin             string
	Banned                                    bool
	OnlineStatus                              string
	BanKnown                                  bool
	AccountBlocked, IPBlocked, MachineBlocked bool
}

type characterRow struct {
	ID, AccountID, Level, Gold int
	Name, AccountName, Job     string
	Online                     bool
	OnlineStatus               string
	GoldKnown                  bool
}

func onlineText(online bool) string {
	if online {
		return "在线"
	}
	return "离线"
}

func matches(keyword string, values ...string) bool {
	keyword = strings.ToLower(strings.TrimSpace(keyword))
	for _, value := range values {
		if strings.Contains(strings.ToLower(value), keyword) {
			return true
		}
	}
	return false
}

type accountTableModel struct {
	walk.TableModelBase
	rows []accountRow
}

func (m *accountTableModel) RowCount() int { return len(m.rows) }
func (m *accountTableModel) Value(row, col int) interface{} {
	if row < 0 || row >= len(m.rows) {
		return nil
	}
	r := m.rows[row]
	status, ban := onlineText(r.Online), accountBanText(r.Banned)
	if r.OnlineStatus != "" {
		status = remoteOnlineText(r.OnlineStatus)
		ban = "未知"
		if r.BanKnown {
			ban = "正常"
			if r.Banned {
				ban = "已封禁"
			}
		}
	}
	values := []interface{}{r.ID, r.Name, status, r.Points, r.Tokens, r.CharacterCount, reportedIdentity(r.LoginIP), reportedIdentity(r.MachineID), ban, reportedIdentity(r.LastLogin)}
	if col < 0 || col >= len(values) {
		return nil
	}
	return values[col]
}

func reportedIdentity(value string) string {
	if strings.TrimSpace(value) == "" {
		return "未上报"
	}
	return value
}

func accountBanText(banned bool) string {
	if banned {
		return "已封禁"
	}
	return "正常"
}

type characterTableModel struct {
	walk.TableModelBase
	rows []characterRow
}

func (m *characterTableModel) RowCount() int { return len(m.rows) }
func (m *characterTableModel) Value(row, col int) interface{} {
	if row < 0 || row >= len(m.rows) {
		return nil
	}
	r := m.rows[row]
	status := onlineText(r.Online)
	var gold any = r.Gold
	if r.OnlineStatus != "" {
		status = remoteOnlineText(r.OnlineStatus)
		if !r.GoldKnown {
			gold = "未知"
		}
	}
	values := []interface{}{r.ID, r.Name, r.AccountName, r.AccountID, r.Level, r.Job, status, gold}
	if col < 0 || col >= len(values) {
		return nil
	}
	return values[col]
}

func remoteOnlineText(status string) string {
	switch status {
	case "online":
		return "在线"
	case "offline":
		return "离线"
	default:
		return "未知"
	}
}
