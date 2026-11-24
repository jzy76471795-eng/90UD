//go:build windows

package main

import (
	"context"
	"errors"
	"github.com/lxn/walk"
	d "github.com/lxn/walk/declarative"
	"image"
	"newadmin/internal/player"
	"strings"
)

func accountField(label string, field d.Widget) d.Composite {
	return d.Composite{Layout: d.VBox{Margins: d.Margins{Top: 10}, Spacing: 5}, Children: []d.Widget{d.Label{Text: label}, field}}
}
func (u *launcherUI) accountDialog(recovery bool) {
	var dialog *walk.Dialog
	var account, password, confirm, answer, code *walk.LineEdit
	var question *walk.ComboBox
	var status *walk.Label
	var codePanel *walk.Composite
	var submit *walk.CustomWidget
	title, width, height := "注册账号", 440, 540
	if recovery {
		title, width, height = "找回密码", 480, 560
	}
	line := func(to **walk.LineEdit, secret bool, max int) d.LineEdit {
		return d.LineEdit{AssignTo: to, PasswordMode: secret, MaxLength: max, MinSize: d.Size{Height: 32}, MaxSize: d.Size{Height: 32}, Background: d.SolidColorBrush{Color: walk.RGB(20, 21, 25)}}
	}
	children := []d.Widget{d.Label{Text: title, Font: d.Font{PointSize: 16}}}
	if recovery {
		children = append(children, d.Label{Text: "请输入账号，并填写注册时设置的密保问题和答案。"})
	}
	children = append(children, accountField("账号", line(&account, false, 64)))
	if !recovery {
		children = append(children, accountField("密码（8–128 位）", line(&password, true, 128)), accountField("确认密码", line(&confirm, true, 128)))
	}
	children = append(children, accountField("密保问题", d.ComboBox{AssignTo: &question, Model: questions, CurrentIndex: -1, MinSize: d.Size{Height: 32}, MaxSize: d.Size{Height: 32}}), accountField("密保答案（2–100 个字符，请牢记）", line(&answer, true, 100)))
	if recovery {
		children = append(children, accountField("新密码（8–128 位）", line(&password, true, 128)), accountField("确认新密码", line(&confirm, true, 128)))
	} else {
		panel := accountField("注册码", line(&code, false, 256))
		panel.AssignTo = &codePanel
		panel.Visible = false
		children = append(children, panel)
	}
	completed, policyReady, codeRequired, registrationClosed := false, false, false, false
	var readPolicy func()
	readPolicy = func() {
		u.job("正在读取注册规则…", func(ctx context.Context) (func(), error) {
			var policy struct {
				Closed       bool `json:"closed"`
				CodeRequired bool `json:"code_required"`
			}
			if e := u.api.Request(ctx, "GET", "/launcher/v1/registration", "", nil, &policy); e != nil {
				return nil, e
			}
			return func() {
				policyReady = true
				codeRequired = policy.CodeRequired
				registrationClosed = policy.Closed
				codePanel.SetVisible(codeRequired)
				if codeRequired {
					_ = dialog.SetClientSize(walk.Size{Width: width - 16, Height: height + 64})
				}
				if registrationClosed {
					status.SetText("服务器已关闭注册。")
					submit.SetEnabled(false)
				} else {
					status.SetText("")
				}
			}, nil
		})
	}
	caption := "注册账号"
	if recovery {
		caption = "验证密保并重设密码"
	}
	submitAction := func() {
		if u.busy {
			return
		}
		if completed {
			dialog.Accept()
			return
		}
		if !recovery && !policyReady {
			readPolicy()
			return
		}
		if registrationClosed {
			return
		}
		a, p, ans, q := strings.TrimSpace(account.Text()), password.Text(), answer.Text(), question.CurrentIndex()
		if !accountValid(a) || !validPassword(p) || p != confirm.Text() || !validSecurity(q, ans) {
			u.message("请检查账号、两次密码及密保答案")
			return
		}
		body := map[string]string{"account": a, "security_question": questionIDs[q], "security_answer": ans}
		path := "/launcher/v1/accounts"
		if recovery {
			path += "/security-recovery"
			body["new_password"] = p
		} else {
			body["password"] = p
			body["registration_code"] = strings.TrimSpace(code.Text())
			if codeRequired && body["registration_code"] == "" {
				u.message("请填写注册码")
				return
			}
		}
		u.job("正在处理，请勿重复提交…", func(ctx context.Context) (func(), error) {
			var result struct {
				Registered bool   `json:"registered"`
				Account    string `json:"account"`
			}
			if e := u.api.Request(ctx, "POST", path, "", body, &result); e != nil {
				return nil, e
			}
			if result.Account == "" || (!recovery && !result.Registered) {
				return nil, errors.New("结果尚未确认，请使用该账号登录核对")
			}
			return func() {
				completed = true
				password.SetText("")
				confirm.SetText("")
				answer.SetText("")
				for _, w := range []walk.Widget{account, password, confirm, answer, question} {
					w.SetEnabled(false)
				}
				if code != nil {
					code.SetEnabled(false)
				}
				caption = "返回登录"
				submit.Invalidate()
				status.SetTextColor(walk.RGB(144, 238, 144))
				if recovery {
					status.SetText("密码已重设，请使用新密码登录。")
					if rows, e := u.cfg.ReadLogin(); e == nil {
						kept := []player.SavedLogin{}
						for _, row := range rows {
							if !strings.EqualFold(row.Account, result.Account) {
								kept = append(kept, row)
							}
						}
						_ = u.cfg.SaveLogin(kept)
					}
					for i, edit := range u.accounts {
						if strings.EqualFold(edit.Text(), result.Account) {
							u.passwords[i].SetText("")
						}
					}
				} else {
					status.SetText("注册成功。请牢记账号和密保答案，以后仅通过密保问题重设密码。")
				}
				u.accounts[0].SetText(result.Account)
			}, nil
		})
	}
	children = append(children, d.Label{AssignTo: &status, MinSize: d.Size{Height: 34}}, d.CustomWidget{AssignTo: &submit, MinSize: d.Size{Height: 36}, MaxSize: d.Size{Height: 36}, PaintMode: d.PaintBuffered, Paint: func(c *walk.Canvas, b walk.Rectangle) error {
		u.view.button(c, "accountSubmit", caption, image.Rect(0, 0, b.Width, b.Height), 0, submit != nil && submit.Enabled() && !u.busy)
		return nil
	}, OnMouseDown: func(_, _ int, button walk.MouseButton) {
		if button == walk.LeftButton {
			submitAction()
		}
	}, OnKeyDown: func(key walk.Key) {
		if key == walk.KeyReturn || key == walk.KeySpace {
			submitAction()
		}
	}})

	err := (d.Dialog{AssignTo: &dialog, Title: title, Size: d.Size{Width: width, Height: height}, FixedSize: true, Font: d.Font{Family: "Microsoft YaHei UI", PointSize: 10}, Layout: d.VBox{Margins: d.Margins{Left: 28, Top: 28, Right: 28, Bottom: 28}, Spacing: 5}, Children: children}).Create(u.mw)
	if err != nil {
		u.message(err.Error())
		return
	}
	defer dialog.Dispose()
	themeSkinContainer(dialog)
	status.SetTextColor(walk.RGB(255, 69, 0))
	u.view.dialogStatus = status
	defer func() { u.view.dialogStatus = nil }()
	dialog.Closing().Attach(func(cancel *bool, _ walk.CloseReason) {
		if u.busy {
			*cancel = true
			u.message("请求正在处理，请等待回执")
		}
	})
	if !recovery && u.api != nil && !u.offline {
		readPolicy()
	}
	dialog.Run()
}
