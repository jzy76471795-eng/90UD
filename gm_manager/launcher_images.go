package main

import (
	"encoding/json"
	"fmt"
	"newadmin/internal/launcherruntime"
	"os"
	"path/filepath"
	"strings"
)

type launcherImageEntry struct {
	Name   string `json:"name"`
	SHA256 string `json:"sha256"`
}

func stageLauncherImages(template, stage string) ([]string, error) {
	source := filepath.Join(filepath.Dir(template), "Image")
	manifest := filepath.Join(source, "清单.json")
	if _, err := os.Stat(manifest); os.IsNotExist(err) {
		return nil, nil
	}
	if err := launcherNoLinks(manifest); err != nil {
		return nil, err
	}
	raw, err := readLauncherLimited(manifest, 64<<10)
	if err != nil {
		return nil, err
	}
	var entries []launcherImageEntry
	if err = json.Unmarshal(raw, &entries); err != nil || len(entries) > 64 {
		return nil, fmt.Errorf("登陆器图片清单无效")
	}
	if err = os.MkdirAll(filepath.Join(stage, "Image"), 0700); err != nil {
		return nil, err
	}
	names := []string{}
	seen := map[string]bool{}
	for _, entry := range entries {
		ext := strings.ToLower(filepath.Ext(entry.Name))
		lower := strings.ToLower(entry.Name)
		if entry.Name == "" || filepath.Base(entry.Name) != entry.Name || strings.ContainsAny(entry.Name, "/\\:\x00") || seen[lower] || !(ext == ".png" || ext == ".jpg" || ext == ".jpeg" || ext == ".ico") {
			return nil, fmt.Errorf("图片清单包含无效名称")
		}
		seen[lower] = true
		pin, err := launcherPin(entry.SHA256)
		if err != nil {
			return nil, err
		}
		input := filepath.Join(source, entry.Name)
		if err = launcherNoLinks(input); err != nil {
			return nil, err
		}
		info, err := os.Stat(input)
		if err != nil || !info.Mode().IsRegular() || info.Size() > 64<<20 {
			return nil, fmt.Errorf("图片不可读或超过大小限制：%s", entry.Name)
		}
		output := filepath.Join(stage, "Image", entry.Name)
		if err = copyOutputFile(input, output); err != nil {
			return nil, err
		}
		sum, err := sha256File(output)
		if err != nil || sum != pin {
			return nil, fmt.Errorf("登陆器图片校验失败：%s", entry.Name)
		}
		names = append(names, entry.Name)
	}
	return names, nil
}

// Add missing defaults only. Existing client artwork is owned by the operator.
// A later EXE commit failure removes only the exact new files from this call.
func installLauncherImages(stage, root string, names []string) (func(), error) {
	noop := func() {}
	if len(names) == 0 {
		return noop, nil
	}
	targetRoot := filepath.Join(root, "Image")
	if err := launcherruntime.CheckPath(targetRoot); err != nil {
		return noop, err
	}
	_, statErr := os.Stat(targetRoot)
	createdDir := os.IsNotExist(statErr)
	if err := os.MkdirAll(targetRoot, 0700); err != nil {
		return noop, err
	}
	added := map[string]string{}
	rollback := func() {
		for path, sum := range added {
			if current, err := sha256File(path); err == nil && current == sum {
				_ = os.Remove(path)
			}
		}
		if createdDir {
			_ = os.Remove(targetRoot)
		}
	}
	for _, name := range names {
		target := filepath.Join(targetRoot, name)
		if err := launcherruntime.CheckPath(target); err != nil {
			rollback()
			return noop, err
		}
		if info, err := os.Lstat(target); err == nil {
			if !info.Mode().IsRegular() {
				rollback()
				return noop, fmt.Errorf("Image 中同名项目不是文件")
			}
			continue
		} else if !os.IsNotExist(err) {
			rollback()
			return noop, err
		}
		input := filepath.Join(stage, "Image", name)
		sum, err := sha256File(input)
		if err != nil {
			rollback()
			return noop, err
		}
		if err = commitLauncherDirectory(input, target); err != nil {
			rollback()
			return noop, err
		}
		added[target] = sum
	}
	return rollback, nil
}
