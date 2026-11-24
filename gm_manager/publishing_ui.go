//go:build windows

package main

import (
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
)

type publishingUI struct {
	configRead                                   configurationReadState
	saveConfigButton                             *walk.PushButton
	releaseVersion                               *walk.LineEdit
	releaseEnabled                               *walk.CheckBox
	lastPVF                                      *PVFResult
	window                                       *walk.MainWindow
	pvfSource, pvfDirectory, updateURL           *walk.LineEdit
	pvfOutput                                    *walk.TextEdit
	launcherName, launcherDirectory              *walk.LineEdit
	downloaderName, clientURL, archiveName       *walk.LineEdit
	archiveLauncher, downloaderDirectory         *walk.LineEdit
	buildOutput                                  *walk.TextEdit
	pvfKey, loginURL, serverID, loginCertificate *walk.LineEdit
	pvfBusy, buildBusy                           bool
	revision                                     int64
}

func (p *publishingUI) pvfPage() decl.TabPage {
	configurationLoads = append(configurationLoads, p.loadPublishing, p.fillPlayerDefaults)
	return decl.TabPage{
		Title: "PVF管理", Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 10},
		Children: []decl.Widget{
			decl.Label{Text: "本机PVF换钥、完整性校验与密钥清单生成；文件保存在所选输出目录，发布到游戏另行处理。"},
			decl.GroupBox{
				Title: "PVF加密", Layout: decl.VBox{Margins: decl.Margins{10, 10, 10, 10}, Spacing: 8},
				Children: []decl.Widget{
					decl.Composite{
						Layout: decl.HBox{MarginsZero: true, Spacing: 8},
						Children: []decl.Widget{
							decl.Label{Text: "PVF文件：", MinSize: decl.Size{Width: 110}},
							decl.LineEdit{AssignTo: &p.pvfSource, StretchFactor: 1, CueBanner: "选择要加密的 .pvf 文件", OnTextChanged: p.pvfInputsChanged},
							decl.PushButton{Text: "选择PVF", OnClicked: p.choosePVF},
						},
					},
					p.directoryRow("输出目录：", &p.pvfDirectory, true),
					decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{Text: "原96位密钥：", MinSize: decl.Size{Width: 110}}, decl.LineEdit{AssignTo: &p.pvfKey, StretchFactor: 1, PasswordMode: true, CueBanner: "标准或兼容PVF可留空；已换钥文件填写原密钥"}}},
					decl.Label{Text: "处理流程：本机换钥 → 内容等价校验 → 保存密钥与校验清单。游戏发布另行处理。"},
					decl.Composite{
						Layout:   decl.HBox{MarginsZero: true, Spacing: 8},
						Children: []decl.Widget{decl.PushButton{Text: "执行本机换钥与校验", OnClicked: p.buildPVF}, decl.HSpacer{}},
					},
				},
			},

			decl.GroupBox{Title: "PVF版本发布", Layout: decl.VBox{Margins: decl.Margins{10, 8, 10, 8}, Spacing: 6}, Children: []decl.Widget{
				decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{Text: "版本号："}, decl.LineEdit{AssignTo: &p.releaseVersion, StretchFactor: 1, CueBanner: "留空使用当前时间"}, decl.CheckBox{AssignTo: &p.releaseEnabled, Text: "发布后启用", Checked: true}, decl.PushButton{Text: "读取本机校验清单", OnClicked: p.loadPVFResult}, decl.PushButton{Text: "发布本机结果", OnClicked: p.publishPVF}, decl.PushButton{Text: "查询版本", OnClicked: p.readPVFVersions}, decl.PushButton{Text: "停用当前版本", OnClicked: p.disableCurrentPVF}}},
			}},
			decl.GroupBox{
				Title: "更新包直链", Layout: decl.VBox{Margins: decl.Margins{10, 10, 10, 10}, Spacing: 8},
				Children: []decl.Widget{
					decl.Composite{
						Layout: decl.HBox{MarginsZero: true, Spacing: 8},
						Children: []decl.Widget{
							decl.Label{Text: "直链地址：", MinSize: decl.Size{Width: 110}},
							decl.LineEdit{AssignTo: &p.updateURL, StretchFactor: 1, CueBanner: "https://example.com/update.zip", OnTextChanged: p.pvfInputsChanged},
							decl.PushButton{AssignTo: &p.saveConfigButton, Enabled: false, Text: "保存管理配置", OnClicked: p.savePublishing}, decl.PushButton{Text: "读取下载配置", OnClicked: p.loadPublishing},
						},
					},
					decl.Label{Text: "直链保存后同步当前已发布的玩家版本。更换服务器源文件需先停服并完成本机备份。"},
				},
			},
			decl.GroupBox{
				Title: "处理结果", StretchFactor: 1, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}},
				Children: []decl.Widget{decl.TextEdit{AssignTo: &p.pvfOutput, ReadOnly: true, VScroll: true, Text: "选择PVF文件与输出目录后，点击“执行本机换钥与校验”。"}},
			},
		},
	}
}

