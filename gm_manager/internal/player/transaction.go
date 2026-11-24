package player

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

func ID() string {
	b := make([]byte, 16)
	if _, e := rand.Read(b); e != nil {
		panic("系统随机源不可用")
	}
	return hex.EncodeToString(b)
}
func copyDurable(src, dst string) error {
	if e := NoLinks(src); e != nil {
		return e
	}
	if e := NoLinks(dst); e != nil {
		return e
	}
	in, e := os.Open(src)
	if e != nil {
		return e
	}
	defer in.Close()
	s, e := in.Stat()
	if e != nil || !s.Mode().IsRegular() {
		return errors.New("源文件无效")
	}
	out, e := os.OpenFile(dst, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return e
	}
	_, e = io.Copy(out, in)
	if e == nil {
		e = out.Sync()
	}
	ce := out.Close()
	if e == nil {
		e = ce
	}
	return e
}
func currentHash(path string) (string, error) {
	s, e := os.Lstat(path)
	if os.IsNotExist(e) {
		return "", nil
	}
	if e != nil {
		return "", e
	}
	if !s.Mode().IsRegular() {
		return "", errors.New("更新目标不是普通文件")
	}
	return FileHash(path)
}

type TransactionEntry struct {
	Path   string `json:"path"`
	Before string `json:"before"`
	After  string `json:"after"`
}
type transactionJournal struct {
	Version int                `json:"version"`
	Root    string             `json:"root"`
	Job     string             `json:"job"`
	Files   []TransactionEntry `json:"files"`
}
type Transaction struct {
	Root, Cache  string
	Running      func() bool
	AfterReplace func(int) error
	Check        func() error
}

