//go:build windows

package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"github.com/lxn/walk"
	"newadmin/internal/player"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"time"
)

type slot struct {
	Account string
	Session player.Session
	Game    *player.Game
	Lease   *player.RouteLease
}
type character struct {
	ID    int64  `json:"id"`
	Name  string `json:"name"`
	Level int    `json:"level"`
}
type launcherUI struct {
	mw                       *walk.MainWindow
	view                     *skinView
	prefs                    skinPreferences
	tray                     *walk.NotifyIcon
	forceExit                bool
	status, server, multi    *walk.Label
	path                     *walk.LineEdit
	remember                 *walk.CheckBox
	accounts, passwords      [2]*walk.LineEdit
	loginButtons             [2]*walk.PushButton
	slotState                [2]*walk.Label
	notice, history, result  *walk.TextEdit
	cdkAccount, cdkCharacter *walk.ComboBox
	code                     *walk.LineEdit
	cfg                      player.Config
	api                      *player.API
	pool                     player.RoutePool
	slots                    [2]*slot
	characters               []character
	cdkSlots                 []int
	ctx                      context.Context
	stop                     context.CancelFunc
	cancel                   context.CancelFunc
	busy, closing, polling   bool
	offline                  bool
	closed                   atomic.Bool
	cursor                   int64
	cursorToken              string
	controls                 []*walk.PushButton
}

