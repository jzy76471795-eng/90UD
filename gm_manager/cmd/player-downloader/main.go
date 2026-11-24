package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"

	"github.com/lxn/walk"
	d "github.com/lxn/walk/declarative"
	"newadmin/internal/downloader"
)

func main() {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	exe, err := os.Executable()
	if err != nil {
		return
	}
	cfg, err := downloader.LoadExecutable(exe)
	if err != nil {
		walk.MsgBox(nil, "下载器", err.Error(), walk.MsgBoxIconError)
		return
	}
	var mw *walk.MainWindow
	var folder *walk.LineEdit
	var status *walk.Label
	var progress *walk.ProgressBar
	var start, stop, choose, open, startLauncher *walk.PushButton
	var cancel context.CancelFunc
	busy, pendingClose := false, false
	var result downloader.Result
	defaultFolder := filepath.Join(filepath.Dir(exe), "游戏下载")
	launch := func() {
		if result.Launcher == "" {
			return
		}
		cmd := exec.Command(result.Launcher)
		cmd.Dir = filepath.Dir(result.Launcher)
		if err := cmd.Start(); err != nil {
			walk.MsgBox(mw, "启动失败", "客户端下载完成，但登录器未能启动，请在下载目录中手动打开。", walk.MsgBoxIconWarning)
		} else {
			go cmd.Wait()
		}
	}
	err = d.MainWindow{AssignTo: &mw, Title: cfg.Name, MinSize: d.Size{Width: 640, Height: 300}, Size: d.Size{Width: 720, Height: 340}, Layout: d.VBox{Margins: d.Margins{Left: 20, Top: 20, Right: 20, Bottom: 20}, Spacing: 12}, Children: []d.Widget{
		d.Label{Text: cfg.Name + " — 完整客户端下载"},
		d.Label{Text: "中断后点击重试可继续下载，完成后可打开目录或启动登录器。"},
		d.Composite{Layout: d.HBox{}, Children: []d.Widget{d.Label{Text: "保存到："}, d.LineEdit{AssignTo: &folder, Text: defaultFolder}, d.PushButton{AssignTo: &choose, Text: "选择目录", OnClicked: func() {
			dialog := &walk.FileDialog{Title: "选择下载目录", FilePath: folder.Text()}
			if ok, _ := dialog.ShowBrowseFolder(mw); ok {
				folder.SetText(dialog.FilePath)
			}
		}}}},
		d.ProgressBar{AssignTo: &progress, MinValue: 0, MaxValue: 1000},
		d.Label{AssignTo: &status, Text: "准备就绪", MinSize: d.Size{Height: 40}, EllipsisMode: d.EllipsisEnd},
		d.Composite{Layout: d.HBox{}, Children: []d.Widget{
			d.PushButton{AssignTo: &start, Text: "开始下载", OnClicked: func() {
				if busy {
					return
				}
				target := folder.Text()
				if target == "" {
					status.SetText("请选择下载目录")
					return
				}
				ctx, c := context.WithCancel(context.Background())
				cancel = c
				busy = true
				start.SetEnabled(false)
				folder.SetEnabled(false)
				choose.SetEnabled(false)
				stop.SetEnabled(true)
				open.SetEnabled(false)
				startLauncher.SetEnabled(false)
				status.SetText("正在准备下载…")
				progress.SetValue(0)
				go func() {
					done, e := (downloader.Engine{}).Run(ctx, cfg, target, func(p downloader.Progress) {
						mw.Synchronize(func() {
							text := p.Stage
							if p.Total > 0 {
								progress.SetValue(int(p.Done * 1000 / p.Total))
								text += fmt.Sprintf("  %.1f / %.1f MB", float64(p.Done)/(1<<20), float64(p.Total)/(1<<20))
							} else if p.Done > 0 {
								text += fmt.Sprintf("  %.1f MB", float64(p.Done)/(1<<20))
							}
							status.SetText(text)
						})
					})
					c()
					mw.Synchronize(func() {
						busy = false
						cancel = nil
						if pendingClose {
							mw.Close()
							return
						}
						folder.SetEnabled(true)
						choose.SetEnabled(true)
						stop.SetEnabled(false)
						if e != nil {
							start.SetEnabled(true)
							start.SetText("重试 / 继续下载")
							if errors.Is(e, context.Canceled) {
								status.SetText("已暂停；再次点击可继续下载。")
							} else {
								status.SetText(e.Error())
								status.SetToolTipText(e.Error())
							}
							return
						}
						result = done
						progress.SetValue(1000)
						status.SetText("下载完成。" + done.Directory)
						open.SetEnabled(true)
						startLauncher.SetEnabled(done.Launcher != "")
						if cfg.AutoStart {
							launch()
						}
					})
				}()
			}},
			d.PushButton{AssignTo: &stop, Text: "暂停", Enabled: false, OnClicked: func() {
				if cancel != nil {
					cancel()
					stop.SetEnabled(false)
					status.SetText("正在暂停…")
				}
			}},
			d.PushButton{AssignTo: &open, Text: "打开目录", Enabled: false, OnClicked: func() {
				if result.Directory != "" {
					cmd := exec.Command("explorer.exe", result.Directory)
					if cmd.Start() == nil {
						go cmd.Wait()
					}
				}
			}},
			d.PushButton{AssignTo: &startLauncher, Text: "启动登录器", Enabled: false, OnClicked: launch},
		}},
	}}.Create()
	if err != nil {
		walk.MsgBox(nil, "下载器", err.Error(), walk.MsgBoxIconError)
		return
	}
	mw.Closing().Attach(func(canceled *bool, _ walk.CloseReason) {
		if busy {
			*canceled = true
			pendingClose = true
			if cancel != nil {
				cancel()
			}
			status.SetText("正在停止下载并关闭…")
		}
	})
	mw.Run()
}
