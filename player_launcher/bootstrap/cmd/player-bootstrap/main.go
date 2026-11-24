//go:build windows

// The native host uses the pinned private desktop runtime beside the launcher.
package main

import (
	"bytes"
	"dnf90manager/internal/launcherbundle"
	"dnf90manager/internal/launcherruntime"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"unsafe"
)

func run() error {
	if err := checkWindowsPrerequisites(); err != nil { return err }
	self, err := os.Executable()
	if err != nil {
		return err
	}
	f, err := os.Open(self)
	if err != nil {
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	root := filepath.Dir(self)
	_, hostEnd, err := launcherbundle.Read(f, info.Size(), launcherbundle.MaxConfig, launcherbundle.ConfigMagic)
	if err != nil {
		return err
	}
	runtimeManifest, err := launcherruntime.Read(f, hostEnd)
	if err != nil {
		return err
	}
	if runtimeManifest == nil {
		return fmt.Errorf("缺少配套 runtime 清单，请重新生成完整登陆器")
	}
	dotnetRoot, err := launcherruntime.Verify(root, runtimeManifest)
	if err != nil {
		return err
	}
	cacheRoot := filepath.Join(root, "data")
	if info, err := os.Stat(`E:\codex`); err == nil && info.IsDir() {
		cacheRoot = filepath.Join(`E:\codex`, "登录器缓存", "gm-native-mail")
	}
	executable, err := prepareCachedImage(f, info.Size(), cacheRoot)
	if err != nil {
		return err
	}
	extraction := filepath.Join(cacheRoot, "dotnet")
	temp := filepath.Join(cacheRoot, "temp")
	if err = os.MkdirAll(temp, 0700); err != nil {
		return err
	}
	profile := filepath.Join(cacheRoot, "profiles")
	if err = os.MkdirAll(profile, 0700); err != nil {
		return err
	}
	command := exec.Command(executable, os.Args[1:]...)
	command.Dir = root
	// Replace, rather than duplicate, inherited environment entries.
	env := map[string]string{"DOTNET_BUNDLE_EXTRACT_BASE_DIR": extraction, "PY90_PORTABLE_ROOT": root, "PY90_LAUNCHER_EXE": self, "TEMP": temp, "TMP": temp, "YH_SINGLE_FILE_HOST": "1", "YH_LAUNCHER_DIRECTORY": root, "YH_LAUNCHER_DATA": profile}
	runtimePath := runtimeEnvironmentPath(dotnetRoot, runtimeManifest.Files)
	env["DOTNET_ROOT"] = runtimePath
	env["DOTNET_ROOT_X86"] = runtimePath
	env["DOTNET_ROOT(x86)"] = runtimePath
	env["DOTNET_MULTILEVEL_LOOKUP"] = "0"
	env["DOTNET_ROLL_FORWARD"] = "LatestPatch"
	command.Env = os.Environ()
	for k, v := range env {
		command.Env = replaceEnv(command.Env, k, v)
	}
	command.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	return command.Run()
}

func replaceEnv(env []string, key, value string) []string {
	// Windows names are case-insensitive.
	for i := 0; i < len(env); i++ {
		split := bytes.IndexByte([]byte(env[i]), '=')
		if split > 0 && bytes.EqualFold([]byte(env[i][:split]), []byte(key)) {
			env = append(env[:i], env[i+1:]...)
			i--
		}
	}
	return append(env, key+"="+value)
}

func main() {
	if err := run(); err != nil {
		text, _ := syscall.UTF16PtrFromString("登录器启动失败：" + err.Error())
		title, _ := syscall.UTF16PtrFromString("玩家登录器")
		syscall.NewLazyDLL("user32.dll").NewProc("MessageBoxW").Call(0, uintptr(unsafe.Pointer(text)), uintptr(unsafe.Pointer(title)), 0x10)
		os.Exit(1)
	}
}
