//go:build windows

package main

import (
	"context"
	"encoding/json"
	"github.com/lxn/walk"
	d "github.com/lxn/walk/declarative"
	"golang.org/x/sys/windows"
	"strconv"
	"strings"
	"time"
	"unsafe"
)

func dataObject(raw json.RawMessage) json.RawMessage {
	var outer map[string]json.RawMessage
	if json.Unmarshal(raw, &outer) == nil {
		if v := outer["data"]; len(v) > 0 {
			return v
		}
	}
	return raw
}
func (u *launcherUI) notices(ctx context.Context) (string, error) {
	var raw json.RawMessage
	if e := u.api.Request(ctx, "GET", "/launcher/v1/notices", "", nil, &raw); e != nil {
		return "", e
	}
	var data struct {
		Notices []struct {
			Title string `json:"title"`
			Date  string `json:"notice_date"`
		} `json:"notices"`
	}
	if e := json.Unmarshal(dataObject(raw), &data); e != nil {
		return "", e
	}
	var b strings.Builder
	for _, v := range data.Notices {
		if b.Len() > 100000 {
			break
		}
		b.WriteString(v.Date + "  " + v.Title + "\r\n\r\n")
	}
	if b.Len() == 0 {
		return "", nil
	}
	return b.String(), nil
}
func (u *launcherUI) refreshNotices() {
	u.job("正在读取公告…", func(ctx context.Context) (func(), error) {
		text, e := u.notices(ctx)
		if e != nil {
			return nil, e
		}
		return func() {
			u.notice.SetText(text)
			if u.view != nil {
				u.view.recompose()
			}
			u.message("公告已刷新")
		}, nil
	})
}
func (u *launcherUI) pollLoop() {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	tick := 0
	for {
		select {
		case <-u.ctx.Done():
			return
		case <-ticker.C:
			tick++
			n := tick
			u.sync(func() { u.poll(n) })
		}
	}
}
func (u *launcherUI) poll(tick int) {
	if u.busy || u.polling || u.offline || u.api == nil {
		return
	}
	u.polling = true
	active := []*slot{}
	for _, s := range u.slots {
		if s != nil && s.Game.Alive() {
			active = append(active, s)
		}
	}
	if len(active) == 0 {
		u.cursor = -1
		u.cursorToken = ""
	} else if u.cursorToken != active[0].Session.AccessToken {
		u.cursorToken = active[0].Session.AccessToken
		u.cursor = -1
	}
	cursor := u.cursor
	go func() {
		defer u.sync(func() { u.polling = false })
		if tick == 1 || tick%15 == 0 {
			if text, e := u.notices(u.ctx); e == nil {
				u.sync(func() {
					u.notice.SetText(text)
					if u.view != nil {
						u.view.recompose()
					}
				})
			}
		}
		if tick%5 == 0 {
			if enabled, e := u.api.MultiOpen(u.ctx); e == nil {
				u.sync(func() { u.setMulti(enabled) })
			}
		}
		if len(active) == 0 {
			return
		}
		s := active[0]
		var raw json.RawMessage
		if u.api.Request(u.ctx, "GET", "/launcher/v1/announcements?since="+strconv.FormatInt(cursor, 10), s.Session.AccessToken, nil, &raw) != nil {
			return
		}
		var data struct {
			Cursor       int64 `json:"cursor"`
			Announcement *struct {
				ID      int64  `json:"id"`
				Content string `json:"content"`
				Color   string `json:"color"`
			} `json:"announcement"`
		}
		if json.Unmarshal(dataObject(raw), &data) != nil {
			return
		}
		u.sync(func() {
			if u.currentPrimary() != s {
				return
			}
			u.cursor = data.Cursor
			if data.Announcement == nil {
				return
			}
			a := data.Announcement
			if len([]rune(a.Content)) < 1 || len([]rune(a.Content)) > 400 {
				return
			}
			color, e := strconv.ParseUint(strings.TrimPrefix(a.Color, "0x"), 16, 32)
			if e != nil || len(a.Color) != 10 || !strings.HasPrefix(a.Color, "0x") {
				return
			}
			for _, slot := range active {
				showOverlay(u, slot, a.Content, uint32(color), a.ID)
			}
		})
	}()
}
func (u *launcherUI) currentPrimary() *slot {
	for _, s := range u.slots {
		if s != nil && s.Game.Alive() {
			return s
		}
	}
	return nil
}

