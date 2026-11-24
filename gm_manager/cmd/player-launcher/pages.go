//go:build windows

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/lxn/walk"
	"newadmin/internal/player"
	"os"
	"path/filepath"
	"strings"
	"unicode"
	"unicode/utf8"
)

var questions = []string{"我小时候的昵称是什么？", "我就读的第一所学校叫什么？", "对我最有纪念意义的城市是哪里？", "我小时候最喜欢的一本书叫什么？", "我印象最深的一位老师叫什么？"}
var questionIDs = []string{"childhood_nickname", "first_school", "memorable_city", "childhood_book", "favorite_teacher"}

func validPassword(v string) bool {
	n := utf8.RuneCountInString(v)
	return n >= 8 && n <= 128 && !strings.ContainsAny(v, "\x00\r\n")
}
func validSecurity(i int, answer string) bool {
	n := utf8.RuneCountInString(strings.TrimSpace(answer))
	if i < 0 || i >= len(questions) || n < 2 || n > 100 {
		return false
	}
	for _, r := range answer {
		if unicode.IsControl(r) {
			return false
		}
	}
	return true
}
func accountValid(v string) bool {
	return strings.TrimSpace(v) != "" && utf8.RuneCountInString(v) <= 64 && !strings.ContainsAny(v, "?\x00\r\n")
}
func (u *launcherUI) selectedSlot() *slot {
	i := u.cdkAccount.CurrentIndex()
	if i < 0 || i >= len(u.cdkSlots) {
		return nil
	}
	return u.slots[u.cdkSlots[i]]
}
func (u *launcherUI) refreshAccounts() {
	names := []string{}
	u.cdkSlots = nil
	for i, s := range u.slots {
		if s != nil && s.Game.Alive() {
			label := "主账号"
			if i == 1 {
				label = "副账号"
			}
			names = append(names, s.Account+"（"+label+"）")
			u.cdkSlots = append(u.cdkSlots, i)
		}
	}
	u.cdkAccount.SetModel(names)
	u.characters = nil
	u.cdkCharacter.SetModel([]string{})
	if len(names) > 0 {
		u.cdkAccount.SetCurrentIndex(0)
	} else {
		u.history.SetText("当前没有登录账号。")
	}
}
func (u *launcherUI) loadCDK() {
	s := u.selectedSlot()
	if s == nil {
		u.message("请先登录并启动游戏")
		return
	}
	u.job("正在查询角色与兑换记录…", func(ctx context.Context) (func(), error) {
		var chars struct {
			Rows []character `json:"characters"`
		}
		if e := u.api.Request(ctx, "GET", "/launcher/v1/cdk/characters", s.Session.AccessToken, nil, &chars); e != nil {
			return nil, e
		}
		var history json.RawMessage
		if e := u.api.Request(ctx, "GET", "/launcher/v1/cdk/redemptions", s.Session.AccessToken, nil, &history); e != nil {
			return nil, e
		}
		return func() {
			if u.selectedSlot() != s {
				return
			}
			u.characters = chars.Rows
			names := []string{}
			for _, v := range chars.Rows {
				names = append(names, fmt.Sprintf("%s · Lv.%d", v.Name, v.Level))
			}
			u.cdkCharacter.SetModel(names)
			if len(names) > 0 {
				u.cdkCharacter.SetCurrentIndex(0)
			}
			u.history.SetText(redemptionHistory(history))
			u.message("兑换记录已刷新")
		}, nil
	})
}

type pendingRedemption struct {
	Account, Code string
	CharacterID   int64
	RequestID     string
}

