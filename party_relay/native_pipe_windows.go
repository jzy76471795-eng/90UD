package main

import (
	"context"
	"encoding/hex"
	"errors"
	"io"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

var pipeKernel = syscall.NewLazyDLL("kernel32.dll")
var pipeCreate = pipeKernel.NewProc("CreateNamedPipeW")
var pipeConnect = pipeKernel.NewProc("ConnectNamedPipe")
var pipeDisconnect = pipeKernel.NewProc("DisconnectNamedPipe")
var pipeClientPID = pipeKernel.NewProc("GetNamedPipeClientProcessId")
var pipeTick = pipeKernel.NewProc("GetTickCount64")
var pipeLocalFree = pipeKernel.NewProc("LocalFree")
var pipeSecurity = syscall.NewLazyDLL("advapi32.dll").NewProc("ConvertStringSecurityDescriptorToSecurityDescriptorW")

type pipeAttributes struct {
	Length     uint32
	Descriptor uintptr
	Inherit    int32
}
type pipeBridge struct {
	mu     sync.RWMutex
	game   gameIdentity
	handle syscall.Handle
	name   string
}

func processCreated(pid uint32) (uint64, error) {
	h, e := syscall.OpenProcess(0x1000, false, pid)
	if e != nil {
		return 0, e
	}
	defer syscall.CloseHandle(h)
	var created, exit, kernel, user syscall.Filetime
	if e = syscall.GetProcessTimes(h, &created, &exit, &kernel, &user); e != nil {
		return 0, e
	}
	return uint64(created.HighDateTime)<<32 | uint64(created.LowDateTime), nil
}
func ticks() uint64 { value, _, _ := pipeTick.Call(); return uint64(value) }
func newPipeBridge(name string) (*pipeBridge, error) {
	prefix := `\\.\pipe\90us-relay-`
	suffix := strings.TrimPrefix(name, prefix)
	raw, e := hex.DecodeString(suffix)
	if !strings.HasPrefix(name, prefix) || len(raw) != 16 || e != nil || hex.EncodeToString(raw) != suffix {
		return nil, errors.New("invalid private pipe name")
	}
	path, _ := syscall.UTF16PtrFromString(name)
	sddl, _ := syscall.UTF16PtrFromString("D:P(A;;GA;;;SY)(A;;GA;;;OW)")
	var descriptor uintptr
	ok, _, e := pipeSecurity.Call(uintptr(unsafe.Pointer(sddl)), 1, uintptr(unsafe.Pointer(&descriptor)), 0)
	if ok == 0 {
		return nil, e
	}
	defer pipeLocalFree.Call(descriptor)
	security := pipeAttributes{Descriptor: descriptor}
	security.Length = uint32(unsafe.Sizeof(security))
	h, _, e := pipeCreate.Call(uintptr(unsafe.Pointer(path)), 3|0x80000, 1|8, 1, 16384, 16384, 100, uintptr(unsafe.Pointer(&security)))
	if h == uintptr(syscall.InvalidHandle) {
		return nil, e
	}
	return &pipeBridge{handle: syscall.Handle(h), name: name}, nil
}
func (p *pipeBridge) bind(v gameIdentity) error {
	actual, e := processCreated(v.PID)
	if e != nil || v.PID == 0 || v.Created == 0 || actual != v.Created {
		return errors.New("game process identity changed")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.game.PID != 0 && p.game != v {
		return errors.New("game process already bound")
	}
	p.game = v
	return nil
}
func (p *pipeBridge) identity() gameIdentity { p.mu.RLock(); defer p.mu.RUnlock(); return p.game }
func (p *pipeBridge) Close() error {
	p.mu.Lock()
	h := p.handle
	p.handle = syscall.InvalidHandle
	p.mu.Unlock()
	if h != syscall.InvalidHandle {
		return syscall.CloseHandle(h)
	}
	return nil
}
func pipeWait(ctx context.Context) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(5 * time.Millisecond):
		return nil
	}
}
func pipeIO(ctx context.Context, h syscall.Handle, b []byte, write bool) error {
	deadline := time.Now().Add(500 * time.Millisecond)
	for len(b) > 0 {
		var n uint32
		var e error
		if write {
			e = syscall.WriteFile(h, b, &n, nil)
		} else {
			e = syscall.ReadFile(h, b, &n, nil)
		}
		if n > 0 {
			b = b[n:]
		}
		if e != nil && e != syscall.Errno(232) && e != syscall.Errno(536) {
			return e
		}
		if len(b) == 0 {
			return nil
		}
		if time.Now().After(deadline) {
			return errors.New("private pipe deadline")
		}
		if e = pipeWait(ctx); e != nil {
			return e
		}
	}
	return nil
}
func (p *pipeBridge) serve(ctx context.Context, exchange func(nativeReport) localView) error {
	p.mu.RLock()
	h := p.handle
	p.mu.RUnlock()
	for ctx.Err() == nil {
		identity := p.identity()
		if identity.PID == 0 {
			if e := pipeWait(ctx); e != nil {
				return e
			}
			continue
		}
		created, e := processCreated(identity.PID)
		if e != nil || created != identity.Created {
			return errors.New("bound game process exited")
		}
		ok, _, err := pipeConnect.Call(uintptr(h), 0)
		if ok == 0 && err != syscall.Errno(535) {
			if err == syscall.Errno(232) {
				pipeDisconnect.Call(uintptr(h))
			}
			if err != syscall.Errno(536) && err != syscall.Errno(232) {
				return err
			}
			if e = pipeWait(ctx); e != nil {
				return e
			}
			continue
		}
		var pid uint32
		valid, _, _ := pipeClientPID.Call(uintptr(h), uintptr(unsafe.Pointer(&pid)))
		if valid == 0 || pid != identity.PID {
			pipeDisconnect.Call(uintptr(h))
			continue
		}
		actual, e := processCreated(pid)
		if e != nil || actual != identity.Created {
			pipeDisconnect.Call(uintptr(h))
			continue
		}
		var sequence uint64
		for ctx.Err() == nil {
			header := make([]byte, ipcHeader)
			if e = pipeIO(ctx, h, header, false); e != nil {
				break
			}
			sequence++
			n, err := ipcSize(header, 1, sequence)
			if err != nil {
				break
			}
			body := make([]byte, n)
			if err = pipeIO(ctx, h, body, false); err != nil {
				break
			}
			native, err := parseNativeSnapshot(body, identity)
			if err != nil {
				break
			}
			view := exchange(native)
			view.Game = identity
			payload, err := encodeLocalView(view)
			if err != nil {
				break
			}
			frame, _ := ipcFrame(2, sequence, payload)
			if err = pipeIO(ctx, h, frame, true); err != nil {
				break
			}
		}
		pipeDisconnect.Call(uintptr(h))
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return io.EOF
}
