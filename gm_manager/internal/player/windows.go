//go:build windows

package player

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"unsafe"
)

var kernel = windows.NewLazySystemDLL("kernel32.dll")
var user32 = windows.NewLazySystemDLL("user32.dll")

func AtomicReplace(src, dst string) error { return moveFile(src, dst, 0x9) }
func AtomicMoveNew(src, dst string) error { return moveFile(src, dst, 0x8) }
func moveFile(src, dst string, flags uintptr) error {
	if e := NoLinks(src); e != nil {
		return e
	}
	if e := NoLinks(dst); e != nil {
		return e
	}
	s, e := windows.UTF16PtrFromString(src)
	if e != nil {
		return e
	}
	d, e := windows.UTF16PtrFromString(dst)
	if e != nil {
		return e
	}
	ok, _, e := kernel.NewProc("MoveFileExW").Call(uintptr(unsafe.Pointer(s)), uintptr(unsafe.Pointer(d)), flags)
	if ok == 0 {
		return e
	}
	return nil
}
func LockFile(path string) (func(), error) {
	if e := NoLinks(path); e != nil {
		return nil, e
	}
	p, e := windows.UTF16PtrFromString(path)
	if e != nil {
		return nil, e
	}
	h, e := windows.CreateFile(p, windows.GENERIC_READ|windows.GENERIC_WRITE, 0, nil, windows.OPEN_ALWAYS, windows.FILE_ATTRIBUTE_NORMAL, 0)
	if e != nil {
		return nil, e
	}
	return func() { windows.CloseHandle(h) }, nil
}
func DeviceID() (string, error) {
	k, e := registry.OpenKey(registry.LOCAL_MACHINE, `SOFTWARE\Microsoft\Cryptography`, registry.QUERY_VALUE|registry.WOW64_64KEY)
	if e != nil {
		return "", errors.New("无法读取Windows设备标识")
	}
	defer k.Close()
	v, _, e := k.GetStringValue("MachineGuid")
	if e != nil || strings.TrimSpace(v) == "" {
		return "", errors.New("Windows设备标识无效")
	}
	return shaText("Py90-device-v1:" + strings.ToLower(strings.TrimSpace(v))), nil
}

type SavedLogin struct {
	Slot     int    `json:"slot"`
	Account  string `json:"account"`
	Password string `json:"password"`
}

