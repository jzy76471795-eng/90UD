//go:build windows

package player

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"golang.org/x/sys/windows"
	"io"
	"os"
	"unsafe"
)

var pvfAddresses = []uintptr{0x03d3ae60, 0x03d3ae74, 0x03d3ae88, 0x03d3ae9c, 0x03d57db8, 0x03d57dcc}
var pvfFrozen = [][20]byte{
	{0x00, 0x54, 0x51, 0x2a, 0x96, 0x4c, 0x3e, 0x9a, 0x2a, 0xf0, 0x5d, 0x4f, 0x56, 0x5a},
	{0x00, 0x54, 0x51, 0x2a, 0x9c, 0x4c, 0x34, 0x9a, 0x35, 0xf0, 0x7e, 0x4f, 0xcd, 0x9b},
	{0x00, 0x54, 0x51, 0x2a, 0x93, 0x4c, 0x03, 0x9a, 0x03, 0xf0, 0x2e, 0x4f, 0xbd, 0x2b},
	{0x00, 0x54, 0x51, 0x2a, 0x9c, 0x4c, 0x10, 0x9a, 0x07, 0xf0, 0x5e, 0x4f, 0xff, 0xf9},
	{0x00, 0x54, 0x51, 0x2a, 0xa7, 0x4c, 0x05, 0x9a, 0x0d, 0xf0, 0x3e, 0x4f, 0xb3, 0xe5},
	{0x00, 0x54, 0x51, 0x2a, 0xa7, 0x4c, 0x05, 0x9a, 0x0d, 0xf0, 0x28, 0x4f, 0xb3, 0xe5},
}
var keyOrder = []int{3, 0, 2, 1, 5, 4}

func cryptPvfHeader(header, key []byte) {
	a := uint32(binary.LittleEndian.Uint16(key))
	b := uint32(binary.LittleEndian.Uint16(key[2:]))
	c := uint32(binary.LittleEndian.Uint16(key[4:]))
	d := uint32(binary.LittleEndian.Uint16(key[6:]))
	seed := uint32(866031377)*a + 915*(d+915*(c+915*b))
	for i := 0; i < 32; i += 4 {
		s1 := 214013*seed + 2531011
		seed = 214013*s1 + 2531011
		mask := (s1 & 0xffff0000) | (seed >> 16)
		for j := 0; j < 4; j++ {
			header[i+j] ^= byte(mask >> uint(8*j))
		}
	}
}
func VerifyPvfKey(path, key string) ([]byte, error) {
	raw, e := hex.DecodeString(key)
	if e != nil || len(raw) != 48 {
		return nil, errors.New("PVF96密钥格式无效")
	}
	if e = NoLinks(path); e != nil {
		clear(raw)
		return nil, e
	}
	f, e := os.Open(path)
	if e != nil {
		clear(raw)
		return nil, e
	}
	defer f.Close()
	s, e := f.Stat()
	if e != nil || s.Size() < 32 {
		clear(raw)
		return nil, errors.New("PVF文件头无效")
	}
	header := make([]byte, 32)
	defer clear(header)
	if _, e = io.ReadFull(f, header); e != nil {
		clear(raw)
		return nil, e
	}
	cryptPvfHeader(header, raw[:8])
	end := int64(binary.LittleEndian.Uint32(header[28:]))
	if binary.LittleEndian.Uint32(header) != 0x69706b6e || end < 32 || end > s.Size() {
		clear(raw)
		return nil, errors.New("动态密钥不能解密当前PVF，游戏未恢复")
	}
	return raw, nil
}
func readMemory(process windows.Handle, address uintptr, size int) ([]byte, error) {
	b := make([]byte, size)
	var n uintptr
	if e := windows.ReadProcessMemory(process, address, &b[0], uintptr(size), &n); e != nil || n != uintptr(size) {
		return nil, errors.New("冻结游戏内存签名读取失败")
	}
	return b, nil
}
func gameImageBase(process windows.Handle) (uintptr, error) {
	var info struct{ Exit, PEB, Affinity, Priority, PID, Parent uintptr }
	var length uint32
	status, _, _ := windows.NewLazySystemDLL("ntdll.dll").NewProc("NtQueryInformationProcess").Call(uintptr(process), 0, uintptr(unsafe.Pointer(&info)), unsafe.Sizeof(info), uintptr(unsafe.Pointer(&length)))
	if status != 0 || info.PEB == 0 {
		return 0, errors.New("无法核对冻结游戏映像")
	}
	b, e := readMemory(process, info.PEB+8, 4)
	if e != nil {
		return 0, e
	}
	base := uintptr(binary.LittleEndian.Uint32(b))
	if base == 0 {
		return 0, errors.New("游戏映像基址无效")
	}
	return base, nil
}

// This is the reviewed frozen-client key gate, ported to Go. No loader DLL,
// readiness event, or secret environment variable is needed by the new launcher.
func applyRemotePvfKeys(process windows.Handle, base uintptr, keys []byte) (err error) {
	if len(keys) != 48 {
		return errors.New("PVF密钥长度无效")
	}
	call, e := readMemory(process, base+0x030194a3-0x00400000, 5)
	if e != nil || !bytes.Equal(call, []byte{0xe8, 0x98, 0xb0, 0xff, 0xff}) {
		return errors.New("冻结客户端PVF调用签名不匹配")
	}
	targets := make([]uintptr, 6)
	for i, original := range pvfFrozen {
		targets[i] = base + pvfAddresses[i] - 0x00400000
		got, e := readMemory(process, targets[i], 20)
		if e != nil || !bytes.Equal(got, original[:]) {
			return errors.New("冻结客户端PVF密钥位置签名不匹配")
		}
	}
	old := make([]uint32, 6)
	count := 0
	defer func() {
		for i := count - 1; i >= 0; i-- {
			var ignored uint32
			ok, _, _ := kernel.NewProc("VirtualProtectEx").Call(uintptr(process), targets[i], 20, uintptr(old[i]), uintptr(unsafe.Pointer(&ignored)))
			if ok == 0 && err == nil {
				err = errors.New("恢复游戏内存页保护失败")
			}
		}
	}()
	for i, target := range targets {
		ok, _, _ := kernel.NewProc("VirtualProtectEx").Call(uintptr(process), target, 20, 0x04, uintptr(unsafe.Pointer(&old[i])))
		if ok == 0 {
			return errors.New("无法准备PVF密钥页保护")
		}
		count++
	}
	for i, target := range targets {
		blob := make([]byte, 20)
		blob[0] = 0xa1
		blob[1] = 0x2c
		copy(blob[4:12], keys[8*keyOrder[i]:8*keyOrder[i]+8])
		var n uintptr
		e := windows.WriteProcessMemory(process, target, &blob[0], 20, &n)
		got, re := readMemory(process, target, 20)
		ok := e == nil && re == nil && n == 20 && bytes.Equal(got, blob)
		clear(blob)
		if !ok {
			return errors.New("PVF密钥写入校验失败")
		}
	}
	ok, _, _ := kernel.NewProc("FlushInstructionCache").Call(uintptr(process), 0, 0)
	if ok == 0 {
		return errors.New("刷新游戏指令缓存失败")
	}
	return nil
}