var overlayUser32 = windows.NewLazySystemDLL("user32.dll")

func showOverlay(u *launcherUI, s *slot, text string, color uint32, id int64) {
	target, _ := s.Game.Window()
	if target == 0 {
		return
	}
	var window *walk.MainWindow
	var label *walk.TextLabel
	e := d.MainWindow{AssignTo: &window, Title: "服务器公告", Size: d.Size{Width: 700, Height: 90}, Layout: d.VBox{Margins: d.Margins{Left: 12, Top: 10, Right: 12, Bottom: 10}}, Children: []d.Widget{d.TextLabel{AssignTo: &label, Text: text, Font: d.Font{Family: "Microsoft YaHei UI", PointSize: 15, Bold: true}}}}.Create()
	if e != nil {
		return
	}
	label.SetTextColor(walk.RGB(byte(color), byte(color>>8), byte(color>>16)))
	brush, e := walk.NewSolidColorBrush(walk.RGB(20, 22, 30))
	if e == nil {
		window.SetBackground(brush)
		window.Disposing().Attach(func() { brush.Dispose() })
	}
	hwnd := uintptr(window.Handle())
	style, _, _ := overlayUser32.NewProc("GetWindowLongW").Call(hwnd, uintptr(int32ToUint(-16)))
	overlayUser32.NewProc("SetWindowLongW").Call(hwnd, uintptr(int32ToUint(-16)), style&^0x00c40000|0x80000000)
	ex, _, _ := overlayUser32.NewProc("GetWindowLongW").Call(hwnd, uintptr(int32ToUint(-20)))
	overlayUser32.NewProc("SetWindowLongW").Call(hwnd, uintptr(int32ToUint(-20)), ex|0x20|0x80|0x08000000)
	go func() {
		ticker := time.NewTicker(200 * time.Millisecond)
		defer ticker.Stop()
		deadline := time.Now().Add(8 * time.Second)
		displayed := false
		for {
			select {
			case <-u.ctx.Done():
				u.sync(func() { window.Dispose() })
				return
			case <-ticker.C:
				u.sync(func() {
					if window.IsDisposed() {
						return
					}
					if time.Now().After(deadline) || !s.Game.Alive() {
						window.Dispose()
						return
					}
					foreground, _, _ := overlayUser32.NewProc("GetForegroundWindow").Call()
					iconic, _, _ := overlayUser32.NewProc("IsIconic").Call(uintptr(target))
					if foreground != uintptr(target) || iconic != 0 {
						window.Hide()
						return
					}
					var r struct{ Left, Top, Right, Bottom int32 }
					ok, _, _ := overlayUser32.NewProc("GetWindowRect").Call(uintptr(target), uintptr(unsafe.Pointer(&r)))
					if ok == 0 {
						return
					}
					width := int(r.Right-r.Left) - 50
					if width < 240 {
						width = 240
					}
					if width > 1000 {
						width = 1000
					}
					left := int(r.Left) + (int(r.Right-r.Left)-width)/2
					overlayUser32.NewProc("SetWindowPos").Call(hwnd, ^uintptr(0), uintptr(left), uintptr(int(r.Top)+65), uintptr(width), 90, 0x10|0x40)
					if !displayed {
						displayed = true
						go func() {
							_ = u.api.Request(u.ctx, "POST", "/launcher/v1/announcements/receipt", s.Session.AccessToken, map[string]int64{"id": id}, nil)
						}()
					}
				})
				if time.Now().After(deadline) {
					return
				}
			}
		}
	}()
}
func int32ToUint(i int32) uint32 { return uint32(i) }
