//go:build windows

package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"github.com/lxn/walk"
	d "github.com/lxn/walk/declarative"
	"github.com/lxn/win"
	"golang.org/x/sys/windows"
	"image"
	"newadmin/internal/player"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

func (u *launcherUI) create(exe string, configErr error) error {
	title := u.cfg.DisplayName
	if title == "" {
		title = "90US 登录器"
	}
	if u.offline {
		title += " · 离线预览"
	}
	v := &skinView{u: u, fonts: map[int]*walk.Font{}, start: time.Now(), statusAt: time.Now()}
	u.view = v
	u.prefs = skinPreferences{ExitGamePopup: true, TrayOnClose: true}
	u.loadPreferences()
	msg := ""
	if configErr != nil {
		msg = configErr.Error()
	}
	if u.offline {
		msg = "离线预览，不连接服务器或启动游戏"
	}
	children := []d.Widget{
		d.CustomWidget{AssignTo: &v.canvas, Paint: v.paint, PaintMode: d.PaintBuffered, Style: win.WS_CLIPSIBLINGS, OnMouseDown: v.mouseDown, OnMouseMove: v.mouseMove, OnKeyDown: v.keyDown},
		d.Label{AssignTo: &u.status, Text: msg, Visible: false}, d.Label{AssignTo: &u.server, Text: u.cfg.LoginURL, Visible: false}, d.Label{AssignTo: &u.multi, Visible: false}, d.TextEdit{AssignTo: &u.notice, Text: "", ReadOnly: true, Visible: false},
		d.CheckBox{AssignTo: &u.remember, Visible: false, OnCheckedChanged: func() {
			if u.remember != nil && !u.remember.Checked() && u.api != nil {
				if e := u.cfg.SaveLogin(nil); e != nil {
					u.message(e.Error())
				}
			}
			if v.canvas != nil {
				v.canvas.Invalidate()
			}
		}},
	}
	for i := 0; i < 2; i++ {
		index := i
		children = append(children, d.LineEdit{AssignTo: &u.accounts[i], MaxLength: 64, OnKeyDown: func(key walk.Key) {
			if key == walk.KeyReturn {
				u.login(index)
			}
		}}, d.LineEdit{AssignTo: &u.passwords[i], MaxLength: 128, PasswordMode: true, OnKeyDown: func(key walk.Key) {
			if key == walk.KeyReturn {
				u.login(index)
			}
		}}, d.PushButton{AssignTo: &u.loginButtons[i], Visible: false}, d.Label{AssignTo: &u.slotState[i], Visible: false})
	}
	children = append(children,
		d.LineEdit{AssignTo: &u.path, Text: filepath.Dir(executable()), Visible: false},
		d.ComboBox{AssignTo: &u.cdkAccount, Model: []string{}, Visible: false, OnCurrentIndexChanged: func() {
			if u.cdkCharacter != nil && !u.busy {
				u.loadCDK()
			}
		}},
		d.ComboBox{AssignTo: &u.cdkCharacter, Model: []string{}, Visible: false},
		d.LineEdit{AssignTo: &u.code, MaxLength: 100, Visible: false},
		d.TextEdit{AssignTo: &u.history, ReadOnly: true, VScroll: true, Visible: false},
	)
	if e := (d.MainWindow{AssignTo: &u.mw, Title: title, Size: d.Size{Width: 1280, Height: 728}, Layout: skinFixedLayout{view: v}, Font: d.Font{Family: "Microsoft YaHei UI", PointSize: 10}, Children: children}).Create(); e != nil {
		return e
	}
	style := win.GetWindowLong(u.mw.Handle(), win.GWL_STYLE)
	win.SetWindowLong(u.mw.Handle(), win.GWL_STYLE, (style&^int32(win.WS_OVERLAPPEDWINDOW))|int32(-2147483648))
	win.SetWindowPos(u.mw.Handle(), 0, 0, 0, 0, 0, win.SWP_NOMOVE|win.SWP_NOSIZE|win.SWP_NOZORDER|win.SWP_FRAMECHANGED)
	_ = u.mw.SetClientSize(walk.Size{Width: 1280, Height: 728})
	b := u.mw.BoundsPixels()
	_ = u.mw.SetBoundsPixels(walk.Rectangle{X: (int(win.GetSystemMetrics(win.SM_CXSCREEN)) - b.Width) / 2, Y: (int(win.GetSystemMetrics(win.SM_CYSCREEN)) - b.Height) / 2, Width: b.Width, Height: b.Height})
	gdi := windows.NewLazySystemDLL("gdi32.dll")
	region, _, _ := gdi.NewProc("CreateRoundRectRgn").Call(0, 0, uintptr(b.Width+1), uintptr(b.Height+1), 20, 20)
	if region != 0 {
		ok, _, _ := overlayUser32.NewProc("SetWindowRgn").Call(uintptr(u.mw.Handle()), region, 1)
		if ok == 0 {
			gdi.NewProc("DeleteObject").Call(region)
		}
	}
	_ = v.canvas.SetBounds(walk.Rectangle{Width: 1280, Height: 728})

	v.canvas.MouseWheel().Attach(func(x, y int, button walk.MouseButton) {
		if v.page == 0 {
			if int(button) > 0 {
				v.noticeScroll -= 2
			} else {
				v.noticeScroll += 2
			}
			if v.noticeScroll < 0 {
				v.noticeScroll = 0
			}
			v.canvas.Invalidate()
		}
	})
	for _, edit := range []*walk.LineEdit{u.accounts[0], u.accounts[1], u.passwords[0], u.passwords[1], u.code} {
		edit.SetTextColor(walk.RGB(255, 255, 255))
		edit.SetFont(v.font(10, false))
		ex := win.GetWindowLong(edit.Handle(), win.GWL_EXSTYLE)
		style := win.GetWindowLong(edit.Handle(), win.GWL_STYLE)
		win.SetWindowLong(edit.Handle(), win.GWL_EXSTYLE, ex&^int32(win.WS_EX_CLIENTEDGE))
		win.SetWindowLong(edit.Handle(), win.GWL_STYLE, style&^int32(win.WS_BORDER))
		win.SetWindowPos(edit.Handle(), 0, 0, 0, 0, 0, win.SWP_NOMOVE|win.SWP_NOSIZE|win.SWP_NOZORDER|win.SWP_FRAMECHANGED)
		win.SendMessage(edit.Handle(), win.EM_SETMARGINS, 3, 0)
	}
	historyBrush, _ := walk.NewSolidColorBrush(walk.RGB(20, 21, 25))
	u.history.SetBackground(historyBrush)
	u.history.SetTextColor(walk.RGB(255, 255, 255))
	u.history.SetFont(v.font(9, false))
	ex := win.GetWindowLong(u.history.Handle(), win.GWL_EXSTYLE)
	win.SetWindowLong(u.history.Handle(), win.GWL_EXSTYLE, ex&^int32(win.WS_EX_CLIENTEDGE))
	win.SetWindowPos(u.history.Handle(), 0, 0, 0, 0, 0, win.SWP_NOMOVE|win.SWP_NOSIZE|win.SWP_NOZORDER|win.SWP_FRAMECHANGED)
	u.history.TextChanged().Attach(func() {
		show := 0
		if len(strings.Split(u.history.Text(), "\n")) > 6 {
			show = 1
		}
		overlayUser32.NewProc("ShowScrollBar").Call(uintptr(u.history.Handle()), 1, uintptr(show))
	})
	overlayUser32.NewProc("ShowScrollBar").Call(uintptr(u.history.Handle()), 1, 0)
	u.history.Disposing().Attach(func() { historyBrush.Dispose() })

	if iconImage, e := readSkinImage(filepath.Dir(exe), "90US-launcher.png"); e == nil {
		v.icon, _ = walk.NewIconFromImage(iconImage)
		if v.icon != nil {
			u.mw.SetIcon(v.icon)
		}
	}
	u.mw.Closing().Attach(u.onClose)
	u.mw.Disposing().Attach(func() {
		u.closed.Store(true)
		u.stop()
		if u.tray != nil {
			u.tray.Dispose()
		}
		if v.frame != nil {
			v.frame.Dispose()
		}
		for _, f := range v.fonts {
			f.Dispose()
		}
		for _, brush := range v.editBrushes {
			brush.Dispose()
		}
		for _, bmp := range v.editBitmaps {
			bmp.Dispose()
		}
		if v.icon != nil {
			v.icon.Dispose()
		}
	})
	v.layout()
	u.refresh()
	go v.backgroundLoop()
	return nil
}