func (p *publishingUI) launcherPage() decl.TabPage {
	return decl.TabPage{
		Title: "登录下载管理", Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 10},
		Children: []decl.Widget{
			decl.Label{Text: "本机生成登陆器和下载器，输出目录可自行选择。"},
			decl.GroupBox{
				Title: "生成登陆器", Layout: decl.VBox{Margins: decl.Margins{10, 10, 10, 10}, Spacing: 8},
				Children: []decl.Widget{
					decl.Composite{
						Layout: decl.HBox{MarginsZero: true, Spacing: 8},
						Children: []decl.Widget{
							decl.Label{Text: "登陆器名称：", MinSize: decl.Size{Width: 110}},
							decl.LineEdit{AssignTo: &p.launcherName, MinSize: decl.Size{Width: 220}, MaxSize: decl.Size{Width: 220}, CueBanner: "例如：90US登陆器", OnTextChanged: p.buildInputsChanged},
							decl.HSpacer{},
						},
					},
					p.directoryRow("输出目录：", &p.launcherDirectory, false),
					decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{Text: "玩家登录地址："}, decl.LineEdit{AssignTo: &p.loginURL, StretchFactor: 2, CueBanner: "https://游戏服务器:玩家端口"}, decl.Label{Text: "服务器标识："}, decl.LineEdit{AssignTo: &p.serverID, Text: "90us", StretchFactor: 1}}},
					decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{Text: "玩家服务公钥证书："}, decl.LineEdit{AssignTo: &p.loginCertificate, StretchFactor: 1, CueBanner: "填写玩家HTTPS服务的PEM公钥证书路径，不包含私钥"}}},
					decl.Composite{
						Layout:   decl.HBox{MarginsZero: true, Spacing: 8},
						Children: []decl.Widget{decl.PushButton{Text: "生成本机登陆器", OnClicked: p.buildLauncher}, decl.HSpacer{}},
					},
				},
			},
			decl.GroupBox{
				Title: "生成下载器", Layout: decl.VBox{Margins: decl.Margins{10, 10, 10, 10}, Spacing: 8},
				Children: []decl.Widget{
					decl.Composite{
						Layout: decl.HBox{MarginsZero: true, Spacing: 8},
						Children: []decl.Widget{
							decl.Label{Text: "下载器名称：", MinSize: decl.Size{Width: 110}},
							decl.LineEdit{AssignTo: &p.downloaderName, MinSize: decl.Size{Width: 220}, MaxSize: decl.Size{Width: 220}, CueBanner: "例如：90US下载器", OnTextChanged: p.buildInputsChanged},
							decl.Label{Text: "下载文件名："},
							decl.LineEdit{AssignTo: &p.archiveName, MinSize: decl.Size{Width: 220}, MaxSize: decl.Size{Width: 220}, CueBanner: "例如：客户端.zip", OnTextChanged: p.buildInputsChanged}, decl.HSpacer{},
						},
					},
					decl.Composite{
						Layout: decl.HBox{MarginsZero: true, Spacing: 8},
						Children: []decl.Widget{
							decl.Label{Text: "客户端直链：", MinSize: decl.Size{Width: 110}},
							decl.LineEdit{AssignTo: &p.clientURL, StretchFactor: 1, CueBanner: "填写客户端压缩包的直接下载地址", OnTextChanged: p.buildInputsChanged},
						},
					},
					decl.Composite{
						Layout: decl.HBox{MarginsZero: true, Spacing: 8},
						Children: []decl.Widget{
							decl.Label{Text: "包内登陆器：", MinSize: decl.Size{Width: 110}},
							decl.LineEdit{AssignTo: &p.archiveLauncher, StretchFactor: 1, CueBanner: "例如：登陆器.exe 或 客户端\\登陆器.exe", OnTextChanged: p.buildInputsChanged},
						},
					},
					p.directoryRow("输出目录：", &p.downloaderDirectory, false),
					decl.Composite{
						Layout: decl.HBox{MarginsZero: true, Spacing: 12},
						Children: []decl.Widget{
							decl.PushButton{Text: "生成本机下载器", OnClicked: p.buildDownloader},
							decl.Label{Text: "下载完成后自动解压，再启动包内指定的登陆器。"}, decl.HSpacer{},
						},
					},
				},
			},
			decl.GroupBox{
				Title: "生成结果与提示", StretchFactor: 1, MinSize: decl.Size{Height: 100}, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}},
				Children: []decl.Widget{decl.TextEdit{AssignTo: &p.buildOutput, ReadOnly: true, VScroll: true, Text: "填写完整参数后，点击对应按钮生成本机产物。"}},
			},
		},
	}
}