func (u *launcherUI) redeem() {
	s := u.selectedSlot()
	i := u.cdkCharacter.CurrentIndex()
	code := strings.TrimSpace(u.code.Text())
	if s == nil || i < 0 || i >= len(u.characters) || code == "" {
		u.message("请选择账号、角色并填写兑换码")
		return
	}
	request := pendingRedemption{s.Account, code, u.characters[i].ID, player.ID()}
	if len([]rune(code)) > 100 {
		u.message("兑换码不能超过100个字符")
		return
	}
	pending := filepath.Join(u.cfg.DataDirectory(), "pending-redemption.json")
	u.job("正在兑换，请勿重复提交…", func(ctx context.Context) (func(), error) {
		if e := os.MkdirAll(filepath.Dir(pending), 0700); e != nil {
			return nil, e
		}
		unlock, e := player.LockFile(pending + ".lock")
		if e != nil {
			return nil, errors.New("另一个登录器正在处理兑换，请稍后核对")
		}
		defer unlock()
		firstRequest := true
		if player.NoLinks(pending) != nil {
			return nil, errors.New("待核对兑换路径无效")
		}
		if b, e := os.ReadFile(pending); e == nil {
			var old pendingRedemption
			if len(b) > 65536 || player.StrictJSON(b, &old) != nil {
				return nil, errors.New("待核对兑换记录无效，请保留文件并联系管理员")
			}
			if old.Account != request.Account || old.Code != request.Code || old.CharacterID != request.CharacterID {
				return nil, errors.New("有未确认的兑换，请先核对原账号、原兑换码和原角色的记录")
			}
			request = old
			firstRequest = false
		} else if !os.IsNotExist(e) {
			return nil, e
		} else {
			if e = os.MkdirAll(filepath.Dir(pending), 0700); e != nil {
				return nil, e
			}
			b, _ := json.Marshal(request)
			if e = player.Durable(pending, b); e != nil {
				return nil, e
			}
		}
		var v struct {
			Message string `json:"message"`
		}
		if e := u.api.Request(ctx, "POST", "/launcher/v1/cdk/redemptions", s.Session.AccessToken, map[string]any{"code": request.Code, "character_id": request.CharacterID, "request_id": request.RequestID}, &v); e != nil {
			var status *player.HTTPError
			if firstRequest && errors.As(e, &status) && status.Status >= 400 && status.Status < 500 && status.Status != 408 {
				_ = os.Remove(pending)
				return nil, e
			}
			return nil, fmt.Errorf("%w；已保留原请求编号，请刷新记录核对", e)
		}
		if v.Message == "" {
			return nil, errors.New("兑换回执不完整，保留原请求等待核对")
		}
		if e := os.Remove(pending); e != nil {
			return nil, errors.New("兑换已收到回执，但本地待核对记录尚未清理")
		}
		return func() {
			u.message(v.Message)
			u.code.SetText("")
			u.history.SetText("本次回执：" + v.Message + "\r\n点击刷新查询实际兑换记录。")
		}, nil
	})
}
func redemptionHistory(raw []byte) string {
	var b strings.Builder
	var v struct {
		Rows []struct {
			Name        string `json:"name"`
			Code        string `json:"code"`
			Date        string `json:"created_at"`
			CharacterID int64  `json:"character_id"`
		} `json:"redemptions"`
	}
	if json.Unmarshal(raw, &v) != nil {
		return "服务器返回的记录无效"
	}
	for _, row := range v.Rows {
		fmt.Fprintf(&b, "%s  %s  %s\r\n", row.Date, row.Name, row.Code)
	}
	if b.Len() == 0 {
		return "暂无兑换记录。"
	}
	return b.String()
}
func (u *launcherUI) clearPendingRedemption() {
	if u.busy || u.api == nil || u.offline {
		return
	}
	path := filepath.Join(u.cfg.DataDirectory(), "pending-redemption.json")
	if player.NoLinks(path) != nil {
		u.message("待确认记录路径无效")
		return
	}
	b, e := os.ReadFile(path)
	if os.IsNotExist(e) {
		u.message("没有待确认的兑换")
		return
	}
	var record pendingRedemption
	if e != nil || len(b) > 65536 || player.StrictJSON(b, &record) != nil {
		u.message("待确认记录无法读取，请保留文件并联系管理员")
		return
	}
	if walk.MsgBox(u.mw, "核对兑换结果", "请先刷新兑换记录，核对账号“"+record.Account+"”的兑换结果。\n取消此标记只影响本机，不会撤销服务器上的兑换。\n是否取消？", walk.MsgBoxYesNo|walk.MsgBoxIconQuestion) != walk.DlgCmdYes {
		return
	}
	if e = player.AtomicMoveNew(path, filepath.Join(filepath.Dir(path), "cancelled-redemption-"+player.ID()+".json")); e != nil {
		u.message(e.Error())
		return
	}
	u.message("本机待确认标记已取消，原记录已保留。")
}
