//go:build windows

package main

import (
	"bytes"
	"embed"
	"fmt"
	"github.com/lxn/walk"
	"github.com/lxn/win"
	_ "golang.org/x/image/bmp"
	xdraw "golang.org/x/image/draw"
	"image"
	"image/color"
	"image/draw"
	_ "image/jpeg"
	_ "image/png"
	"math"
	"newadmin/internal/player"
	"os"
	"path/filepath"
	"strings"
	"time"
)

//go:embed assets/original/*
var originalSkin embed.FS

type skinView struct {
	u                       *launcherUI
	canvas                  *walk.CustomWidget
	frame                   *walk.Bitmap
	base                    *image.RGBA
	page, settingsTab       int
	dual, multiAllowed      bool
	hover                   string
	noticeScroll            int
	dialogStatus            *walk.Label
	fonts                   map[int]*walk.Font
	start                   time.Time
	statusAt                time.Time
	icon                    *walk.Icon
	popup                   string
	popupIndex, popupScroll int
	editBrushes             map[*walk.LineEdit]*tintedBitmapBrush
	editBitmaps             map[*walk.LineEdit]*walk.Bitmap
	editColors              map[win.HWND]walk.Color
}

func (v *skinView) noticeCard() image.Rectangle {
	if v.dual {
		return image.Rect(44, 367, 344, 657)
	}
	return image.Rect(38, 367, 558, 657)
}
func readSkinImage(root, name string) (image.Image, error) {
	path := filepath.Join(root, "Image", name)
	var raw []byte
	if filepath.Base(name) == name && player.NoLinks(path) == nil {
		if info, e := os.Stat(path); e == nil && info.Mode().IsRegular() && info.Size() <= 64<<20 {
			raw, _ = os.ReadFile(path)
		}
	}
	if len(raw) == 0 {
		raw, _ = originalSkin.ReadFile("assets/original/" + name)
	}
	cfg, _, e := image.DecodeConfig(bytes.NewReader(raw))
	if e != nil {
		return nil, e
	}
	if cfg.Width < 1 || cfg.Height < 1 || int64(cfg.Width)*int64(cfg.Height) > 40_000_000 {
		return nil, os.ErrInvalid
	}
	im, _, e := image.Decode(bytes.NewReader(raw))
	return im, e
}
func scaledImage(im image.Image, w, h int) *image.RGBA {
	out := image.NewRGBA(image.Rect(0, 0, w, h))
	xdraw.ApproxBiLinear.Scale(out, out.Bounds(), im, im.Bounds(), draw.Src, nil)
	return out
}
func blendImage(a, b *image.RGBA, f int) *image.RGBA {
	out := image.NewRGBA(a.Bounds())
	for i := 0; i < len(out.Pix); i++ {
		out.Pix[i] = byte((int(a.Pix[i])*(255-f) + int(b.Pix[i])*f) / 255)
	}
	return out
}
func roundContains(r image.Rectangle, x, y, radius int) bool {
	cx := x
	cy := y
	if x < r.Min.X+radius {
		cx = r.Min.X + radius
	}
	if x >= r.Max.X-radius {
		cx = r.Max.X - radius - 1
	}
	if y < r.Min.Y+radius {
		cy = r.Min.Y + radius
	}
	if y >= r.Max.Y-radius {
		cy = r.Max.Y - radius - 1
	}
	dx, dy := x-cx, y-cy
	return dx*dx+dy*dy <= radius*radius
}
func tintRound(out *image.RGBA, r image.Rectangle, radius int, c color.NRGBA) {
	r = r.Intersect(out.Bounds())
	for y := r.Min.Y; y < r.Max.Y; y++ {
		for x := r.Min.X; x < r.Max.X; x++ {
			if !roundContains(r, x, y, radius) {
				continue
			}
			i := out.PixOffset(x, y)
			a := int(c.A)
			out.Pix[i] = byte((int(out.Pix[i])*(255-a) + int(c.R)*a) / 255)
			out.Pix[i+1] = byte((int(out.Pix[i+1])*(255-a) + int(c.G)*a) / 255)
			out.Pix[i+2] = byte((int(out.Pix[i+2])*(255-a) + int(c.B)*a) / 255)
			out.Pix[i+3] = 255
		}
	}
}
func (v *skinView) recompose() {
	if v.base == nil {
		return
	}
	out := image.NewRGBA(v.base.Bounds())
	copy(out.Pix, v.base.Pix)
	if v.page != 0 {
		tintRound(out, image.Rect(0, 68, 1280, 728), 0, color.NRGBA{16, 16, 20, 200})
	} else {
		tintRound(out, v.mainCard(), 16, color.NRGBA{16, 16, 21, 176})
		if strings.TrimSpace(v.u.notice.Text()) != "" {
			tintRound(out, v.noticeCard(), 16, color.NRGBA{16, 16, 21, 176})
		}
		if v.dual {
			tintRound(out, v.secondaryCard(), 16, color.NRGBA{16, 16, 21, 176})
		}
	}
	tintRound(out, image.Rect(458, 12, 822, 66), 22, color.NRGBA{20, 20, 25, 204})
	if v.page >= 0 && v.page < len(navBounds) {
		tintRound(out, navBounds[v.page], 8, color.NRGBA{255, 255, 255, 36})
	}
	if v.page == 0 {
		for i := 0; i < 2; i++ {
			if i == 1 && !v.dual {
				continue
			}
			for _, password := range []bool{false, true} {
				r := v.editBounds(i, password)
				r = image.Rect(r.Min.X-8, r.Min.Y-7, r.Max.X+8, r.Min.Y+25)
				tintRound(out, r, 0, color.NRGBA{255, 255, 255, 31})
				for _, edge := range []image.Rectangle{image.Rect(r.Min.X, r.Min.Y, r.Max.X, r.Min.Y+1), image.Rect(r.Min.X, r.Max.Y-1, r.Max.X, r.Max.Y), image.Rect(r.Min.X, r.Min.Y+1, r.Min.X+1, r.Max.Y-1), image.Rect(r.Max.X-1, r.Min.Y+1, r.Max.X, r.Max.Y-1)} {
					tintRound(out, edge, 0, color.NRGBA{255, 255, 255, 23})
				}
			}
		}
	}
	if v.page == 1 {
		tintRound(out, cdkCodeBounds, 0, color.NRGBA{255, 255, 255, 51})
		tintRound(out, cdkCodeBounds.Inset(1), 0, color.NRGBA{16, 16, 21, 176})
	}
	if logo, e := readSkinImage(filepath.Dir(executable()), "logo.png"); e == nil {
		w := int(math.Round(float64(logo.Bounds().Dx()) * 56 / float64(logo.Bounds().Dy())))
		xdraw.ApproxBiLinear.Scale(out, image.Rect(112, 20, 112+w, 76), logo, logo.Bounds(), draw.Over, nil)
	}
	bitmap, e := walk.NewBitmapFromImage(out)
	if e != nil {
		return
	}
	old := v.frame
	v.frame = bitmap
	if old != nil {
		old.Dispose()
	}
	v.updateEditBackgrounds(out)
	v.canvas.Invalidate()
}
func executable() string { p, _ := os.Executable(); return p }
func (v *skinView) backgroundLoop() {
	root := filepath.Dir(executable())
	names := []string{"default-bg-img.png", "RotatingBackground1.jpg", "RotatingBackground2.jpg", "RotatingBackground3.png", "RotatingBackground4.jpg", "RotatingBackground5.png", "RotatingBackground6.png"}
	cache := map[string]*image.RGBA{}
	get := func(name string) *image.RGBA {
		if im := cache[name]; im != nil {
			return im
		}
		im, e := readSkinImage(root, name)
		if e != nil {
			return nil
		}
		scaled := scaledImage(im, 1280, 728)
		cache[name] = scaled
		return scaled
	}
	current := get(names[0])
	if current == nil {
		current = image.NewRGBA(image.Rect(0, 0, 1280, 728))
		draw.Draw(current, current.Bounds(), image.NewUniform(color.RGBA{23, 25, 32, 255}), image.Point{}, draw.Src)
	}
	initial := current
	v.u.sync(func() { v.base = initial; v.recompose() })
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	index := 0
	nextAt := time.Now().Add(5 * time.Second)
	for {
		select {
		case <-v.u.ctx.Done():
			return
		case <-ticker.C:
			if time.Now().Before(nextAt) {
				v.u.sync(func() { v.canvas.Invalidate() })
				continue
			}
			index = (index + 1) % len(names)
			next := get(names[index])
			if next == nil {
				nextAt = time.Now().Add(5 * time.Second)
				continue
			}
			for step := 1; step <= 12; step++ {
				frame := blendImage(current, next, step*255/12)
				v.u.sync(func() { v.base = frame; v.recompose() })
				select {
				case <-v.u.ctx.Done():
					return
				case <-ticker.C:
				}
			}
			current = next
			nextAt = time.Now().Add(5 * time.Second)
			v.u.sync(func() { v.start = time.Now() })
		}
	}
}
func (v *skinView) font(size int, bold bool) *walk.Font {
	key := size
	if bold {
		key += 100
	}
	if f := v.fonts[key]; f != nil {
		return f
	}
	style := walk.FontStyle(0)
	if bold {
		style = walk.FontBold
	}
	f, e := walk.NewFont("Microsoft YaHei UI", size, style)
	if e != nil {
		return v.u.mw.Font()
	}
	v.fonts[key] = f
	return f
}
func (v *skinView) text(c *walk.Canvas, text string, r image.Rectangle, size int, bold bool, col walk.Color, center bool) {
	flags := walk.TextWordbreak | walk.TextNoPrefix
	if center {
		flags |= walk.TextCenter | walk.TextVCenter | walk.TextSingleLine
	}
	_ = c.DrawText(text, v.font(size, bold), col, walk.Rectangle{X: r.Min.X, Y: r.Min.Y, Width: r.Dx(), Height: r.Dy()}, flags)
}
func fillSkin(c *walk.Canvas, r image.Rectangle, col walk.Color, radius int) {
	brush, e := walk.NewSolidColorBrush(col)
	if e != nil {
		return
	}
	defer brush.Dispose()
	_ = c.FillRoundedRectangle(brush, walk.Rectangle{X: r.Min.X, Y: r.Min.Y, Width: r.Dx(), Height: r.Dy()}, walk.Size{Width: radius * 2, Height: radius * 2})
}

