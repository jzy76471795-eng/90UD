//go:build windows

package player

import (
	"golang.org/x/sys/windows"
	"io"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
	"unsafe"
)

func TestRelayPreservesTCPHalfClose(t *testing.T) {
	remote, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer remote.Close()
	go func() {
		c, e := remote.Accept()
		if e != nil {
			return
		}
		defer c.Close()
		b, _ := io.ReadAll(c)
		time.Sleep(10 * time.Millisecond)
		c.Write(append([]byte("response:"), b...))
	}()
	relay, e := newRelay("127.0.0.1", remote.Addr().(*net.TCPAddr).Port, 0, nil)
	if e != nil {
		t.Fatal(e)
	}
	defer relay.Close()
	conn, e := net.DialTCP("tcp4", nil, relay.listener.Addr().(*net.TCPAddr))
	if e != nil {
		t.Fatal(e)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(3 * time.Second))
	conn.Write([]byte("complete-request"))
	conn.CloseWrite()
	b, e := io.ReadAll(conn)
	if e != nil || string(b) != "response:complete-request" {
		t.Fatal("half-close lost delayed reply", e)
	}
}
func TestTCPConnectionUsesActualOwnerPID(t *testing.T) {
	listener, e := net.ListenTCP("tcp4", &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if e != nil {
		t.Fatal(e)
	}
	defer listener.Close()
	conn, e := net.DialTCP("tcp4", nil, listener.Addr().(*net.TCPAddr))
	if e != nil {
		t.Fatal(e)
	}
	defer conn.Close()
	accepted, e := listener.AcceptTCP()
	if e != nil {
		t.Fatal(e)
	}
	defer accepted.Close()
	pid, e := TCPPeerPID(conn.LocalAddr().(*net.TCPAddr).Port, conn.RemoteAddr().(*net.TCPAddr).Port)
	if e != nil || pid != uint32(os.Getpid()) {
		t.Fatal("TCP owner mismatch", pid, e)
	}
	pool := RoutePool{leases: map[*RouteLease]bool{}}
	if _, e = pool.resolve(accepted); e == nil {
		t.Fatal("unregistered local process received game credentials")
	}
}
func TestGameEnvironmentDoesNotInheritManagementSecrets(t *testing.T) {
	t.Setenv("USDOF_GM_TOKEN", "should-not-inherit")
	t.Setenv("DNF_REMOTE_TOKEN", "should-not-inherit")
	t.Setenv("DNF_PYTHON_OLD_KEY", "should-not-inherit")
	rows := nativeEnvironment(t.TempDir(), t.TempDir(), fixtureStage())
	for _, s := range rows {
		if strings.Contains(s, "should-not-inherit") {
			t.Fatal("inherited management secret")
		}
	}
	if strings.Contains(fixtureStage().String(), fixtureStage().Token) {
		t.Fatal("stage formatting exposed credential")
	}
}
func TestOptionalNativeGoSuspendedLaunchFixture(t *testing.T) {
	path := os.Getenv("GO_LAUNCHER_NATIVE_FIXTURE")
	if path == "" {
		t.Skip("explicit owned x86 fixture only")
	}
	if runtime.GOARCH != "386" {
		t.Skip("native child injector is x86")
	}
	if !strings.EqualFold(filepath.Clean(path), `E:\codex\go-launcher-20261031\验证\native-fixture.exe`) {
		t.Fatal("unexpected native fixture")
	}
	marker := filepath.Join(t.TempDir(), "started.txt")
	app, _ := windows.UTF16PtrFromString(path)
	command, _ := windows.UTF16FromString(syscall.EscapeArg(path) + " " + syscall.EscapeArg(marker))
	si := windows.StartupInfo{Cb: uint32(unsafe.Sizeof(windows.StartupInfo{}))}
	var pi windows.ProcessInformation
	if e := windows.CreateProcess(app, &command[0], nil, nil, false, windows.CREATE_SUSPENDED, nil, nil, &si, &pi); e != nil {
		t.Fatal(e)
	}
	defer windows.CloseHandle(pi.Process)
	defer windows.CloseHandle(pi.Thread)
	defer windows.TerminateProcess(pi.Process, 1)
	if _, e := os.Stat(marker); !os.IsNotExist(e) {
		t.Fatal("child ran before validation")
	}
	if e := initializeLoader(pi.Process); e != nil {
		t.Fatal(e)
	}
	testRemotePvfKeyGate(t, pi.Process)
	system, e := windows.GetSystemDirectory()
	if e != nil {
		t.Fatal(e)
	}
	if e = injectDLL(pi.Process, filepath.Join(system, "version.dll")); e != nil {
		t.Fatal("native Go DLL load", e)
	}
	if _, e = windows.ResumeThread(pi.Thread); e != nil {
		t.Fatal(e)
	}
	wait, e := windows.WaitForSingleObject(pi.Process, 10000)
	if e != nil || wait != windows.WAIT_OBJECT_0 {
		t.Fatal("fixture child did not exit", e)
	}
	b, e := os.ReadFile(marker)
	if e != nil || string(b) != "fixture-started" {
		t.Fatal("native child did not resume", e)
	}
	if report := os.Getenv("GO_LAUNCHER_NATIVE_REPORT"); report != "" {
		if e = os.WriteFile(report, []byte(`{"owned_x86_fixture":true,"go_created_suspended":true,"system_dll_loaded_by_go":true,"resumed_after_load":true,"game_started":false,"server_contacted":false}`), 0600); e != nil {
			t.Fatal(e)
		}
	}
}

func testRemotePvfKeyGate(t *testing.T, process windows.Handle) {
	t.Helper()
	base, _, e := kernel.NewProc("VirtualAllocEx").Call(uintptr(process), 0, 0x4000000, 0x3000, 0x04)
	if base == 0 {
		t.Fatal(e)
	}
	defer kernel.NewProc("VirtualFreeEx").Call(uintptr(process), base, 0, 0x8000)
	put := func(address uintptr, b []byte) {
		t.Helper()
		var n uintptr
		if e := windows.WriteProcessMemory(process, address, &b[0], uintptr(len(b)), &n); e != nil || n != uintptr(len(b)) {
			t.Fatal("fixture write", e)
		}
	}
	call := base + 0x030194a3 - 0x00400000
	put(call, []byte{0xe8, 0x98, 0xb0, 0xff, 0xff})
	for i, original := range pvfFrozen {
		put(base+pvfAddresses[i]-0x00400000, original[:])
	}
	key := make([]byte, 48)
	for i := range key {
		key[i] = byte(i + 1)
	}
	put(call, []byte{0})
	if applyRemotePvfKeys(process, base, key) == nil {
		t.Fatal("accepted changed complete client signature")
	}
	for i, original := range pvfFrozen {
		got, e := readMemory(process, base+pvfAddresses[i]-0x00400000, 20)
		if e != nil || string(got) != string(original[:]) {
			t.Fatal("wrote keys before complete signature check")
		}
	}
	put(call, []byte{0xe8})
	if e := applyRemotePvfKeys(process, base, key); e != nil {
		t.Fatal("Go remote PVF key gate", e)
	}
	for i, order := range keyOrder {
		got, e := readMemory(process, base+pvfAddresses[i]-0x00400000, 20)
		if e != nil || got[0] != 0xa1 || got[1] != 0x2c || string(got[4:12]) != string(key[order*8:order*8+8]) {
			t.Fatal("PVF key mapping mismatch")
		}
	}
}