func main() {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	inspect := flag.Bool("inspect", false, "仅校验内置公开配置并输出结果，不连接服务器")
	preview := flag.Bool("offline-preview", false, "仅预览界面，不发送请求或启动游戏")
	flag.Parse()
	exe, e := os.Executable()
	if e != nil {
		return
	}
	cfg, configErr := player.LoadConfig(exe)
	if *inspect {
		if configErr != nil {
			fmt.Fprintln(os.Stderr, configErr)
			os.Exit(2)
		}
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"language": "go", "version": player.Version, "configured": true, "server_id": cfg.ServerID, "login_url": cfg.LoginURL, "display_name": cfg.DisplayName, "architecture": runtime.GOARCH, "dotnet_required": false, "server_contacted": false})
		return
	}
	ctx, stop := context.WithCancel(context.Background())
	defer stop()
	u := &launcherUI{cfg: cfg, ctx: ctx, stop: stop, offline: *preview, cursor: -1}
	if configErr == nil {
		u.api, configErr = player.NewAPI(cfg)
		if u.api != nil {
			defer u.api.Close()
		}
	}
	if e = u.create(exe, configErr); e != nil {
		walk.MsgBox(nil, "Go登录器", e.Error(), walk.MsgBoxIconError)
		return
	}
	if u.api != nil && !u.offline {
		u.restore()
		u.check()
		go u.pollLoop()
	}
	u.mw.Run()
	u.closed.Store(true)
	stop()
}
func (u *launcherUI) sync(fn func()) {
	if !u.closed.Load() {
		u.mw.Synchronize(func() {
			if !u.closed.Load() {
				fn()
			}
		})
	}
}
func (u *launcherUI) message(text string) {
	u.status.SetText(text)
	if u.view != nil {
		u.view.statusAt = time.Now()
		if u.view.canvas != nil {
			u.view.canvas.Invalidate()
		}
		if u.view.dialogStatus != nil {
			u.view.dialogStatus.SetText(text)
		}
	}
}
func (u *launcherUI) progress(text string) { u.sync(func() { u.message(text) }) }
func (u *launcherUI) job(text string, fn func(context.Context) (func(), error)) {
	if u.busy {
		return
	}
	if u.api == nil || u.offline {
		u.message("离线预览：请先通过GM后台生成带公开配置的Go登录器")
		return
	}
	u.busy = true
	ctx, cancel := context.WithCancel(u.ctx)
	u.cancel = cancel
	u.refresh()
	u.message(text)
	go func() {
		done, e := fn(ctx)
		cancel()
		u.sync(func() {
			u.busy = false
			u.cancel = nil
			if e != nil {
				u.message(e.Error())
			} else if done != nil {
				done()
			}
			u.refresh()
			if u.closing {
				u.mw.Close()
			}
		})
	}()
}
func (u *launcherUI) refresh() {
	for i := range u.loginButtons {
		u.loginButtons[i].SetEnabled(!u.busy && u.slots[i] == nil && u.api != nil && !u.offline)
		u.accounts[i].SetEnabled(!u.busy && u.slots[i] == nil)
		u.passwords[i].SetEnabled(!u.busy && u.slots[i] == nil)
	}
	u.path.SetEnabled(!u.busy)
	u.remember.SetEnabled(!u.busy)
	if u.cdkAccount != nil {
		u.cdkAccount.SetEnabled(!u.busy)
		u.cdkCharacter.SetEnabled(!u.busy)
		u.code.SetEnabled(!u.busy)
	}
	for _, b := range u.controls {
		b.SetEnabled(!u.busy && u.api != nil && !u.offline)
	}
	if u.view != nil && u.view.canvas != nil {
		u.view.layout()
	}
}
func (u *launcherUI) restore() {
	if rows, e := u.cfg.ReadLogin(); e == nil {
		for _, v := range rows {
			u.accounts[v.Slot].SetText(v.Account)
			u.passwords[v.Slot].SetText(v.Password)
		}
		u.remember.SetChecked(len(rows) > 0)
	} else {
		u.message("已保存密码不可读取，请重新输入")
	}
	path := filepath.Join(u.cfg.DataDirectory(), "client-location.json")
	b, e := os.ReadFile(path)
	if e == nil && len(b) < 32768 {
		var v struct{ Path string }
		if json.Unmarshal(b, &v) == nil && player.NoLinks(v.Path) == nil && v.Path != "" {
			u.path.SetText(v.Path)
		}
	}
	u.refresh()
}
func (u *launcherUI) saveLocation() {
	if u.api == nil {
		return
	}
	dir := u.cfg.DataDirectory()
	if player.NoLinks(dir) != nil {
		return
	}
	if os.MkdirAll(dir, 0700) != nil {
		return
	}
	b, _ := json.Marshal(struct{ Path string }{u.path.Text()})
	temp := filepath.Join(dir, ".location-"+player.ID())
	if player.Durable(temp, b) == nil {
		_ = player.AtomicReplace(temp, filepath.Join(dir, "client-location.json"))
	}
}
func (u *launcherUI) check() {
	root := u.path.Text()
	exe, _ := os.Executable()
	u.job("正在检查服务和资源…", func(ctx context.Context) (func(), error) {
		stage, e := u.api.Meta(ctx)
		if e != nil {
			return nil, e
		}
		if e = u.api.EnsureResources(ctx, root, exe, stage, u.progress); e != nil {
			return nil, e
		}
		enabled, e := u.api.MultiOpen(ctx)
		if e != nil && stage.NativeTunnelVersion == 2 {
			return nil, e
		}
		return func() {
			u.server.SetText("服务器已就绪：" + u.cfg.LoginURL)
			u.setMulti(enabled)
			u.message("客户端检查通过，可以登录。")
			u.saveLocation()
		}, nil
	})
}
func (u *launcherUI) setMulti(enabled bool) {
	if u.view != nil {
		u.view.multiAllowed = enabled
		if !enabled && u.view.dual {
			u.view.dual = false
			u.view.layout()
		}
	}
	s := "服务器当前关闭双开；已经运行的游戏不受影响。"
	if enabled {
		s = "服务器允许双开，最多同时运行两个账号。"
	}
	u.multi.SetText(s)
}
func (u *launcherUI) login(index int) {
	account := strings.TrimSpace(u.accounts[index].Text())
	password := u.passwords[index].Text()
	root := u.path.Text()
	remember := u.remember.Checked()
	if account == "" || password == "" {
		u.message("请填写账号和密码")
		return
	}
	exe, _ := os.Executable()
	u.job("正在检查客户端和账号…", func(ctx context.Context) (func(), error) {
		stage, e := u.api.Meta(ctx)
		if e != nil {
			return nil, e
		}
		if stage.NativeTunnelVersion == 2 {
			enabled, e := u.api.MultiOpen(ctx)
			if e != nil {
				return nil, e
			}
			if index == 1 && !enabled {
				return nil, errors.New("服务器当前关闭双开")
			}
		}
		if e = u.api.EnsureResources(ctx, root, exe, stage, u.progress); e != nil {
			return nil, e
		}
		device, e := player.DeviceID()
		if e != nil {
			return nil, e
		}
		session, e := u.api.Login(ctx, account, password, device, stage)
		if e != nil {
			return nil, e
		}
		keep := false
		defer func() {
			if !keep {
				c, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				_ = u.api.Logout(c, session)
			}
		}()
		plan, e := player.ValidateClient(root, session.Stage)
		if e != nil {
			return nil, e
		}
		key := ""
		if session.Stage.PvfProfile == "keyset96" {
			key, e = u.api.Key(ctx, session)
			if e != nil {
				return nil, e
			}
		}
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		u.progress("账号认证成功，正在启动游戏…")
		u.pool.LaunchGate.Lock()
		defer u.pool.LaunchGate.Unlock()
		lease, e := u.pool.Acquire(session)
		if e != nil {
			return nil, e
		}
		game, e := player.Launch(plan, session, key, u.cfg.DataDirectory(), lease.Bind)
		key = ""
		if e != nil {
			lease.Close()
			return nil, e
		}
		keep = true
		var saveErr error
		if remember {
			saved, e := u.cfg.ReadLogin()
			if e != nil {
				saved = nil
			}
			rows := []player.SavedLogin{}
			for _, v := range saved {
				if v.Slot != index {
					rows = append(rows, v)
				}
			}
			rows = append(rows, player.SavedLogin{Slot: index, Account: account, Password: password})
			saveErr = u.cfg.SaveLogin(rows)
		} else {
			saveErr = u.cfg.SaveLogin(nil)
		}
		return func() {
			s := &slot{Account: account, Session: session, Game: game, Lease: lease}
			u.slots[index] = s
			u.slotState[index].SetText(fmt.Sprintf("%s · PID %d", account, game.PID))
			u.passwords[index].SetText("")
			u.refreshAccounts()
			u.message("游戏已启动")
			if saveErr != nil {
				u.message("游戏已启动，但密码保存失败")
			}
			u.saveLocation()
			go func() {
				<-game.Done()
				lease.Close()
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				_ = u.api.Logout(ctx, session)
				u.sync(func() {
					if u.slots[index] == s {
						u.slots[index] = nil
						u.slotState[index].SetText("游戏已退出")
						u.refreshAccounts()
						u.refresh()
						if u.prefs.ExitGamePopup {
							u.restoreWindow()
						}
					}
				})
			}()
			u.minimize()
		}, nil
	})
}
func (u *launcherUI) stopGame(i int) {
	s := u.slots[i]
	if s == nil || !s.Game.Alive() {
		u.message("此账号没有由本登录器启动的游戏")
		return
	}
	if walk.MsgBox(u.mw, "结束游戏", "结束此账号的游戏进程？", walk.MsgBoxYesNo|walk.MsgBoxIconQuestion) == walk.DlgCmdYes {
		if e := s.Game.Stop(); e != nil {
			u.message(e.Error())
		}
	}
}

func (u *launcherUI) minimize() { overlayUser32.NewProc("ShowWindow").Call(uintptr(u.mw.Handle()), 6) }