func (v *skinView) button(c *walk.Canvas, name, caption string, r image.Rectangle, kind int, enabled bool) {
	fg := walk.RGB(255, 255, 255)
	col := walk.RGB(232, 74, 58)
	if kind == 1 {
		col = walk.RGB(242, 243, 245)
		fg = walk.RGB(51, 51, 51)
	}
	if kind == 2 {
		col = walk.RGB(51, 52, 60)
	}
	if !enabled {
		col = walk.RGB(158, 158, 158)
		if name == "redeem" {
			col = walk.RGB(85, 86, 95)
		}
		if kind == 2 {
			col = walk.RGB(51, 52, 60)
		}
	}
	if enabled && v.hover == name {
		if kind == 1 {
			col = walk.RGB(225, 227, 230)
		} else if kind == 2 {
			col = walk.RGB(65, 66, 74)
		} else {
			col = walk.RGB(239, 123, 111)
		}
	}
	if enabled && kind == 0 && v.hover != name {
		brush, e := walk.NewHorizontalGradientBrush([]walk.GradientStop{{Offset: 0, Color: walk.RGB(232, 74, 58)}, {Offset: 1, Color: walk.RGB(198, 40, 40)}})
		if e == nil {
			_ = c.FillRoundedRectangle(brush, toWalk(r), walk.Size{Width: 20, Height: 20})
			brush.Dispose()
		} else {
			fillSkin(c, r, col, 10)
		}
	} else {
		fillSkin(c, r, col, 10)
	}
	v.text(c, caption, r, 10, kind != 2, fg, true)
}
func (v *skinView) paint(c *walk.Canvas, _ walk.Rectangle) error {
	if v.frame != nil {
		_ = c.DrawImageStretched(v.frame, walk.Rectangle{Width: 1280, Height: 728})
	} else {
		fillSkin(c, image.Rect(0, 0, 1280, 728), walk.RGB(23, 25, 32), 0)
	}
	if v.u.remember == nil || v.u.status == nil || v.u.loginButtons[0] == nil || v.u.code == nil {
		return nil
	}
	white, muted, red := walk.RGB(255, 255, 255), walk.RGB(204, 204, 210), walk.RGB(232, 74, 58)
	for i, caption := range []string{"开始游戏", "CDK兑换", "设置"} {
		r := navBounds[i]
		if v.page == i {
			fillSkin(c, image.Rect(r.Min.X+22, 57, r.Max.X-22, 60), red, 1)
		}
		v.text(c, caption, r, 11, false, white, true)
	}
	v.text(c, "—", image.Rect(1196, 12, 1232, 48), 15, false, white, true)
	v.text(c, "×", image.Rect(1232, 12, 1268, 48), 18, false, white, true)
	switch v.page {
	case 0:
		y := v.mainCard().Min.Y
		a := v.actionBounds()
		v.text(c, "登录游戏", image.Rect(1000, y+27, 1084, y+55), 15, true, white, false)
		v.text(c, "90US", image.Rect(1090, y+31, 1150, y+55), 12, true, white, false)
		v.text(c, "账号", image.Rect(918, y+77, 1214, y+95), 9, false, muted, false)
		v.text(c, "密码", image.Rect(918, y+139, 1214, y+157), 9, false, muted, false)
		if s := v.u.slots[0]; s != nil {
			v.text(c, "已登录："+s.Account, image.Rect(918, y+200, 1214, y+218), 9, false, walk.RGB(46, 204, 113), false)
		}
		v.checkbox(c, "remember", "记住密码", a["remember"], v.u.remember.Checked())
		v.checkbox(c, "dual", "双开", a["dual"], v.dual)
		v.text(c, "清除", a["clear"], 9, false, red, true)
		caption := "登 录"
		if v.u.slots[0] != nil {
			caption = "游戏运行中"
		}
		v.button(c, "login", caption, a["login"], 0, v.u.loginButtons[0].Enabled())
		v.button(c, "register", "注 册", a["register"], 1, !v.u.busy)
		v.text(c, "找回密码", a["recovery"], 9, false, walk.RGB(143, 199, 255), false)
		if strings.TrimSpace(v.u.notice.Text()) != "" {
			r := v.noticeCard()
			fillSkin(c, image.Rect(r.Min.X+22, r.Min.Y+20, r.Min.X+26, r.Min.Y+38), red, 2)
			v.text(c, "服务器公告", image.Rect(r.Min.X+34, r.Min.Y+17, r.Max.X-20, r.Min.Y+44), 13, true, white, false)
			lines := strings.Split(v.u.notice.Text(), "\r\n")
			if v.noticeScroll > len(lines) {
				v.noticeScroll = 0
			}
			v.text(c, strings.Join(lines[v.noticeScroll:], "\n"), image.Rect(r.Min.X+22, r.Min.Y+62, r.Max.X-22, r.Max.Y-18), 10, false, muted, false)
		}
		if v.dual {
			y := secondaryTop(v.u.slots[1] != nil)
			v.text(c, "双开登录", image.Rect(582, y, 854, y+28), 14, true, white, false)
			v.text(c, "副账号", image.Rect(582, y+40, 854, y+58), 9, false, muted, false)
			v.text(c, "密码", image.Rect(582, y+98, 854, y+116), 9, false, muted, false)
			caption := "副账号登 录"
			if s := v.u.slots[1]; s != nil {
				v.text(c, "已登录："+s.Account, image.Rect(582, y+158, 854, y+176), 9, false, walk.RGB(46, 204, 113), false)
				caption = "游戏运行中"
			}
			v.button(c, "login2", caption, a["login2"], 0, v.u.loginButtons[1].Enabled())
		}
	case 1:
		v.text(c, "CDK 兑换", image.Rect(360, 160, 920, 192), 18, false, white, false)
		v.text(c, "选择角色后兑换，物品在游戏内邮箱领取，点券直接到账。", image.Rect(360, 200, 920, 220), 9, false, muted, false)
		v.text(c, "当前登录账号", image.Rect(360, 228, 920, 246), 9, false, white, false)
		v.text(c, "领取角色", image.Rect(360, 292, 920, 310), 9, false, white, false)
		v.text(c, "兑换码", image.Rect(360, 356, 920, 374), 9, false, white, false)
		v.combo(c, "account", image.Rect(360, 248, 920, 280), v.u.cdkAccount)
		v.combo(c, "character", image.Rect(360, 312, 920, 344), v.u.cdkCharacter)
		v.button(c, "redeem", "兑换 CDK", image.Rect(360, 422, 920, 458), 0, !v.u.busy && v.u.selectedSlot() != nil && len(v.u.characters) > 0)
		status := v.u.status.Text()
		if status == "" || time.Since(v.statusAt) > 8*time.Second {
			status = "请选择角色并填写兑换码。"
		}
		v.text(c, status, image.Rect(360, 470, 920, 496), 9, false, white, false)
		v.button(c, "refreshCDK", "刷新角色和兑换记录", image.Rect(360, 628, 920, 660), 2, !v.u.busy && v.u.selectedSlot() != nil)
		v.paintPopup(c)
	case 2:
		for i, caption := range []string{"基础设置", "关于"} {
			r := image.Rect(50+i*112, 76, 118+i*112, 100)
			if i == 1 {
				r = image.Rect(162, 76, 204, 100)
			}
			col := white
			if v.settingsTab == i {
				col = red
				fillSkin(c, image.Rect(r.Min.X, 99, r.Max.X, 101), red, 0)
			}
			v.text(c, caption, r, 10, false, col, true)
		}
		if v.settingsTab == 0 {
			v.text(c, "基础设置", image.Rect(40, 134, 1240, 158), 12, true, white, false)
			fillSkin(c, image.Rect(40, 172, 1240, 198), walk.RGB(34, 35, 41), 13)
			v.text(c, "退出游戏时弹出", image.Rect(58, 178, 1120, 198), 10, false, white, false)
			col := walk.RGB(100, 100, 106)
			caption := "OFF"
			knob := 1168
			if v.u.prefs.ExitGamePopup {
				col = red
				caption = "ON"
				knob = 1198
			}
			fillSkin(c, image.Rect(1166, 173, 1222, 198), col, 13)
			v.text(c, caption, image.Rect(1170, 176, 1198, 195), 8, true, white, true)
			fillSkin(c, image.Rect(knob, 176, knob+20, 196), white, 10)
			fillSkin(c, image.Rect(40, 218, 1240, 326), walk.RGB(34, 35, 41), 16)
			v.text(c, "关闭窗口", image.Rect(58, 234, 300, 254), 10, false, white, false)
			v.radio(c, "最小化到系统托盘", 266, v.u.prefs.TrayOnClose)
			v.radio(c, "退出启动器", 293, !v.u.prefs.TrayOnClose)
			fillSkin(c, image.Rect(1120, 672, 1240, 708), red, 8)
			fillSkin(c, image.Rect(1121, 673, 1239, 707), walk.RGB(110, 26, 22), 7)
			v.text(c, "清除所有配置", image.Rect(1120, 672, 1240, 708), 9, false, white, true)
		} else {
			v.text(c, "YH登录器 Go版", image.Rect(40, 346, 1240, 373), 14, true, white, true)
			v.text(c, "本工具仅做学习与研究使用，不得用于包括但不限于非法盈利及任何非法用途。", image.Rect(40, 382, 1240, 402), 10, false, muted, true)
			v.text(c, "本工具所使用的素材、标志等著作权均归各著作权人（单位）所有。", image.Rect(40, 411, 1240, 431), 10, false, muted, true)
			v.text(c, "本工具请下载后于24小时内删除，如因违反相关法律法规出现任何后果自行承担，工具开发者不承担任何责任。使用本工具则默认您同意本规则，如不同意请立即删除本工具。", image.Rect(40, 440, 1240, 460), 9, false, muted, true)
			v.text(c, "DC群：", image.Rect(517, 469, 568, 489), 10, false, muted, false)
			v.text(c, "https://discord.gg/kxppgMyT7", image.Rect(569, 469, 800, 489), 10, false, walk.RGB(79, 168, 255), false)
			v.text(c, "版本 "+player.Version+"   ·   第三方许可", image.Rect(40, 680, 1240, 708), 9, false, muted, true)
		}
	}
	text := v.u.status.Text()
	if v.page != 1 && text != "" && (v.u.busy || time.Since(v.statusAt) < 8*time.Second) {
		r := image.Rect(944, 76, 1264, 172)
		fillSkin(c, r, walk.RGB(30, 32, 40), 10)
		v.text(c, text, image.Rect(960, 90, 1248, 158), 9, false, white, false)
	}
	progress := int(time.Since(v.start).Seconds() / 5 * 1280)
	if progress > 1280 {
		progress = 1280
	}
	if progress < 0 {
		progress = 0
	}
	fillSkin(c, image.Rect(0, 724, progress, 728), walk.RGB(54, 55, 58), 0)
	return nil
}
func (v *skinView) checkbox(c *walk.Canvas, _, caption string, r image.Rectangle, checked bool) {
	fillSkin(c, image.Rect(r.Min.X, r.Min.Y+3, r.Min.X+12, r.Min.Y+15), walk.RGB(255, 255, 255), 0)
	if checked {
		v.text(c, "✓", image.Rect(r.Min.X, r.Min.Y-1, r.Min.X+13, r.Min.Y+17), 10, true, walk.RGB(232, 74, 58), true)
	}
	v.text(c, caption, image.Rect(r.Min.X+17, r.Min.Y, r.Max.X, r.Max.Y), 9, false, walk.RGB(238, 238, 240), false)
}
func (v *skinView) radio(c *walk.Canvas, caption string, y int, selected bool) {
	col := walk.RGB(152, 152, 156)
	if selected {
		col = walk.RGB(232, 74, 58)
	}
	fillSkin(c, image.Rect(58, y, 74, y+16), col, 8)
	fillSkin(c, image.Rect(60, y+2, 72, y+14), walk.RGB(34, 35, 41), 6)
	if selected {
		fillSkin(c, image.Rect(62, y+4, 70, y+12), col, 4)
	}
	v.text(c, caption, image.Rect(84, y-1, 500, y+20), 10, false, walk.RGB(255, 255, 255), false)
}
func (v *skinView) combo(c *walk.Canvas, name string, r image.Rectangle, model *walk.ComboBox) {
	fillSkin(c, r, walk.RGB(25, 26, 30), 8)
	v.text(c, model.Text(), image.Rect(r.Min.X+12, r.Min.Y+5, r.Max.X-28, r.Max.Y), 10, false, walk.RGB(255, 255, 255), false)
	v.text(c, "⌄", image.Rect(r.Max.X-28, r.Min.Y, r.Max.X-5, r.Max.Y), 12, false, walk.RGB(200, 200, 205), true)
}
func (v *skinView) popupOptions() []string {
	if v.popup == "account" {
		names := []string{}
		for _, i := range v.u.cdkSlots {
			s := v.u.slots[i]
			if s != nil {
				label := "主账号"
				if i == 1 {
					label = "副账号"
				}
				names = append(names, s.Account+"（"+label+"）")
			}
		}
		return names
	}
	names := []string{}
	for _, ch := range v.u.characters {
		names = append(names, fmt.Sprintf("%s · Lv.%d", ch.Name, ch.Level))
	}
	return names
}
func (v *skinView) popupRect() image.Rectangle {
	y := 280
	if v.popup == "character" {
		y = 344
	}
	n := len(v.popupOptions())
	if n > 8 {
		n = 8
	}
	return image.Rect(360, y, 920, y+n*30)
}
func (v *skinView) paintPopup(c *walk.Canvas) {
	if v.popup == "" {
		return
	}
	r := v.popupRect()
	fillSkin(c, r, walk.RGB(25, 26, 30), 3)
	names := v.popupOptions()
	for j := 0; j < 8 && j+v.popupScroll < len(names); j++ {
		i := j + v.popupScroll
		row := image.Rect(r.Min.X+1, r.Min.Y+j*30, r.Max.X-1, r.Min.Y+(j+1)*30)
		if i == v.popupIndex {
			fillSkin(c, row, walk.RGB(65, 66, 74), 1)
		}
		v.text(c, names[i], row.Inset(5), 10, false, walk.RGB(255, 255, 255), false)
	}
}
func (v *skinView) updateEditBackgrounds(out *image.RGBA) {
	if v.editBrushes == nil {
		v.editBrushes = map[*walk.LineEdit]*tintedBitmapBrush{}
		v.editBitmaps = map[*walk.LineEdit]*walk.Bitmap{}
		v.editColors = map[win.HWND]walk.Color{}
	}
	updates := map[*walk.LineEdit]image.Rectangle{v.u.code: image.Rect(368, 383, 912, 402)}
	for i := 0; i < 2; i++ {
		updates[v.u.accounts[i]] = v.editBounds(i, false)
		updates[v.u.passwords[i]] = v.editBounds(i, true)
	}
	for edit, r := range updates {
		if edit == nil {
			continue
		}
		point := out.RGBAAt(r.Min.X+r.Dx()/2, r.Min.Y+r.Dy()/2)
		v.editColors[edit.Handle()] = walk.RGB(point.R, point.G, point.B)
		im := image.NewRGBA(image.Rect(0, 0, r.Dx(), r.Dy()))
		draw.Draw(im, im.Bounds(), out, r.Min, draw.Src)
		bmp, e := walk.NewBitmapFromImage(im)
		if e != nil {
			continue
		}
		brush, e := walk.NewBitmapBrush(bmp)
		if e != nil {
			bmp.Dispose()
			continue
		}
		oldBrush, oldBmp := v.editBrushes[edit], v.editBitmaps[edit]
		tinted := &tintedBitmapBrush{BitmapBrush: brush, color: v.editColors[edit.Handle()]}
		edit.SetBackground(tinted)
		v.editBrushes[edit], v.editBitmaps[edit] = tinted, bmp
		if oldBrush != nil {
			oldBrush.Dispose()
		}
		if oldBmp != nil {
			oldBmp.Dispose()
		}
	}
}