func (s SavedLogin) String() string   { return "SavedLogin (credentials redacted)" }
func (s SavedLogin) GoString() string { return s.String() }
func crypt(data, entropy []byte, protect bool) ([]byte, error) {
	if len(data) == 0 || len(entropy) == 0 {
		return nil, errors.New("加密输入无效")
	}
	in := windows.DataBlob{Size: uint32(len(data)), Data: &data[0]}
	salt := windows.DataBlob{Size: uint32(len(entropy)), Data: &entropy[0]}
	var out windows.DataBlob
	var e error
	if protect {
		e = windows.CryptProtectData(&in, nil, &salt, 0, nil, 1, &out)
	} else {
		e = windows.CryptUnprotectData(&in, nil, &salt, 0, nil, 1, &out)
	}
	if e != nil {
		return nil, errors.New("无法访问当前Windows用户的已保存密码")
	}
	defer windows.LocalFree(windows.Handle(unsafe.Pointer(out.Data)))
	result := append([]byte(nil), unsafe.Slice(out.Data, out.Size)...)
	clear(unsafe.Slice(out.Data, out.Size))
	return result, nil
}
func (c Config) ReadLogin() ([]SavedLogin, error) {
	p := filepath.Join(c.DataDirectory(), "remembered-login.bin")
	if e := NoLinks(p); e != nil {
		return nil, e
	}
	b, e := readBounded(p, 65536)
	if os.IsNotExist(e) {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	entropy := sha256.Sum256([]byte("Go90-remember-v1|" + c.ServerID + "|" + c.LoginURL))
	plain, e := crypt(b, entropy[:], false)
	if e != nil {
		return nil, e
	}
	defer clear(plain)
	var rows []SavedLogin
	if StrictJSON(plain, &rows) != nil || len(rows) > 2 {
		return nil, errors.New("保存的账号信息无效")
	}
	seen := map[int]bool{}
	for _, v := range rows {
		if v.Slot < 0 || v.Slot > 1 || seen[v.Slot] || len(v.Account) == 0 || len(v.Account) > 256 || len(v.Password) < 1 || len(v.Password) > 512 {
			return nil, errors.New("保存的账号格式无效")
		}
		seen[v.Slot] = true
	}
	return rows, nil
}
func (c Config) SaveLogin(rows []SavedLogin) error {
	if len(rows) > 2 {
		return errors.New("最多保存两个账号")
	}
	dir := c.DataDirectory()
	if e := NoLinks(dir); e != nil {
		return e
	}
	if e := os.MkdirAll(dir, 0700); e != nil {
		return e
	}
	path := filepath.Join(dir, "remembered-login.bin")
	if len(rows) == 0 {
		if e := NoLinks(path); e != nil {
			return e
		}
		e := os.Remove(path)
		if os.IsNotExist(e) {
			return nil
		}
		return e
	}
	raw, e := json.Marshal(rows)
	if e != nil {
		return e
	}
	defer clear(raw)
	entropy := sha256.Sum256([]byte("Go90-remember-v1|" + c.ServerID + "|" + c.LoginURL))
	encrypted, e := crypt(raw, entropy[:], true)
	if e != nil {
		return e
	}
	temp := filepath.Join(dir, ".remember-"+ID())
	if e = Durable(temp, encrypted); e != nil {
		return e
	}
	return AtomicReplace(temp, path)
}
func ClientRunning(root string) bool {
	snap, e := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if e != nil {
		return true
	}
	defer windows.CloseHandle(snap)
	var row windows.ProcessEntry32
	row.Size = uint32(unsafe.Sizeof(row))
	for e = windows.Process32First(snap, &row); e == nil; e = windows.Process32Next(snap, &row) {
		if !strings.EqualFold(windows.UTF16ToString(row.ExeFile[:]), "DFO.exe") {
			continue
		}
		h, e := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, row.ProcessID)
		if e != nil {
			return true
		}
		b := make([]uint16, 32768)
		n := uint32(len(b))
		e = windows.QueryFullProcessImageName(h, 0, &b[0], &n)
		windows.CloseHandle(h)
		if e != nil || strings.EqualFold(filepath.Dir(windows.UTF16ToString(b[:n])), filepath.Clean(root)) {
			return true
		}
	}
	return false
}
func TCPPeerPID(localPort, remotePort int) (uint32, error) {
	proc := windows.NewLazySystemDLL("iphlpapi.dll").NewProc("GetExtendedTcpTable")
	size := uint32(0)
	_, _, _ = proc.Call(0, uintptr(unsafe.Pointer(&size)), 0, 2, 5, 0)
	if size < 4 || size > 16<<20 {
		return 0, errors.New("TCP进程表不可用")
	}
	b := make([]byte, size)
	r, _, _ := proc.Call(uintptr(unsafe.Pointer(&b[0])), uintptr(unsafe.Pointer(&size)), 0, 2, 5, 0)
	if r != 0 || int(size) > len(b) {
		return 0, errors.New("TCP进程表查询失败")
	}
	count := binary.LittleEndian.Uint32(b)
	if uint64(count)*24+4 > uint64(size) {
		return 0, errors.New("TCP进程表格式无效")
	}
	for i := uint32(0); i < count; i++ {
		row := b[4+i*24 : 4+(i+1)*24]
		if int(binary.BigEndian.Uint16(row[8:10])) == localPort && int(binary.BigEndian.Uint16(row[16:18])) == remotePort && binary.LittleEndian.Uint32(row[12:16]) == 0x0100007f {
			return binary.LittleEndian.Uint32(row[20:24]), nil
		}
	}
	return 0, errors.New("未找到实际游戏连接进程")
}
func nativeEnvironment(root, temp string, stage Stage) []string {
	values := map[string]string{}
	allowed := map[string]bool{"SYSTEMROOT": true, "WINDIR": true, "SYSTEMDRIVE": true, "PATH": true, "PATHEXT": true, "COMSPEC": true, "APPDATA": true, "LOCALAPPDATA": true, "PROGRAMDATA": true, "USERPROFILE": true, "PROGRAMFILES": true, "PROGRAMFILES(X86)": true, "COMMONPROGRAMFILES": true, "COMMONPROGRAMFILES(X86)": true, "COMPUTERNAME": true, "USERNAME": true, "USERDOMAIN": true, "NUMBER_OF_PROCESSORS": true, "PROCESSOR_ARCHITECTURE": true, "PROCESSOR_IDENTIFIER": true, "PROCESSOR_LEVEL": true, "PROCESSOR_REVISION": true, "OS": true, "LANG": true, "LC_ALL": true}
	for _, entry := range os.Environ() {
		k, v, ok := strings.Cut(entry, "=")
		if !ok || k == "" {
			continue
		}
		upper := strings.ToUpper(k)
		if !allowed[upper] {
			continue
		}
		if strings.HasPrefix(upper, "USDOF_") || strings.HasPrefix(upper, "DNF_REMOTE_") || strings.HasPrefix(upper, "DNF_PYTHON_") || upper == "DNF_PVF_KEYSET" {
			continue
		}
		values[upper] = v
	}
	set := map[string]string{"TEMP": temp, "TMP": temp, "TMPDIR": temp, "__COMPAT_LAYER": "DPIUNAWARE", "USDOF_CLIENT_DIR": root, "USDOF_CLIENT_EXE": filepath.Join(root, "DFO.exe"), "USDOF_PVF_PATH": filepath.Join(root, "Script.pvf"), "USDOF_EXPECTED_PVF_PROFILE": stage.PvfProfile, "USDOF_EXPECTED_PVF_SHA256": stage.PvfSHA256, "USDOF_PVF_BRIDGE": "0", "USDOF_SKIP_PVF_STARTUP_PREPROCESS": "1", "USDOF_FUNC_EXTEND_VIDEO": "1", "USDOF_FUNC_EXTEND_VIDEO_HOOK": "1", "USDOF_GAME_PLAIN_WIRE": "0", "USDOF_GAME_C2S_PLAIN_WIRE": "1", "USDOF_GAME_S2C_PLAIN_WIRE": "0", "USDOF_GAME_PLAIN_WIRE_HEADER": "16"}
	if stage.PvfProfile == "utility-guarded" {
		set["USDOF_PVF_BRIDGE"] = "1"
	}
	if stage.NativeTunnelVersion == 2 {
		set["DNF_PYTHON_ALLOW_SECOND_INSTANCE"] = "1"
	}
	for k, v := range map[string]string{"USDOF_NATIVE_LOADOUT_PLAN_PATH": "USDOFLoadoutNativePlan.bin", "USDOF_NATIVE_LOADOUT_ACK_PATH": "USDOFLoadoutNativeAck.bin", "USDOF_NATIVE_LOADOUT_THREAD_PATH": "USDOFNativeLoadoutThread.bin", "USDOF_FUNC_EXTEND_TOAST_REQUEST_PATH": "USDOFFuncExtendToastRequest.bin", "USDOF_FUNC_EXTEND_THREAD_PATH": "USDOFFuncExtendThread.bin", "USDOF_FUNC_EXTEND_VIDEO_COMMAND_PATH": "USDOFFuncExtendVideoCommand.bin"} {
		set[k] = filepath.Join(temp, v)
	}
	for k, v := range set {
		values[k] = v
	}
	var result []string
	for k, v := range values {
		result = append(result, k+"="+v)
	}
	return result
}
func environmentBlock(rows []string) ([]uint16, error) {
	sort.Slice(rows, func(i, j int) bool { return strings.ToUpper(rows[i]) < strings.ToUpper(rows[j]) })
	var b []uint16
	for _, s := range rows {
		v, e := windows.UTF16FromString(s)
		if e != nil {
			return nil, e
		}
		b = append(b, v...)
	}
	return append(b, 0), nil
}
func LastNativeError(message string, e error) error {
	if e == nil || e == syscall.Errno(0) {
		return errors.New(message)
	}
	return fmt.Errorf("%s（Windows %v）", message, e)
}
func decodeHex(v string) ([]byte, error) { return hex.DecodeString(v) }
