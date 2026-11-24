//go:build windows

package main

import (
	"runtime"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
	"github.com/lxn/win"
)

// Match the existing GM window's default size and screen adaptation.
func windowSize() (size, minimum decl.Size) {
	width, height := int(win.GetSystemMetrics(win.SM_CXSCREEN)), int(win.GetSystemMetrics(win.SM_CYSCREEN))
	if width <= 0 || height <= 0 {
		width, height = 1280, 720
	}
	width, height = width-40, height-80
	if width <= 0 {
		width = 1240
	}
	if height <= 0 {
		height = 640
	}
	width, height = min(width, 1280), min(height, 760)
	return decl.Size{Width: width, Height: height}, decl.Size{Width: min(width, 900), Height: min(height, 520)}
}

func managementPages(accounts *accountUI, cdks *cdkUI, publishing *publishingUI) []decl.TabPage {
	titles := []string{
		"服务器管理",
		"账号管理",
		"充值设置",
		"CDK管理",
		"公告管理",
		"在线福利",
		"注册管理",
		"PVF管理",
		"登录下载管理",
		"记录查询",
		"操作日志",
		"一键部署",
	}
	pages := make([]decl.TabPage, len(titles))
	for i, title := range titles {
		pages[i] = decl.TabPage{
			Title:      title,
			Background: decl.SolidColorBrush{Color: walk.RGB(255, 255, 255)},
			Layout:     decl.VBox{MarginsZero: true, SpacingZero: true},
		}
	}
	globalServer = newServerUI()
	pages[0] = globalServer.page()
	pages[1] = accounts.page()
	pages[2] = rechargePage()
	pages[3] = cdks.page()
	pages[4] = announcementPage()
	pages[5] = welfarePage()
	pages[6] = registrationPage()
	pages[7] = publishing.pvfPage()
	pages[8] = publishing.launcherPage()
	pages[9] = recordsPage()
	pages[10] = managementAudit.page()
	pages[11] = deploymentPage()
	return pages
}

func main() {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()

	var window *walk.MainWindow
	loadLocalAudit()
	accounts := newAccountUI()
	cdks := newCDKUI()
	publishing := &publishingUI{}
	size, minimum := windowSize()
	err := (decl.MainWindow{
		AssignTo:   &window,
		Title:      "新版管理系统 - 数据库直写完善版",
		Size:       size,
		MinSize:    minimum,
		Background: decl.SolidColorBrush{Color: walk.RGB(255, 255, 255)},
		Layout:     decl.VBox{MarginsZero: true, SpacingZero: true},
		Children: []decl.Widget{
			decl.TabWidget{
				ContentMarginsZero: true,
				Pages:              managementPages(accounts, cdks, publishing),
			},
		},
	}).Create()
	if err != nil {
		walk.MsgBox(nil, "新版管理系统", "无法创建窗口：\n"+err.Error(), walk.MsgBoxIconError)
		return
	}
	accounts.window = window
	applicationWindow = window
	globalAccounts = accounts
	publishing.window = window
	accounts.refresh()
	logUIAction("系统", "启动GM后台", "本机界面", "界面已启动，Go管理接口待连接。")
	go func() {
		window.Synchronize(func() {
			if globalServer.loadProfile() {
				globalServer.connectManager()
			}
		})
	}()
	window.Run()
	setManager(nil)
}
