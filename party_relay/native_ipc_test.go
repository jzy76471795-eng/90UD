package main

import (
	"context"
	"encoding/binary"
	"os"
	"syscall"
	"testing"
	"time"
)

func snapshotBytes(game gameIdentity) []byte {
	b := make([]byte, 56)
	binary.LittleEndian.PutUint32(b, game.PID)
	binary.LittleEndian.PutUint32(b[4:], 10)
	binary.LittleEndian.PutUint64(b[8:], game.Created)
	binary.LittleEndian.PutUint32(b[16:], 97)
	binary.LittleEndian.PutUint32(b[20:], 1)
	binary.LittleEndian.PutUint64(b[24:], 7)
	binary.LittleEndian.PutUint16(b[32:], 12345)
	binary.LittleEndian.PutUint16(b[34:], 1)
	binary.LittleEndian.PutUint32(b[44:], 222)
	binary.LittleEndian.PutUint32(b[52:], 1)
	return b
}
func TestNativeIPCIdentityProofAndMalformedFrames(t *testing.T) {
	game := gameIdentity{PID: 1, Created: 2}
	raw := snapshotBytes(game)
	n, err := parseNativeSnapshot(raw, game)
	if err != nil || !n.Peers[0].Confirmed || n.Peers[0].State != 0 {
		t.Fatalf("positive incoming proof: %v", err)
	}
	cases := []struct {
		offset int
		value  uint32
	}{{0, 2}, {8, 3}, {36, 1}, {40, 10}, {44, 0}, {48, 1}, {52, 2}}
	for _, v := range cases {
		b := append([]byte{}, raw...)
		binary.LittleEndian.PutUint32(b[v.offset:], v.value)
		if _, e := parseNativeSnapshot(b, game); e == nil {
			t.Fatalf("invalid snapshot at %d", v.offset)
		}
	}
	frame, _ := ipcFrame(1, 1, raw)
	for _, off := range []int{0, 4, 6, 10, 12, 16} {
		b := append([]byte{}, frame[:24]...)
		b[off] ^= 0xff
		if _, e := ipcSize(b, 1, 1); e == nil {
			t.Fatalf("invalid header at %d", off)
		}
	}
	if _, e := ipcFrame(1, 0, nil); e == nil {
		t.Fatal("zero sequence")
	}
	v := localView{Game: game, Context: 1, Version: 1, Until: 1, Required: true, Routes: peerView(1).Peers}
	v.Routes[0].Local = 12345
	if _, e := encodeLocalView(v); e != nil {
		t.Fatal(e)
	}
	v.Routes = append(v.Routes, v.Routes[0])
	v.Routes[1].Member = 3
	v.Routes[1].Local = 12346
	if _, e := encodeLocalView(v); e == nil {
		t.Fatal("duplicate actor/key endpoint")
	}
	v.Routes = v.Routes[:1]
	v.Context = 0
	if _, e := encodeLocalView(v); e == nil {
		t.Fatal("routes without native context")
	}
}
func TestWindowsPrivatePipeBindingAndFirstInstance(t *testing.T) {
	name := `\\.\pipe\90us-relay-` + randomHex(16)
	bridge, err := newPipeBridge(name)
	if err != nil {
		t.Fatal(err)
	}
	defer bridge.Close()
	if second, e := newPipeBridge(name); e == nil {
		second.Close()
		t.Fatal("pipe name hijacked")
	}
	created, err := processCreated(uint32(os.Getpid()))
	if err != nil {
		t.Fatal(err)
	}
	game := gameIdentity{PID: uint32(os.Getpid()), Created: created}
	wrong := game
	wrong.Created++
	if bridge.bind(wrong) == nil {
		t.Fatal("wrong creation time accepted")
	}
	if err = bridge.bind(game); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- bridge.serve(ctx, func(n nativeReport) localView {
			if n.Actor != 10 || !n.Peers[0].Confirmed {
				panic("invalid pipe snapshot")
			}
			return localView{Context: n.Context, Version: 8, Required: true}
		})
	}()
	path, _ := syscall.UTF16PtrFromString(name)
	h, err := syscall.CreateFile(path, syscall.GENERIC_READ|syscall.GENERIC_WRITE, 0, nil, syscall.OPEN_EXISTING, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer syscall.CloseHandle(h)
	frame, _ := ipcFrame(1, 1, snapshotBytes(game))
	if err = pipeIO(ctx, h, frame, true); err != nil {
		t.Fatal(err)
	}
	header := make([]byte, 24)
	if err = pipeIO(ctx, h, header, false); err != nil {
		t.Fatal(err)
	}
	length, err := ipcSize(header, 2, 1)
	if err != nil {
		t.Fatal(err)
	}
	body := make([]byte, length)
	if err = pipeIO(ctx, h, body, false); err != nil {
		t.Fatal(err)
	}
	if length != 40 || binary.LittleEndian.Uint32(body) != game.PID || binary.LittleEndian.Uint64(body[8:]) != created || binary.LittleEndian.Uint64(body[16:]) != 8 {
		t.Fatal("pipe identity/response mismatch")
	}
	cancel()
	bridge.Close()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("private pipe cancellation blocked")
	}
}
