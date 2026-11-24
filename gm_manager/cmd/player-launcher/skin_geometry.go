//go:build windows

package main

import (
	"github.com/lxn/walk"
	"github.com/lxn/win"
	"image"
)

// Design coordinates measured from the original WPF template at 96 DPI.
var navBounds = []image.Rectangle{image.Rect(468, 18, 588, 58), image.Rect(594, 18, 715, 58), image.Rect(721, 18, 812, 58)}
var cdkCodeBounds = image.Rect(360, 376, 920, 408)

func primaryCard(logged bool) image.Rectangle {
	h := 358
	if logged {
		h += 24
	}
	return image.Rect(892, 452-h/2, 1240, 452+h/2)
}
func secondaryTop(logged bool) int {
	if logged {
		return 340
	}
	return 352
}
func (v *skinView) mainCard() image.Rectangle { return primaryCard(v.u.slots[0] != nil) }
func (v *skinView) secondaryCard() image.Rectangle {
	r := v.mainCard()
	return image.Rect(558, r.Min.Y, 878, r.Max.Y)
}
func (v *skinView) actionBounds() map[string]image.Rectangle {
	y := v.mainCard().Min.Y
	extra := 0
	if v.u.slots[0] != nil {
		extra = 24
	}
	row := y + 200 + extra
	sec := secondaryTop(v.u.slots[1] != nil) + 164
	if v.u.slots[1] != nil {
		sec += 24
	}
	return map[string]image.Rectangle{
		"login":    image.Rect(918, y+231+extra, 1214, y+267+extra),
		"register": image.Rect(918, y+273+extra, 1214, y+309+extra),
		"recovery": image.Rect(1158, y+315+extra, 1214, y+333+extra),
		"remember": image.Rect(1047, row, 1115, row+18), "dual": image.Rect(1129, row, 1171, row+18), "clear": image.Rect(1181, row, 1214, row+18),
		"login2": image.Rect(582, sec, 854, sec+36),
	}
}
func (v *skinView) editBounds(i int, password bool) image.Rectangle {
	x, w, y := 918, 296, v.mainCard().Min.Y+96
	if password {
		y += 61
	}
	if i == 1 {
		x, w, y = 582, 272, secondaryTop(v.u.slots[1] != nil)+60
		if password {
			y += 58
		}
	}
	return image.Rect(x+8, y+7, x+w-8, y+26)
}
func toWalk(r image.Rectangle) walk.Rectangle {
	return walk.Rectangle{X: r.Min.X, Y: r.Min.Y, Width: r.Dx(), Height: r.Dy()}
}
func (v *skinView) designBounds() map[win.HWND]walk.Rectangle {
	out := map[win.HWND]walk.Rectangle{}
	add := func(w walk.Widget, r image.Rectangle) {
		if w != nil {
			out[w.Handle()] = toWalk(r)
		}
	}
	add(v.canvas, image.Rect(0, 0, 1280, 728))
	for i := 0; i < 2; i++ {
		add(v.u.accounts[i], v.editBounds(i, false))
		add(v.u.passwords[i], v.editBounds(i, true))
	}
	add(v.u.code, image.Rect(368, 383, 912, 402))
	add(v.u.history, image.Rect(360, 498, 920, 618))
	return out
}
