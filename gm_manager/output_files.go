package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

func flatOutputStage(output, prefix string) (string, string, error) {
	if output == "" {
		return "", "", fmt.Errorf("请选择输出目录")
	}
	root, err := filepath.Abs(output)
	if err != nil {
		return "", "", err
	}
	if err = os.MkdirAll(root, 0700); err != nil {
		return "", "", err
	}
	if err = launcherNoLinks(root); err != nil {
		return "", "", err
	}
	stage, err := os.MkdirTemp(root, prefix)
	if err != nil {
		return "", "", err
	}
	if err = protectLauncherPath(stage, true); err != nil {
		os.RemoveAll(stage)
		return "", "", err
	}
	return root, stage, nil
}

func copyOutputFile(source, target string) (err error) {
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	output, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer func() { err = errors.Join(err, output.Close()) }()
	if err = protectLauncherPath(target, false); err != nil {
		return err
	}
	if _, err = io.Copy(output, input); err != nil {
		return err
	}
	return output.Sync()
}

// Only the named output files can be replaced. Old bytes are verified and kept
// on E: before any replacement; failure restores the files already committed.
func commitOutputFiles(stage, root string, names []string, backupRoot string) (backup string, err error) {
	if err = launcherNoLinks(root); err != nil {
		return
	}
	old := map[string]string{}
	newSHA := map[string]string{}
	for _, name := range names {
		if filepath.Base(name) != name || name == "." || name == "" {
			return "", fmt.Errorf("输出文件名无效")
		}
		source := filepath.Join(stage, name)
		if err = launcherNoLinks(source); err != nil {
			return
		}
		info, statErr := os.Stat(source)
		if statErr != nil || !info.Mode().IsRegular() {
			return "", fmt.Errorf("待提交的输出文件无效")
		}
		if newSHA[name], err = sha256File(source); err != nil {
			return
		}
		target := filepath.Join(root, name)
		info, statErr = os.Lstat(target)
		if os.IsNotExist(statErr) {
			old[name] = ""
			continue
		}
		if statErr != nil {
			return "", statErr
		}
		if !info.Mode().IsRegular() {
			return "", fmt.Errorf("同名目标不是普通文件：%s", target)
		}
		if err = launcherNoLinks(target); err != nil {
			return
		}
		if old[name], err = sha256File(target); err != nil {
			return
		}
	}
	for _, name := range names {
		if old[name] == "" || old[name] == newSHA[name] {
			continue
		}
		if backup == "" {
			if err = os.MkdirAll(backupRoot, 0700); err != nil {
				return
			}
			if err = launcherNoLinks(backupRoot); err != nil {
				return
			}
			if backup, err = os.MkdirTemp(backupRoot, "输出-"); err != nil {
				return
			}
			if err = protectLauncherPath(backup, true); err != nil {
				return
			}
		}
		if err = copyOutputFile(filepath.Join(root, name), filepath.Join(backup, name)); err != nil {
			return
		}
		var sum string
		if sum, err = sha256File(filepath.Join(backup, name)); err != nil {
			return
		}
		if sum != old[name] {
			return backup, fmt.Errorf("备份时目标文件变化，未覆盖：%s", name)
		}
	}
	committed := []string{}
	rollback := func(cause error) error {
		for i := len(committed) - 1; i >= 0; i-- {
			name := committed[i]
			target := filepath.Join(root, name)
			current, readErr := sha256File(target)
			if readErr != nil || current != newSHA[name] {
				cause = errors.Join(cause, fmt.Errorf("输出发生外部变化，未回滚 %s；备份位于 %s", target, backup))
				continue
			}
			if old[name] == "" {
				cause = errors.Join(cause, os.Remove(target))
				continue
			}
			restore := filepath.Join(stage, "restore-"+name)
			if restoreErr := copyOutputFile(filepath.Join(backup, name), restore); restoreErr != nil {
				cause = errors.Join(cause, restoreErr)
				continue
			}
			cause = errors.Join(cause, os.Rename(restore, target))
		}
		return fmt.Errorf("输出未完成，请检查文件占用；旧文件备份：%s: %w", backup, cause)
	}
	for _, name := range names {
		if old[name] == newSHA[name] {
			continue
		}
		target := filepath.Join(root, name)
		if old[name] == "" {
			err = commitLauncherDirectory(filepath.Join(stage, name), target) // no replacement if another writer created it
		} else {
			var current string
			current, err = sha256File(target)
			if err == nil && current != old[name] {
				err = fmt.Errorf("目标文件已变化：%s", target)
			}
			if err == nil {
				err = os.Rename(filepath.Join(stage, name), target)
			}
		}
		if err != nil {
			return backup, rollback(err)
		}
		committed = append(committed, name)
	}
	return backup, nil
}

func commitFlatOutput(stage, root string, names []string) (string, error) {
	return commitOutputFiles(stage, root, names, `E:\codex\GM输出备份`)
}
