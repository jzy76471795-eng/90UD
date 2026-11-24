//go:build windows

package main

import "github.com/lxn/walk"

// Supply both the focused edit's background color and the bitmap pattern.
type tintedBitmapBrush struct {
	*walk.BitmapBrush
	color walk.Color
}

func (b *tintedBitmapBrush) Color() walk.Color { return b.color }
