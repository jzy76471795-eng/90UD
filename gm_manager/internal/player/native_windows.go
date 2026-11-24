//go:build windows

package player

import (
	"encoding/binary"
	"errors"
	"fmt"
	"golang.org/x/sys/windows"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

type Game struct {
	PID       uint32
	handle    windows.Handle
	Directory string
	done      chan struct{}
	mu        sync.Mutex
	closed    bool
}

func (g *Game) Done() <-chan struct{} { return g.done }
func (g *Game) Alive() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.closed {
		return false
	}
	v, e := windows.WaitForSingleObject(g.handle, 0)
	return e == nil && v == uint32(windows.WAIT_TIMEOUT)
}
func (g *Game) Stop() error {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.closed {
		return nil
	}
	return windows.TerminateProcess(g.handle, 1)
}

type windowProbe struct {
	PID    uint32
	Window windows.HWND
	Thread uint32
	Area   int64
}

var probeMu sync.Mutex
var enumerationMu sync.Mutex
var activeProbe *windowProbe
var windowCallback = syscall.NewCallback(func(h uintptr, _ uintptr) uintptr {
	probeMu.Lock()
	defer probeMu.Unlock()
	p := activeProbe
	if p == nil {
		return 1
	}
	var pid uint32
	tid, _ := windows.GetWindowThreadProcessId(windows.HWND(h), &pid)
	visible, _, _ := user32.NewProc("IsWindowVisible").Call(h)
	owner, _, _ := user32.NewProc("GetWindow").Call(h, 4)
	if pid == p.PID && visible != 0 && owner == 0 {
		var rect struct{ L, T, R, B int32 }
		ok, _, _ := user32.NewProc("GetWindowRect").Call(h, uintptr(unsafe.Pointer(&rect)))
		area := int64(rect.R-rect.L) * int64(rect.B-rect.T)
		if ok != 0 && area > p.Area {
			p.Window = windows.HWND(h)
			p.Thread = tid
			p.Area = area
		}
	}
	return 1
})