func (p *publishingUI) directoryRow(label string, target **walk.LineEdit, pvf bool) decl.Composite {
	changed := p.buildInputsChanged
	if pvf {
		changed = p.pvfInputsChanged
	}
	return decl.Composite{
		Layout: decl.HBox{MarginsZero: true, Spacing: 8},
		Children: []decl.Widget{
			decl.Label{Text: label, MinSize: decl.Size{Width: 110}},
			decl.LineEdit{AssignTo: target, StretchFactor: 1, CueBanner: "选择输出目录", OnTextChanged: changed},
			decl.PushButton{Text: "选择目录", OnClicked: func() {
				dialog := &walk.FileDialog{Title: "选择输出目录", FilePath: (*target).Text()}
				if ok, err := dialog.ShowBrowseFolder(p.window); err != nil {
					p.showOutput(pvf, "选择目录失败："+err.Error())
				} else if ok {
					(*target).SetText(dialog.FilePath)
				}
			}},
		},
	}
}

func (p *publishingUI) choosePVF() {
	dialog := &walk.FileDialog{Title: "选择PVF文件", Filter: "PVF文件 (*.pvf)|*.pvf|所有文件 (*.*)|*.*", FilePath: p.pvfSource.Text()}
	if ok, err := dialog.ShowOpen(p.window); err != nil {
		p.pvfOutput.SetText("选择文件失败：" + err.Error())
	} else if ok {
		p.pvfSource.SetText(dialog.FilePath)
	}
}

func (p *publishingUI) showOutput(pvf bool, text string) {
	output := p.buildOutput
	if pvf {
		output = p.pvfOutput
	}
	if output != nil {
		output.SetText(text)
	}
}

func (p *publishingUI) pvfInputsChanged() {
	p.showOutput(true, "设置已修改。可执行PVF换钥与校验，或保存更新包直链。")
}

func (p *publishingUI) buildInputsChanged() {
	p.showOutput(false, "设置已修改，点击对应的生成按钮创建程序。")
}

func checkedOutputDirectory(value string) (string, error) {
	value = strings.TrimSpace(value)
	info, err := os.Stat(value)
	if !filepath.IsAbs(value) || err != nil || !info.IsDir() {
		return "", fmt.Errorf("请选择存在的输出目录。")
	}
	return filepath.Clean(value), nil
}

func checkedDirectURL(value string) (string, error) {
	value = strings.TrimSpace(value)
	parsed, err := url.Parse(value)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Hostname() == "" || parsed.User != nil {
		return "", fmt.Errorf("请填写有效的HTTP或HTTPS直链地址。")
	}
	return value, nil
}

func validWindowsFileName(value string) bool {
	if value == "" || strings.ContainsAny(value, "\\/:*?\"<>|\x00\r\n") || strings.HasSuffix(value, ".") || strings.HasSuffix(value, " ") {
		return false
	}
	for _, character := range value {
		if character < 32 {
			return false
		}
	}
	base := strings.ToUpper(strings.SplitN(value, ".", 2)[0])
	if base == "CON" || base == "PRN" || base == "AUX" || base == "NUL" {
		return false
	}
	if len(base) == 4 && (strings.HasPrefix(base, "COM") || strings.HasPrefix(base, "LPT")) && base[3] >= '1' && base[3] <= '9' {
		return false
	}
	return true
}

func checkedExecutableName(value string) (string, error) {
	value = strings.TrimSpace(value)
	if strings.EqualFold(filepath.Ext(value), ".exe") {
		value = value[:len(value)-4]
	}
	if !validWindowsFileName(value) {
		return "", fmt.Errorf("请填写有效名称，不要填写目录路径。")
	}
	return value + ".exe", nil
}

func checkedArchiveLauncher(value string) (string, error) {
	value = strings.ReplaceAll(strings.TrimSpace(value), "/", "\\")
	if value == "" || filepath.IsAbs(value) || strings.HasPrefix(value, "\\") || strings.Contains(value, ":") {
		return "", fmt.Errorf("包内登陆器请填写压缩包中的相对路径，例如“客户端\\登陆器.exe”。")
	}
	for _, component := range strings.Split(value, "\\") {
		if component != "." && !validWindowsFileName(component) {
			return "", fmt.Errorf("包内登陆器路径不能包含上级目录或无效文件名。")
		}
	}
	if !strings.EqualFold(filepath.Ext(value), ".exe") {
		return "", fmt.Errorf("包内登陆器必须指向一个 .exe 文件。")
	}
	return filepath.Clean(value), nil
}