func (v *skinView) layout() {
	main := v.page == 0
	for i := 0; i < 2; i++ {
		show := main && (i == 0 || v.dual)
		v.u.accounts[i].SetVisible(show)
		v.u.passwords[i].SetVisible(show)
	}
	v.u.code.SetVisible(v.page == 1 && v.popup == "")
	v.u.history.SetVisible(v.page == 1 && v.popup == "")
	win.SetWindowPos(v.canvas.Handle(), win.HWND_BOTTOM, 0, 0, 0, 0, win.SWP_NOMOVE|win.SWP_NOSIZE|win.SWP_NOACTIVATE)
	v.recompose()
	v.canvas.Invalidate()
	v.u.mw.RequestLayout()
}
func (v *skinView) hit(x, y int) string {
	p := image.Pt(x, y)
	if p.In(image.Rect(1196, 12, 1232, 48)) {
		return "minimize"
	}
	if p.In(image.Rect(1232, 12, 1268, 48)) {
		return "close"
	}
	for i, name := range []string{"game", "cdk", "settings"} {
		if p.In(navBounds[i]) {
			return name
		}
	}
	if v.page == 0 {
		for name, r := range v.actionBounds() {
			if name == "login2" && !v.dual {
				continue
			}
			if p.In(r) {
				return name
			}
		}
	}
	if v.page == 1 {
		for name, r := range map[string]image.Rectangle{"account": image.Rect(360, 248, 920, 280), "character": image.Rect(360, 312, 920, 344), "redeem": image.Rect(360, 422, 920, 458), "refreshCDK": image.Rect(360, 628, 920, 660)} {
			if p.In(r) {
				return name
			}
		}
	}
	if v.page == 2 {
		if p.In(image.Rect(50, 76, 118, 102)) {
			return "base"
		}
		if p.In(image.Rect(162, 76, 204, 102)) {
			return "about"
		}
		if v.settingsTab == 0 {
			for name, r := range map[string]image.Rectangle{"popupGame": image.Rect(1166, 173, 1222, 198), "tray": image.Rect(58, 264, 400, 286), "exit": image.Rect(58, 291, 400, 313), "clearAll": image.Rect(1120, 672, 1240, 708)} {
				if p.In(r) {
					return name
				}
			}
		} else {
			if p.In(image.Rect(569, 469, 800, 489)) {
				return "discord"
			}
			if p.In(image.Rect(500, 680, 780, 708)) {
				return "licenses"
			}
		}
	}
	return ""
}
func (v *skinView) keyDown(key walk.Key) {
	if v.popup == "" {
		return
	}
	n := len(v.popupOptions())
	if n == 0 {
		return
	}
	switch key {
	case walk.KeyEscape:
		v.popup = ""
		v.layout()
		return
	case walk.KeyDown:
		if v.popupIndex < n-1 {
			v.popupIndex++
		}
	case walk.KeyUp:
		if v.popupIndex > 0 {
			v.popupIndex--
		}
	case walk.KeyReturn:
		v.selectPopup(v.popupIndex)
		return
	}
	if v.popupIndex < v.popupScroll {
		v.popupScroll = v.popupIndex
	}
	if v.popupIndex >= v.popupScroll+8 {
		v.popupScroll = v.popupIndex - 7
	}
	v.canvas.Invalidate()
}
func (v *skinView) selectPopup(index int) {
	name := v.popup
	v.popup = ""
	v.layout()
	if name == "account" {
		v.u.cdkAccount.SetCurrentIndex(index)
	} else {
		v.u.cdkCharacter.SetCurrentIndex(index)
	}
	v.canvas.Invalidate()
}
func (v *skinView) mouseMove(x, y int, _ walk.MouseButton) {
	hit := v.hit(x, y)
	if hit != v.hover {
		v.hover = hit
		v.canvas.Invalidate()
	}
}
func (v *skinView) mouseDown(x, y int, button walk.MouseButton) {
	if button != walk.LeftButton {
		return
	}
	u := v.u
	if v.popup != "" {
		r := v.popupRect()
		if image.Pt(x, y).In(r) {
			v.selectPopup(v.popupScroll + (y-r.Min.Y)/30)
		} else {
			v.popup = ""
			v.layout()
		}
		return
	}
	hit := v.hit(x, y)
	switch hit {
	case "minimize":
		u.minimize()
	case "close":
		u.mw.Close()
	case "game":
		v.popup = ""
		v.page = 0
		v.layout()
	case "cdk":
		v.page = 1
		v.layout()
		u.refreshAccounts()
	case "settings":
		v.page = 2
		v.layout()
	case "base":
		v.settingsTab = 0
		v.layout()
	case "about":
		v.settingsTab = 1
		v.layout()
	case "account", "character":
		if !u.busy && u.selectedSlot() != nil {
			v.popup = hit
			v.popupIndex = 0
			v.popupScroll = 0
			if len(v.popupOptions()) == 0 {
				v.popup = ""
			}
			v.layout()
			v.canvas.SetFocus()
		}
	case "redeem":
		if !u.busy {
			u.redeem()
		}
	case "refreshCDK":
		if !u.busy {
			u.loadCDK()
		}
	case "popupGame":
		u.prefs.ExitGamePopup = !u.prefs.ExitGamePopup
		u.savePreferences()
		v.canvas.Invalidate()
	case "tray", "exit":
		u.prefs.TrayOnClose = hit == "tray"
		u.prefs.CloseActionAsked = true
		u.savePreferences()
		v.canvas.Invalidate()
	case "clearAll":
		u.clearAllPreferences()
	case "discord":
		_ = win.ShellExecute(u.mw.Handle(), nil, windowsString("https://discord.gg/kxppgMyT7"), nil, nil, win.SW_SHOWNORMAL)
	case "licenses":
		walk.MsgBox(u.mw, "第三方许可", player.ThirdPartyNotices, walk.MsgBoxOK)
	case "login":
		if u.loginButtons[0].Enabled() {
			u.login(0)
		}
	case "login2":
		if u.loginButtons[1].Enabled() {
			u.login(1)
		}
	case "register":
		if !u.busy {
			u.accountDialog(false)
		}
	case "recovery":
		if !u.busy {
			u.accountDialog(true)
		}
	case "remember":
		if !u.busy {
			u.remember.SetChecked(!u.remember.Checked())
		}
	case "dual":
		if !u.busy {
			if !v.dual && !v.multiAllowed && u.api != nil && !u.offline {
				u.message("服务器当前关闭双开")
				return
			}
			v.dual = !v.dual
			v.layout()
		}
	case "clear":
		if !u.busy {
			for _, p := range u.passwords {
				p.SetText("")
			}
			if u.api != nil {
				if e := u.cfg.SaveLogin(nil); e != nil {
					u.message(e.Error())
					return
				}
			}
			u.remember.SetChecked(false)
			u.message("已清除主、副账号密码和保存的凭据")
		}
	default:
		if y < 95 {
			win.ReleaseCapture()
			win.SendMessage(u.mw.Handle(), win.WM_NCLBUTTONDOWN, win.HTCAPTION, 0)
		}
	}
}
func themeSkinContainer(container walk.Container) {
	brush, e := walk.NewSolidColorBrush(walk.RGB(34, 35, 41))
	if e != nil {
		return
	}
	container.SetBackground(brush)
	container.Disposing().Attach(func() { brush.Dispose() })
	for i := 0; i < container.Children().Len(); i++ {
		w := container.Children().At(i)
		if c, ok := w.(walk.Container); ok {
			themeSkinContainer(c)
			continue
		}
		switch w.(type) {
		case *walk.PushButton, *walk.ComboBox:
			continue
		}
		if text, ok := w.(interface{ SetTextColor(walk.Color) }); ok {
			text.SetTextColor(walk.RGB(230, 230, 236))
		}
		if _, ok := w.(*walk.LineEdit); ok {
			inputBrush, e := walk.NewSolidColorBrush(walk.RGB(20, 21, 25))
			if e == nil {
				w.SetBackground(inputBrush)
				w.Disposing().Attach(func() { inputBrush.Dispose() })
			}
		} else {
			w.SetBackground(brush)
		}
	}
}