func (t Transaction) pending() string {
	key := shaText(strings.ToUpper(filepath.Clean(t.Root)))
	return filepath.Join(t.Cache, key+".transaction.json")
}
func (t Transaction) withLock(fn func() error) error {
	if e := NoLinks(t.Root); e != nil {
		return e
	}
	if e := NoLinks(t.Cache); e != nil {
		return e
	}
	if e := os.MkdirAll(t.Cache, 0700); e != nil {
		return e
	}
	close, e := LockFile(t.pending() + ".lock")
	if e != nil {
		return errors.New("客户端正在由另一个登录器更新")
	}
	defer close()
	return fn()
}
func (t Transaction) Recover() error { return t.withLock(t.recover) }
func (t Transaction) recover() error {
	p := t.pending()
	if e := NoLinks(p); e != nil {
		return e
	}
	raw, e := readBounded(p, 16<<20)
	if os.IsNotExist(e) {
		return nil
	}
	if e != nil {
		return e
	}
	var j transactionJournal
	if StrictJSON(raw, &j) != nil || j.Version != 1 || !strings.EqualFold(j.Root, t.Root) || !strings.EqualFold(filepath.Dir(j.Job), t.Cache) || !strings.HasPrefix(filepath.Base(j.Job), "transaction-") || len(j.Files) > 100000 {
		return errors.New("更新恢复记录无效，请保留缓存")
	}
	if t.Running != nil && t.Running() {
		return errors.New("客户端有未完成更新，请先退出游戏")
	}
	seen := map[string]bool{}
	for _, row := range j.Files {
		target, e := Within(t.Root, row.Path)
		if e != nil {
			return e
		}
		k := strings.ToLower(row.Path)
		if seen[k] || !hashPattern.MatchString(row.After) || (row.Before != "" && !hashPattern.MatchString(row.Before)) {
			return errors.New("更新恢复清单无效")
		}
		seen[k] = true
		cur, e := currentHash(target)
		if e != nil {
			return e
		}
		if cur != row.Before && cur != row.After {
			return errors.New("文件被外部修改，保留备份并停止恢复")
		}
		if row.Before != "" {
			backup, e := Within(filepath.Join(j.Job, "previous"), row.Path)
			if e != nil {
				return e
			}
			h, e := currentHash(backup)
			if e != nil || h != row.Before {
				return errors.New("更新备份校验失败")
			}
		}
	}
	for i := len(j.Files) - 1; i >= 0; i-- {
		row := j.Files[i]
		target, e := Within(t.Root, row.Path)
		if e != nil {
			return e
		}
		cur, e := currentHash(target)
		if e != nil {
			return e
		}
		if cur == row.Before {
			continue
		}
		if cur != row.After {
			return errors.New("恢复期间发生外部修改")
		}
		if row.Before == "" {
			if e = os.Remove(target); e != nil {
				return e
			}
		} else {
			backup, e := Within(filepath.Join(j.Job, "previous"), row.Path)
			if e != nil {
				return e
			}
			temp := filepath.Join(filepath.Dir(target), ".go90-restore-"+ID())
			if e = copyDurable(backup, temp); e != nil {
				return e
			}
			if e = AtomicReplace(temp, target); e != nil {
				return e
			}
			h, e := FileHash(target)
			if e != nil || h != row.Before {
				return errors.New("恢复后摘要不符")
			}
		}
	}
	return os.Remove(p)
}
func (t Transaction) Apply(files map[string]string, validate func() error) error {
	return t.withLock(func() error {
		if e := t.recover(); e != nil {
			return e
		}
		if t.Running != nil && t.Running() {
			return errors.New("请先退出游戏再更新")
		}
		job := filepath.Join(t.Cache, "transaction-"+ID())
		if e := os.MkdirAll(job, 0700); e != nil {
			return e
		}
		j := transactionJournal{Version: 1, Root: t.Root, Job: job}
		seen := map[string]bool{}
		for name, source := range files {
			if t.Check != nil {
				if e := t.Check(); e != nil {
					return e
				}
			}
			target, e := Within(t.Root, name)
			if e != nil {
				return e
			}
			k := strings.ToLower(name)
			if seen[k] {
				return errors.New("更新文件重名")
			}
			seen[k] = true
			before, e := currentHash(target)
			if e != nil {
				return e
			}
			after, e := FileHash(source)
			if e != nil {
				return e
			}
			next, e := Within(filepath.Join(job, "new"), name)
			if e != nil {
				return e
			}
			if e = os.MkdirAll(filepath.Dir(next), 0700); e != nil {
				return e
			}
			if e = copyDurable(source, next); e != nil {
				return e
			}
			h, e := FileHash(next)
			if e != nil || h != after {
				return errors.New("候选文件在备份时发生变化")
			}
			if before != "" {
				previous, e := Within(filepath.Join(job, "previous"), name)
				if e != nil {
					return e
				}
				if e = os.MkdirAll(filepath.Dir(previous), 0700); e != nil {
					return e
				}
				if e = copyDurable(target, previous); e != nil {
					return e
				}
				h, e := FileHash(previous)
				if e != nil || h != before {
					return errors.New("旧文件备份期间发生变化")
				}
			}
			j.Files = append(j.Files, TransactionEntry{name, before, after})
		}
		raw, e := json.Marshal(j)
		if e != nil {
			return e
		}
		tempJournal := filepath.Join(job, "journal.json")
		if e = Durable(tempJournal, raw); e != nil {
			return e
		}
		if e = AtomicMoveNew(tempJournal, t.pending()); e != nil {
			return e
		}
		apply := func() error {
			for i, row := range j.Files {
				if t.Check != nil {
					if e := t.Check(); e != nil {
						return e
					}
				}
				if t.Running != nil && t.Running() {
					return errors.New("游戏已启动，停止更新")
				}
				target, e := Within(t.Root, row.Path)
				if e != nil {
					return e
				}
				cur, e := currentHash(target)
				if e != nil || cur != row.Before {
					return errors.New("更新目标发生变化，未覆盖")
				}
				if e = os.MkdirAll(filepath.Dir(target), 0700); e != nil {
					return e
				}
				temp := filepath.Join(filepath.Dir(target), ".go90-update-"+ID())
				source, e := Within(filepath.Join(job, "new"), row.Path)
				if e != nil {
					return e
				}
				if e = copyDurable(source, temp); e != nil {
					return e
				}
				if e = AtomicReplace(temp, target); e != nil {
					return e
				}
				h, e := FileHash(target)
				if e != nil || h != row.After {
					return errors.New("安装后文件摘要不符")
				}
				if t.AfterReplace != nil {
					if e = t.AfterReplace(i + 1); e != nil {
						return e
					}
				}
			}
			if t.Check != nil {
				if e := t.Check(); e != nil {
					return e
				}
			}
			if validate != nil {
				return validate()
			}
			return nil
		}
		if e = apply(); e != nil {
			if re := t.recover(); re != nil {
				return fmt.Errorf("更新失败，恢复未完成，请保留备份：%w", re)
			}
			return e
		}
		return os.Remove(t.pending())
	})
}
func readBounded(path string, max int64) ([]byte, error) {
	f, e := os.Open(path)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	b, e := io.ReadAll(io.LimitReader(f, max+1))
	if int64(len(b)) > max {
		return nil, errors.New("文件超过限制")
	}
	return b, e
}
