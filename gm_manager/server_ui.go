//go:build windows

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
	"newadmin/internal/adminapi"
)

var globalServer *serverUI

type serverUI struct {
	ports                       *textTableModel
	output                      *walk.TextEdit
	address, token, certificate *walk.LineEdit
	status                      *walk.Label
	connect, refresh            *walk.PushButton
	generation                  int
	force                       *walk.CheckBox
	hostname                    string
	operations                  []*walk.PushButton
	fields                      map[string]**walk.Label
}

func newServerUI() *serverUI {
	return &serverUI{ports: &textTableModel{}, fields: map[string]**walk.Label{}, operations: make([]*walk.PushButton, 5)}
}
func (s *serverUI) panel(title string, names []string) decl.GroupBox {
	children := []decl.Widget{}
	for _, name := range names {
		target := new(*walk.Label)
		s.fields[name] = target
		children = append(children, decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{Text: name + "：", MinSize: decl.Size{Width: 100}}, decl.Label{Text: "未获取", AssignTo: target}, decl.HSpacer{}}})
	}
	return decl.GroupBox{Title: title, StretchFactor: 1, Layout: decl.VBox{Margins: decl.Margins{10, 8, 10, 8}, Spacing: 4}, Children: children}
}
func (s *serverUI) page() decl.TabPage {
	address, certificate := "http://127.0.0.1:17890", ""
	if raw, err := os.ReadFile(filepath.Join(localDataDirectory(), "connection.json")); err == nil {
		var saved map[string]string
		if json.Unmarshal(raw, &saved) == nil {
			address, certificate = saved["address"], saved["certificate"]
		}
	}
	return decl.TabPage{Title: "服务器管理", Layout: decl.VBox{Margins: decl.Margins{12, 10, 12, 10}, Spacing: 8}, Children: []decl.Widget{
		decl.GroupBox{Title: "Go管理服务连接", Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}, Spacing: 6}, Children: []decl.Widget{
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{Text: "接口地址："}, decl.LineEdit{AssignTo: &s.address, Text: address, StretchFactor: 2}, decl.Label{Text: "管理令牌："}, decl.LineEdit{AssignTo: &s.token, PasswordMode: true, StretchFactor: 2}, decl.PushButton{AssignTo: &s.connect, Text: "连接 / 验证", OnClicked: s.connectManager}, decl.PushButton{Text: "断开", OnClicked: s.disconnect}}},
			decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{Text: "信任证书："}, decl.LineEdit{AssignTo: &s.certificate, Text: certificate, StretchFactor: 1, CueBanner: "自签名证书填写本机PEM路径；正规证书可留空"}, decl.PushButton{Text: "读取本机配置", OnClicked: s.loadLocalConfig}, decl.Label{AssignTo: &s.status, Text: "尚未连接"}}},
		}},

		decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{
			decl.PushButton{AssignTo: &s.refresh, Text: "刷新状态", OnClicked: s.refreshStatus},
			decl.PushButton{AssignTo: &s.operations[0], Text: "启动游戏", OnClicked: func() { s.operate("start") }},
			decl.PushButton{AssignTo: &s.operations[1], Text: "停止游戏", OnClicked: func() { s.operate("stop") }},
			decl.PushButton{AssignTo: &s.operations[2], Text: "重启游戏", OnClicked: func() { s.operate("restart") }},
			decl.PushButton{AssignTo: &s.operations[3], Text: "重启服务器系统", OnClicked: func() { s.operate("reboot") }}, decl.PushButton{AssignTo: &s.operations[4], Text: "取消系统重启", OnClicked: func() { s.operate("cancel_reboot") }}, decl.CheckBox{AssignTo: &s.force, Text: "允许强制停止在线连接"}, decl.HSpacer{}}},
		decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 10}, Children: []decl.Widget{s.panel("管理服务信息", []string{"服务器地址", "操作系统", "管理版本", "运行时长", "进程 / 协程", "Go堆内存"}), s.panel("游戏与数据库", []string{"服务端目录", "游戏连接", "数据库", "数据库状态", "在线账号", "在线角色"})}},
		decl.GroupBox{Title: "服务器本机端口检测", MinSize: decl.Size{Height: 110}, MaxSize: decl.Size{Height: 140}, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, Children: []decl.Widget{decl.TableView{Model: s.ports, AlternatingRowBG: true, LastColumnStretched: true, NotSortableByHeaderClick: true, Columns: []decl.TableViewColumn{{Title: "用途", Width: 180}, {Title: "协议", Width: 100}, {Title: "端口", Width: 100}, {Title: "本机连通", Width: 180}, {Title: "公网连通", Width: 180}}}}},
		decl.GroupBox{Title: "连接与操作结果", StretchFactor: 1, MinSize: decl.Size{Height: 100}, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, Children: []decl.Widget{decl.TextEdit{AssignTo: &s.output, ReadOnly: true, VScroll: true, Text: "连接Go管理服务后显示实际状态。连接配置保存在程序旁的“管理配置”目录，可随完整管理工具迁移，不绑定Windows账户。"}}},
	}}
}
func (s *serverUI) loadLocalConfig() {
	if s.loadProfile() {
		s.output.SetText("已读取便携连接配置，点击连接验证。")
		return
	}
	if _, err := os.Stat(filepath.Join(localDataDirectory(), "remote-profile.json")); err == nil {
		return // Keep the actionable profile error instead of replacing it with a local-service hint.
	}
	path := filepath.Join(localDataDirectory(), "go-manager.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		s.output.SetText("未找到本机配置。请先运行交付目录的“启动本机管理服务.cmd”。")
		return
	}
	var config struct {
		Listen      string `json:"listen"`
		Token       string `json:"token"`
		Certificate string `json:"tls_certificate"`
	}
	if json.Unmarshal(raw, &config) != nil {
		s.output.SetText("本机配置格式无效")
		return
	}
	scheme := "http://"
	if config.Certificate != "" {
		scheme = "https://"
	}
	s.address.SetText(scheme + config.Listen)
	s.token.SetText(config.Token)
	s.certificate.SetText(config.Certificate)
	s.output.SetText("已读取本机配置，尚未连接。点击“连接 / 验证”检查状态。")
}
func (s *serverUI) connectManager() {
	client, err := adminapi.NewClient(s.address.Text(), s.token.Text(), s.certificate.Text())
	if err != nil {
		s.output.SetText(err.Error())
		return
	}
	s.generation++
	generation := s.generation
	setManager(nil)
	if globalAccounts != nil {
		globalAccounts.clearRemoteRows()
	}
	s.connect.SetEnabled(false)
	s.status.SetText("正在验证…")
	address, token, certificate := s.address.Text(), s.token.Text(), s.certificate.Text()
	go func() {
		var status adminapi.Status
		err := client.Do(context.Background(), "GET", "/manage/v1/status", "", nil, &status)
		applicationWindow.Synchronize(func() {
			if generation != s.generation {
				client.Close()
				return
			}
			s.connect.SetEnabled(true)
			if err != nil {
				client.Close()
				s.status.SetText("连接失败")
				s.output.SetText(err.Error())
				return
			}
			setManager(client)
			for _, load := range configurationLoads {
				load()
			}
			profileErr := saveConnectionProfile(address, token, certificate)
			s.status.SetText("Go管理服务已连接")
			s.renderStatus(status)
			if profileErr != nil {
				s.output.SetText(s.output.Text() + "\r\n管理服务已连接，但便携配置保存失败：" + profileErr.Error())
			}
			s.refreshLifecycle()
			if globalAccounts != nil {
				globalAccounts.refresh()
			}
			logUIAction("服务器管理", "连接Go管理服务", address, "认证与状态接口通过；游戏可用状态单独报告")
		})
	}()
}
func (s *serverUI) disconnect() {
	s.generation++
	s.connect.SetEnabled(true)
	setManager(nil)
	s.token.SetText("")
	s.status.SetText("已断开")
	s.ports.rows = nil
	s.ports.PublishRowsReset()
	for _, target := range s.fields {
		if *target != nil {
			(*target).SetText("未获取")
		}
	}
	s.output.SetText("已断开管理连接并清除本次窗口令牌。最后一次状态不再代表当前状态。")
	if globalAccounts != nil {
		globalAccounts.clearRemoteRows()
		globalAccounts.refresh()
	}
}
func (s *serverUI) refreshStatus() {
	s.refresh.SetEnabled(false)
	var status adminapi.Status
	managerJob("服务器管理", "刷新状态", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "GET", "/manage/v1/status", "", nil, &status)
	}, func(err error) {
		s.refresh.SetEnabled(true)
		if err != nil {
			s.output.SetText(err.Error())
			return
		}
		s.renderStatus(status)
		s.refreshLifecycle()
	})
}
func (s *serverUI) renderStatus(status adminapi.Status) {
	s.hostname = status.Hostname
	set := func(name, value string) {
		if target := s.fields[name]; target != nil && *target != nil {
			(*target).SetText(value)
		}
	}
	number := func(value *int) string {
		if value == nil {
			return "未知"
		}
		return strconv.Itoa(*value)
	}
	states := map[string]string{"unconfigured": "未配置", "unavailable": "不可用", "read_only_connected": "只读连接正常", "read_write_connected": "数据库直写连接正常", "unknown": "未知", "connected": "已连接"}
	set("服务器地址", s.address.Text())
	set("操作系统", status.OS)
	set("管理版本", status.Version+" / "+status.Language)
	set("运行时长", fmt.Sprintf("%d 秒", status.UptimeSeconds))
	set("进程 / 协程", fmt.Sprintf("%d / %d", status.PID, status.Goroutines))
	set("Go堆内存", fmt.Sprintf("%.1f MB", float64(status.MemoryBytes)/1048576))
	set("服务端目录", status.ServerRoot)
	set("游戏连接", states[status.GameState])
	set("数据库", status.Database)
	set("数据库状态", states[status.DatabaseState])
	set("在线账号", number(status.OnlineAccounts))
	set("在线角色", number(status.OnlineCharacters))
	s.ports.rows = nil
	for _, port := range status.Ports {
		s.ports.rows = append(s.ports.rows, []string{port.Name, port.Protocol, strconv.Itoa(port.Port), port.Listening, port.Reachable})
	}
	s.ports.PublishRowsReset()
	s.output.SetText(fmt.Sprintf("管理服务认证通过。\r\n核对时间：%s\r\n游戏状态：%s；数据库状态：%s\r\n可用能力：%s\r\n\r\n端口检测仅表示服务器本机连通，公网可达性尚未检测。", status.CheckedAt.In(chinaTime).Format("2006-01-02 15:04:05"), states[status.GameState], states[status.DatabaseState], strings.Join(status.Capabilities, ", ")))
}