type skinPreferences struct {
	ExitGamePopup    bool `json:"exit_game_popup"`
	TrayOnClose      bool `json:"tray_on_close"`
	CloseActionAsked bool `json:"close_action_asked"`
}

func (u *launcherUI) loadPreferences() {
	if u.api == nil {
		return
	}
	path := filepath.Join(u.cfg.DataDirectory(), "preferences.json")
	if player.NoLinks(path) != nil {
		return
	}
	raw, e := os.ReadFile(path)
	if e == nil && len(raw) < 4096 {
		_ = json.Unmarshal(raw, &u.prefs)
		return
	}
	// Import only UI preferences from the original template, never credentials.
	identity := fmt.Sprintf("%x", sha256.Sum256([]byte(strings.ToLower(u.cfg.ServerID))))
	for _, base := range []string{`E:\codex\登录器缓存\gm-native-mail\profiles`, filepath.Join(filepath.Dir(executable()), "data", "profiles"), filepath.Join(filepath.Dir(executable()), "data")} {
		oldPath := filepath.Join(base, identity, "config.json")
		if player.NoLinks(oldPath) != nil {
			continue
		}
		info, e := os.Stat(oldPath)
		if e != nil || !info.Mode().IsRegular() || info.Size() > 4<<20 {
			continue
		}
		b, e := os.ReadFile(oldPath)
		if e != nil {
			continue
		}
		var old struct {
			CloseAction      string
			CloseActionAsked int
			ExitGamePopup    *bool
		}
		if json.Unmarshal(b, &old) != nil {
			continue
		}
		if old.CloseAction != "Exit" && old.CloseAction != "MinimizeToTray" {
			continue
		}
		u.prefs.TrayOnClose = old.CloseAction == "MinimizeToTray"
		u.prefs.CloseActionAsked = old.CloseActionAsked == 1
		if old.ExitGamePopup != nil {
			u.prefs.ExitGamePopup = *old.ExitGamePopup
		}
		break
	}
}
func (u *launcherUI) savePreferences() {
	if u.api == nil {
		return
	}
	dir := u.cfg.DataDirectory()
	if player.NoLinks(dir) != nil {
		return
	}
	if os.MkdirAll(dir, 0700) != nil {
		return
	}
	raw, _ := json.Marshal(u.prefs)
	temp := filepath.Join(dir, ".preferences-"+player.ID())
	if player.Durable(temp, raw) == nil {
		_ = player.AtomicReplace(temp, filepath.Join(dir, "preferences.json"))
	}
}

