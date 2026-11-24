package main

import (
	"fmt"
	"newadmin/internal/launcherruntime"
	"os"
	"path/filepath"
)

func stageLauncherRuntime(template, stagedExecutable, stage string) (*launcherruntime.Manifest, error) {
	f, err := os.Open(stagedExecutable)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	m, err := launcherruntime.Read(f, info.Size())
	if err != nil || m == nil {
		return m, err
	}
	input, err := launcherruntime.Verify(filepath.Dir(template), m)
	if err != nil {
		return nil, err
	}
	for _, row := range m.Files {
		output := filepath.Join(stage, filepath.FromSlash(m.Directory), filepath.FromSlash(row.Path))
		if err = os.MkdirAll(filepath.Dir(output), 0700); err != nil {
			return nil, err
		}
		if err = copyOutputFile(filepath.Join(input, filepath.FromSlash(row.Path)), output); err != nil {
			return nil, err
		}
	}
	if _, err = launcherruntime.Verify(stage, m); err != nil {
		return nil, err
	}
	return m, nil
}

// Runtime versions are immutable. Reuse a matching complete version, or commit
// a new directory atomically; never touch runtime/client or runtime/server.
func installLauncherRuntime(stage, root string, m *launcherruntime.Manifest) (func(), error) {
	noop := func() {}
	if m == nil {
		return noop, nil
	}
	if err := m.Validate(); err != nil {
		return noop, err
	}
	target := filepath.Join(root, filepath.FromSlash(m.Directory))
	if err := launcherruntime.CheckPath(target); err != nil {
		return noop, err
	}
	if _, err := os.Stat(target); err == nil {
		_, err = launcherruntime.Verify(root, m)
		return noop, err
	} else if !os.IsNotExist(err) {
		return noop, err
	}
	parent := filepath.Dir(target)
	if err := os.MkdirAll(parent, 0700); err != nil {
		return noop, err
	}
	if err := launcherruntime.CheckPath(parent); err != nil {
		return noop, err
	}
	if err := commitLauncherDirectory(filepath.Join(stage, filepath.FromSlash(m.Directory)), target); err != nil {
		return noop, fmt.Errorf("保存配套运行库失败：%w", err)
	}
	rollback := func() {
		// Delete only the exact verified files installed by this transaction.
		if _, err := launcherruntime.Verify(root, m); err != nil {
			return
		}
		for _, row := range m.Files {
			_ = os.Remove(filepath.Join(target, filepath.FromSlash(row.Path)))
		}
		dirs := []string{}
		_ = filepath.WalkDir(target, func(p string, d os.DirEntry, e error) error {
			if e == nil && d.IsDir() {
				dirs = append(dirs, p)
			}
			return e
		})
		for i := len(dirs) - 1; i >= 0; i-- {
			_ = os.Remove(dirs[i])
		}
	}
	return rollback, nil
}