func (g *Game) Window() (windows.HWND, uint32) {
	enumerationMu.Lock()
	defer enumerationMu.Unlock()
	p := &windowProbe{PID: g.PID}
	probeMu.Lock()
	activeProbe = p
	probeMu.Unlock()
	_ = windows.EnumWindows(windowCallback, nil)
	probeMu.Lock()
	defer probeMu.Unlock()
	activeProbe = nil
	return p.Window, p.Thread
}
func (g *Game) bootstrap() {
	deadline := time.Now().Add(90 * time.Second)
	for time.Now().Before(deadline) {
		if !g.Alive() {
			return
		}
		_, thread := g.Window()
		if thread != 0 {
			for name, magic := range map[string]string{"USDOFNativeLoadoutThread.bin": "ULNT", "USDOFFuncExtendThread.bin": "UTBT"} {
				b := make([]byte, 16)
				copy(b, magic)
				binary.LittleEndian.PutUint32(b[4:], 1)
				binary.LittleEndian.PutUint32(b[8:], g.PID)
				binary.LittleEndian.PutUint32(b[12:], thread)
				_ = Durable(filepath.Join(g.Directory, "game-temp", name), b)
			}
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
}
func injectDLL(process windows.Handle, path string) error {
	expectedHash, e := FileHash(path)
	if e != nil {
		return e
	}
	wide, e := windows.UTF16FromString(path)
	if e != nil {
		return e
	}
	size := uintptr(len(wide) * 2)
	memory, _, e := kernel.NewProc("VirtualAllocEx").Call(uintptr(process), 0, size, 0x3000, 0x04)
	if memory == 0 {
		return LastNativeError("无法准备游戏组件内存", e)
	}
	// On an unconfirmed remote thread, leave its path buffer until the child is terminated.
	safeToFree := true
	defer func() {
		if safeToFree {
			kernel.NewProc("VirtualFreeEx").Call(uintptr(process), memory, 0, 0x8000)
		}
	}()
	var written uintptr
	if e = windows.WriteProcessMemory(process, memory, (*byte)(unsafe.Pointer(&wide[0])), size, &written); e != nil || written != size {
		return errors.New("写入游戏组件路径失败")
	}
	load := kernel.NewProc("LoadLibraryW").Addr()
	var probe byte
	var got uintptr
	if e = windows.ReadProcessMemory(process, load, &probe, 1, &got); e != nil || got != 1 {
		return errors.New("无法核对目标游戏加载器地址")
	}
	thread, _, e := kernel.NewProc("CreateRemoteThread").Call(uintptr(process), 0, 0, load, memory, 0, 0)
	if thread == 0 {
		return LastNativeError("无法加载游戏组件", e)
	}
	defer windows.CloseHandle(windows.Handle(thread))
	wait, e := windows.WaitForSingleObject(windows.Handle(thread), 20000)
	if e != nil || wait != windows.WAIT_OBJECT_0 {
		safeToFree = false
		return errors.New("游戏组件加载未确认，停止挂起的游戏进程")
	}
	var code uint32
	ok, _, e := kernel.NewProc("GetExitCodeThread").Call(thread, uintptr(unsafe.Pointer(&code)))
	if ok == 0 || code == 0 {
		return LastNativeError("游戏组件加载失败", e)
	}
	pid, e := windows.GetProcessId(process)
	if e != nil {
		return e
	}
	for attempt := 0; attempt < 20; attempt++ {
		snapshot, e := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPMODULE|windows.TH32CS_SNAPMODULE32, pid)
		if e == nil {
			var row windows.ModuleEntry32
			row.Size = uint32(unsafe.Sizeof(row))
			for e = windows.Module32First(snapshot, &row); e == nil; e = windows.Module32Next(snapshot, &row) {
				if row.ModBaseAddr == uintptr(code) && strings.EqualFold(windows.UTF16ToString(row.Module[:]), filepath.Base(path)) {
					actualHash, e := FileHash(windows.UTF16ToString(row.ExePath[:]))
					windows.CloseHandle(snapshot)
					if e == nil && actualHash == expectedHash {
						return nil
					}
					return errors.New("游戏实际加载组件与核对文件不一致")
				}
			}
			windows.CloseHandle(snapshot)
		}
		time.Sleep(25 * time.Millisecond)
	}
	return errors.New("游戏组件返回值未能与实际加载模块对应")
}
func initializeLoader(process windows.Handle) error {
	entry := windows.NewLazySystemDLL("ntdll.dll").NewProc("RtlExitUserThread").Addr()
	thread, _, e := kernel.NewProc("CreateRemoteThread").Call(uintptr(process), 0, 0, entry, 0, 0, 0)
	if thread == 0 {
		return LastNativeError("无法初始化挂起进程加载器", e)
	}
	defer windows.CloseHandle(windows.Handle(thread))
	wait, e := windows.WaitForSingleObject(windows.Handle(thread), 20000)
	if e != nil || wait != windows.WAIT_OBJECT_0 {
		return errors.New("挂起进程加载器未确认，游戏未恢复")
	}
	return nil
}
func allowSecond(process windows.Handle) error {
	expected := []byte{0xff, 0x15, 0x84, 0x75, 0x62, 0x03, 0x85, 0xc0, 0x74, 0x0e, 0x50, 0xff, 0x15, 0xcc, 0x74, 0x62, 0x03, 0x32, 0xc0, 0x5e, 0x8b, 0xe5, 0x5d, 0xc3}
	got := make([]byte, len(expected))
	var n uintptr
	if e := windows.ReadProcessMemory(process, 0x02c3709a, &got[0], uintptr(len(got)), &n); e != nil || n != uintptr(len(got)) || string(got) != string(expected) {
		return errors.New("冻结客户端双开签名不匹配")
	}
	var old uint32
	ok, _, e := kernel.NewProc("VirtualProtectEx").Call(uintptr(process), 0x02c370a2, 1, 0x40, uintptr(unsafe.Pointer(&old)))
	if ok == 0 {
		return LastNativeError("无法应用当前游戏实例规则", e)
	}
	value := byte(0xeb)
	writeErr := windows.WriteProcessMemory(process, 0x02c370a2, &value, 1, &n)
	var ignored uint32
	restored, _, _ := kernel.NewProc("VirtualProtectEx").Call(uintptr(process), 0x02c370a2, 1, uintptr(old), uintptr(unsafe.Pointer(&ignored)))
	flushed, _, _ := kernel.NewProc("FlushInstructionCache").Call(uintptr(process), 0x02c370a2, 1)
	var result byte
	readErr := windows.ReadProcessMemory(process, 0x02c370a2, &result, 1, &n)
	if writeErr != nil || restored == 0 || flushed == 0 || readErr != nil || result != value {
		return errors.New("当前游戏实例规则校验失败")
	}
	return nil
}

// Launch creates only the frozen game executable and never invokes a legacy injector or a shell.
func Launch(plan ClientPlan, session Session, key, data string, bound func(*Game) error) (game *Game, err error) {
	if runtime.GOARCH != "386" {
		return nil, errors.New("启动冻结32位游戏需要配套Go x86登录器")
	}
	if err = session.Stage.Validate(); err != nil {
		return nil, err
	}
	if strings.ContainsAny(session.Stage.Argument(), " \t\r\n\"") {
		return nil, errors.New("启动参数无效")
	}
	checked, e := ValidateClient(plan.Root, session.Stage)
	if e != nil {
		return nil, e
	}
	plan = checked
	custom := session.Stage.PvfProfile == "keyset96"
	var rawKeys []byte
	if custom {
		rawKeys, e = VerifyPvfKey(plan.Pvf, key)
		if e != nil {
			return nil, e
		}
		defer clear(rawKeys)
	}
	dir := filepath.Join(data, "runtime", ID())
	if e = NoLinks(dir); e != nil {
		return nil, e
	}
	temp := filepath.Join(dir, "game-temp")
	if e = os.MkdirAll(temp, 0700); e != nil {
		return nil, e
	}
	dlls := append([]string(nil), plan.DLLs...)
	rows := nativeEnvironment(plan.Root, temp, session.Stage)
	env, e := environmentBlock(rows)
	if e != nil {
		return nil, e
	}
	defer clear(env)
	exe, _ := windows.UTF16PtrFromString(plan.Exe)
	cwd, _ := windows.UTF16PtrFromString(plan.Root)
	command, e := windows.UTF16FromString(syscall.EscapeArg(plan.Exe) + " " + session.Stage.Argument())
	if e != nil {
		return nil, e
	}
	defer clear(command)
	si := windows.StartupInfo{Cb: uint32(unsafe.Sizeof(windows.StartupInfo{}))}
	var pi windows.ProcessInformation
	if e = windows.CreateProcess(exe, &command[0], nil, nil, false, windows.CREATE_SUSPENDED|windows.CREATE_UNICODE_ENVIRONMENT, &env[0], cwd, &si, &pi); e != nil {
		return nil, LastNativeError("无法创建冻结游戏进程", e)
	}
	clear(env)
	clear(command)
	defer windows.CloseHandle(pi.Thread)
	g := &Game{PID: pi.ProcessId, handle: pi.Process, Directory: dir, done: make(chan struct{})}
	success := false
	defer func() {
		if !success {
			_ = windows.TerminateProcess(pi.Process, 1)
			_, _ = windows.WaitForSingleObject(pi.Process, 5000)
			windows.CloseHandle(pi.Process)
			close(g.done)
		}
	}()
	if session.Stage.NativeTunnelVersion == 2 {
		if e = allowSecond(pi.Process); e != nil {
			return nil, e
		}
	}
	if bound != nil {
		if e = bound(g); e != nil {
			return nil, e
		}
	}
	if e = initializeLoader(pi.Process); e != nil {
		return nil, e
	}
	if custom {
		base, e := gameImageBase(pi.Process)
		if e != nil {
			return nil, e
		}
		if e = applyRemotePvfKeys(pi.Process, base, rawKeys); e != nil {
			return nil, e
		}
		clear(rawKeys)
	}
	for _, dll := range dlls {
		if e = injectDLL(pi.Process, dll); e != nil {
			return nil, fmt.Errorf("%s：%w", filepath.Base(dll), e)
		}
	}
	if _, e = windows.ResumeThread(pi.Thread); e != nil {
		return nil, errors.New("无法恢复游戏线程")
	}
	success = true
	go func() {
		_, _ = windows.WaitForSingleObject(pi.Process, windows.INFINITE)
		g.mu.Lock()
		g.closed = true
		windows.CloseHandle(pi.Process)
		g.mu.Unlock()
		close(g.done)
	}()
	go g.bootstrap()
	return g, nil
}
