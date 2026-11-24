//go:build windows

package main

import (
	"github.com/lxn/walk"
	"github.com/lxn/win"
)

// The original template has fixed design coordinates. Preserve those bounds
// while still allowing the toolkit to lay out CDK/settings children normally.
type skinFixedLayout struct{ view *skinView }

func (s skinFixedLayout) Create() (walk.Layout, error) { return &fixedLayout{view: s.view}, nil }

type fixedLayout struct {
	walk.LayoutBase
	container walk.Container
	view      *skinView
}

func (l *fixedLayout) Container() walk.Container { return l.container }
func (l *fixedLayout) SetContainer(c walk.Container) {
	l.container = c
	if c != nil && c.Layout() != l {
		c.SetLayout(l)
	}
}
func (l *fixedLayout) CreateLayoutItem(ctx *walk.LayoutContext) walk.ContainerLayoutItem {
	result := &fixedLayoutItem{bounds: map[win.HWND]walk.Rectangle{}, min: walk.SizeFrom96DPI(walk.Size{Width: 1280, Height: 728}, l.container.DPI())}
	v := l.view
	dpi := l.container.DPI()
	add := func(h win.HWND, r walk.Rectangle) {
		result.bounds[h] = walk.Rectangle{X: walk.IntFrom96DPI(r.X, dpi), Y: walk.IntFrom96DPI(r.Y, dpi), Width: walk.IntFrom96DPI(r.Width, dpi), Height: walk.IntFrom96DPI(r.Height, dpi)}
	}
	for h, r := range v.designBounds() {
		add(h, r)
	}

	return result
}

type fixedLayoutItem struct {
	walk.ContainerLayoutItemBase
	bounds map[win.HWND]walk.Rectangle
	min    walk.Size
}

func (l *fixedLayoutItem) LayoutFlags() walk.LayoutFlags      { return 0 }
func (l *fixedLayoutItem) MinSize() walk.Size                 { return l.min }
func (l *fixedLayoutItem) MinSizeForSize(walk.Size) walk.Size { return l.min }
func (l *fixedLayoutItem) HasHeightForWidth() bool            { return false }
func (l *fixedLayoutItem) HeightForWidth(int) int             { return l.min.Height }
func (l *fixedLayoutItem) PerformLayout() []walk.LayoutResultItem {
	result := []walk.LayoutResultItem{}
	for _, child := range l.Children() {
		if b, ok := l.bounds[child.Handle()]; ok {
			result = append(result, walk.LayoutResultItem{Item: child, Bounds: b})
		}
	}
	return result
}
