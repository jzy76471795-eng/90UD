//go:build windows

package main

import (
	"fmt"
	"strings"
	"syscall"
	"unsafe"

	"github.com/lxn/walk"
	"github.com/lxn/win"
	"newadmin/internal/adminapi"
)

func generatedCodeStrings(codes []adminapi.Code) []string {
	values := make([]string, 0, len(codes))
	for _, code := range codes {
		if code.Code != "" {
			values = append(values, code.Code)
		}
	}
	return values
}

func copyCodes(codes []string, status *walk.Label) {
	if len(codes) == 0 {
		status.SetText("没有可复制的码")
		return
	}
	if err := writeClipboardText(applicationWindow.Handle(), strings.Join(codes, "\r\n")); err != nil {
		status.SetText(err.Error())
		return
	}
	status.SetText(fmt.Sprintf("已复制 %d 个码", len(codes)))
}

func writeClipboardText(owner win.HWND, text string) error {
	data, err := syscall.UTF16FromString(text)
	if err != nil {
		return fmt.Errorf("复制失败，文本包含无效字符")
	}
	memory := win.GlobalAlloc(win.GMEM_MOVEABLE, uintptr(len(data))*2)
	if memory == 0 {
		return fmt.Errorf("复制失败，无法分配内存")
	}
	defer func() {
		if memory != 0 {
			win.GlobalFree(memory)
		}
	}()
	buffer := win.GlobalLock(memory)
	if buffer == nil {
		return fmt.Errorf("复制失败，无法写入内存")
	}
	copy(unsafe.Slice((*uint16)(buffer), len(data)), data)
	win.GlobalUnlock(memory)

	if !win.OpenClipboard(owner) {
		return fmt.Errorf("剪贴板暂不可用，请重试")
	}
	defer win.CloseClipboard()
	if !win.EmptyClipboard() || win.SetClipboardData(win.CF_UNICODETEXT, win.HANDLE(memory)) == 0 {
		return fmt.Errorf("复制失败，请重试")
	}
	memory = 0 // Windows owns it after SetClipboardData succeeds.
	return nil
}
