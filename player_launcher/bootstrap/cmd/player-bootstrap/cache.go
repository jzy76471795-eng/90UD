//go:build windows

package main

import (
	"bytes"
	"crypto/sha256"
	"dnf90manager/internal/launcherbundle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

func prepareCachedImage(f *os.File, end int64, cacheRoot string) (string, error) {
	config, start, err := launcherbundle.Read(f, end, launcherbundle.MaxConfig, launcherbundle.ConfigMagic)
	if err != nil {
		return "", err
	}
	var doc struct {
		ServerID string `json:"server_id"`
	}
	if err = json.Unmarshal(config, &doc); err != nil || doc.ServerID == "" {
		return "", fmt.Errorf("登录器服务器标识无效")
	}
	managed, _, hostHash, err := launcherbundle.ReadWithDigest(f, start, 512<<20, launcherbundle.HostMagic)
	if err != nil {
		return "", err
	}
	managed, err = decodeManagedHost(managed)
	if err != nil { return "", err }
	identity := sha256.Sum256(append(hostHash[:], config...))
	runtime := filepath.Join(cacheRoot, "host", hex.EncodeToString(identity[:]))
	if err = os.MkdirAll(runtime, 0700); err != nil {
		return "", fmt.Errorf("请将登录器放在可写目录：%w", err)
	}
	footer := launcherbundle.Footer(int64(len(config)), sha256.Sum256(config), launcherbundle.ConfigMagic)
	parts := [][]byte{managed, config, footer}
	executable := filepath.Join(runtime, "player.exe")
	if cachedImageMatches(executable, parts) {
		return executable, nil
	}
	temp, err := os.CreateTemp(runtime, "player-*.tmp")
	if err != nil {
		return "", err
	}
	tempName := temp.Name()
	defer os.Remove(tempName)
	var writeErr error
	for _, part := range parts {
		n, err := temp.Write(part)
		if err != nil {
			writeErr = err
			break
		}
		if n != len(part) {
			writeErr = io.ErrShortWrite
			break
		}
	}
	if writeErr == nil {
		writeErr = temp.Sync()
	}
	closeErr := temp.Close()
	if writeErr != nil {
		return "", writeErr
	}
	if closeErr != nil {
		return "", closeErr
	}
	// A failed replacement must not first delete the existing image.
	if err = os.Rename(tempName, executable); err != nil {
		return "", err
	}
	return executable, nil
}

func cachedImageMatches(path string, parts [][]byte) bool {
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()
	var size int64
	for _, part := range parts {
		size += int64(len(part))
	}
	info, err := f.Stat()
	if err != nil || info.Size() != size {
		return false
	}
	buffer := make([]byte, 2<<20)
	for _, part := range parts {
		for len(part) > 0 {
			count := len(part)
			if count > len(buffer) {
				count = len(buffer)
			}
			if _, err = io.ReadFull(f, buffer[:count]); err != nil {
				return false
			}
			if !bytes.Equal(buffer[:count], part[:count]) {
				return false
			}
			part = part[count:]
		}
	}
	var tail [1]byte
	n, err := f.Read(tail[:])
	return n == 0 && err == io.EOF
}