func (u *launcherUI) clearAllPreferences() {
	if u.busy || u.api == nil || u.offline {
		return
	}
	if walk.MsgBox(u.mw, "清除所有配置", "清除本机保存的账号、密码和界面设置？", walk.MsgBoxYesNo|walk.MsgBoxIconQuestion) != walk.DlgCmdYes {
		return
	}
	if e := u.cfg.SaveLogin(nil); e != nil {
		u.message(e.Error())
		return
	}
	for _, name := range []string{"client-location.json", "preferences.json"} {
		path := filepath.Join(u.cfg.DataDirectory(), name)
		if player.NoLinks(path) == nil {
			_ = os.Remove(path)
		}
	}
	for i := range u.accounts {
		u.accounts[i].SetText("")
		u.passwords[i].SetText("")
	}
	u.remember.SetChecked(false)
	u.prefs = skinPreferences{ExitGamePopup: true, TrayOnClose: true}
	u.path.SetText(filepath.Dir(executable()))
	u.savePreferences()
	u.view.layout()
	u.message("本机账号、密码和设置已清除。")
}
func windowsString(s string) *uint16     { p, _ := sysUTF16(s); return p }
func sysUTF16(s string) (*uint16, error) { return syscall.UTF16PtrFromString(s) }
func (u *launcherUI) onClose(cancel *bool, _ walk.CloseReason) {
	if u.busy {
		if !u.forceExit {
			if walk.MsgBox(u.mw, "操作尚未结束", "取消当前操作并在完成回退后退出登录器？", walk.MsgBoxYesNo|walk.MsgBoxIconQuestion) != walk.DlgCmdYes {
				*cancel = true
				return
			}
			u.forceExit = true
		}
		u.closing = true
		if u.cancel != nil {
			u.cancel()
		}
		*cancel = true
		return
	}
	running := false
	if !u.forceExit && u.api != nil && !u.offline && !u.prefs.CloseActionAsked {
		var dialog *walk.Dialog
		var tray, remember *walk.CheckBox
		accepted := false
		err := (d.Dialog{AssignTo: &dialog, Title: "关闭窗口", FixedSize: true, Size: d.Size{Width: 360, Height: 200}, Layout: d.VBox{Margins: d.Margins{Left: 24, Top: 20, Right: 24, Bottom: 20}, Spacing: 12}, Children: []d.Widget{d.Label{Text: "请选择关闭窗口后的操作"}, d.CheckBox{AssignTo: &tray, Text: "最小化到系统托盘", Checked: u.prefs.TrayOnClose}, d.CheckBox{AssignTo: &remember, Text: "记住我的选择"}, d.PushButton{Text: "确定", OnClicked: func() { accepted = true; dialog.Accept() }}}}).Create(u.mw)
		if err != nil {
			*cancel = true
			return
		}
		themeSkinContainer(dialog)
		dialog.Run()
		if !accepted {
			dialog.Dispose()
			*cancel = true
			return
		}
		u.prefs.TrayOnClose = tray.Checked()
		if remember.Checked() {
			u.prefs.CloseActionAsked = true
			u.savePreferences()
		}
		dialog.Dispose()
	}
	for _, s := range u.slots {
		if s != nil && s.Game.Alive() {
			running = true
		}
	}
	if !u.forceExit && u.api != nil && u.prefs.TrayOnClose {
		*cancel = true
		u.hideToTray()
		return
	}
	if running {
		if walk.MsgBox(u.mw, "退出登录器", "退出将结束本登录器启动的游戏，是否继续？", walk.MsgBoxYesNo|walk.MsgBoxIconQuestion) != walk.DlgCmdYes {
			*cancel = true
			u.forceExit = false
			return
		}
		for _, s := range u.slots {
			if s != nil {
				_ = s.Game.Stop()
			}
		}
	}
	u.closed.Store(true)
	u.stop()
}
func (u *launcherUI) hideToTray() {
	if u.tray == nil {
		tray, e := walk.NewNotifyIcon(u.mw)
		if e != nil {
			u.minimize()
			return
		}
		u.tray = tray
		if u.view.icon != nil {
			_ = tray.SetIcon(u.view.icon)
		}
		_ = tray.SetToolTip(u.mw.Title())
		tray.MouseDown().Attach(func(_, _ int, button walk.MouseButton) {
			if button == walk.LeftButton {
				u.restoreWindow()
			}
		})
		open := walk.NewAction()
		open.SetText("打开主界面")
		open.Triggered().Attach(u.restoreWindow)
		tray.ContextMenu().Actions().Add(open)
		exit := walk.NewAction()
		exit.SetText("退出登录器")
		exit.Triggered().Attach(func() { u.forceExit = true; u.mw.Close() })
		tray.ContextMenu().Actions().Add(exit)
	}
	_ = u.tray.SetVisible(true)
	u.mw.Hide()
}
func (u *launcherUI) restoreWindow() {
	if u.tray != nil {
		_ = u.tray.SetVisible(false)
	}
	u.mw.Show()
	win.ShowWindow(u.mw.Handle(), win.SW_RESTORE)
	win.SetForegroundWindow(u.mw.Handle())
}
func (u *launcherUI) waitLogout(s *slot) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = u.api.Logout(ctx, s.Session)
}
