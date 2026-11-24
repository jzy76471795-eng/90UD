//go:build windows

package main

import (
	"fmt"
	"os/user"
	"strings"
	"syscall"
	"unsafe"
)

// Windows ignores Unix permission bits. Each output gets a protected DACL:
// current user, LocalSystem and administrators only, with no inherited grants.
func protectLauncherPath(path string, directory bool) error {
	current, err := user.Current()
	if err != nil {
		return err
	}
	if !strings.HasPrefix(current.Uid, "S-1-") {
		return fmt.Errorf("无法获取当前 Windows 用户 SID")
	}
	inheritance := ""
	if directory {
		inheritance = "OICI"
	}
	sddl := "D:P(A;" + inheritance + ";FA;;;" + current.Uid + ")(A;" + inheritance + ";FA;;;SY)(A;" + inheritance + ";FA;;;BA)"
	text, err := syscall.UTF16PtrFromString(sddl)
	if err != nil {
		return err
	}
	name, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return err
	}
	advapi := syscall.NewLazyDLL("advapi32.dll")
	var descriptor uintptr
	ok, _, callErr := advapi.NewProc("ConvertStringSecurityDescriptorToSecurityDescriptorW").Call(uintptr(unsafe.Pointer(text)), 1, uintptr(unsafe.Pointer(&descriptor)), 0)
	if ok == 0 {
		return callErr
	}
	defer syscall.NewLazyDLL("kernel32.dll").NewProc("LocalFree").Call(descriptor)
	ok, _, callErr = advapi.NewProc("SetFileSecurityW").Call(uintptr(unsafe.Pointer(name)), 0x80000004, descriptor)
	if ok == 0 {
		return callErr
	}
	return nil
}

func commitLauncherDirectory(stage, destination string) error {
	from, err := syscall.UTF16PtrFromString(stage)
	if err != nil {
		return err
	}
	to, err := syscall.UTF16PtrFromString(destination)
	if err != nil {
		return err
	}
	// No MOVEFILE_REPLACE_EXISTING: a competing destination can never be replaced.
	ok, _, callErr := syscall.NewLazyDLL("kernel32.dll").NewProc("MoveFileExW").Call(uintptr(unsafe.Pointer(from)), uintptr(unsafe.Pointer(to)), 0x8)
	if ok == 0 {
		return callErr
	}
	return nil
}
