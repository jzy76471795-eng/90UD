//go:build windows

package main

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
)

func checkRuntimeArchive(path string) error {
	archive, err := zip.OpenReader(path)
	if err != nil {
		return err
	}
	defer archive.Close()
	entries := map[string]*zip.File{}
	for _, entry := range archive.File {
		entries[entry.Name] = entry
	}
	manifestEntry := entries["manifest.json"]
	if manifestEntry == nil {
		return fmt.Errorf("运行包缺少校验清单")
	}
	reader, err := manifestEntry.Open()
	if err != nil {
		return err
	}
	var manifest map[string]string
	err = json.NewDecoder(io.LimitReader(reader, 8192)).Decode(&manifest)
	reader.Close()
	if err != nil {
		return err
	}
	for _, name := range []string{"gmserver.exe", "go-manager.json", "tls/server-cert.pem", "tls/server-key.pem"} {
		entry := entries[name]
		if entry == nil || len(manifest[name]) != 64 {
			return fmt.Errorf("运行包缺少必要文件")
		}
		r, e := entry.Open()
		if e != nil {
			return e
		}
		hash := sha256.New()
		_, e = io.Copy(hash, io.LimitReader(r, 64<<20+1))
		r.Close()
		if e != nil || hex.EncodeToString(hash.Sum(nil)) != manifest[name] {
			return fmt.Errorf("运行包文件SHA256不一致")
		}
	}
	return nil
}
