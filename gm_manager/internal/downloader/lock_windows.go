package downloader

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"golang.org/x/sys/windows"
	"runtime"
	"strings"
)

func lockDirectory(path string) (func(), error) {
	name, _ := windows.UTF16PtrFromString(fmt.Sprintf("Local\\PY90-download-%x", sha256.Sum256([]byte(strings.ToLower(path)))))
	h, e := windows.CreateMutex(nil, false, name)
	if e != nil && e != windows.ERROR_ALREADY_EXISTS {
		return nil, e
	}
	runtime.LockOSThread()
	result, e := windows.WaitForSingleObject(h, 0)
	if e != nil || (result != windows.WAIT_OBJECT_0 && result != windows.WAIT_ABANDONED) {
		windows.CloseHandle(h)
		runtime.UnlockOSThread()
		return nil, errors.New("此目录已有下载器正在运行")
	}
	return func() { windows.ReleaseMutex(h); windows.CloseHandle(h); runtime.UnlockOSThread() }, nil
}
