//go:build windows

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
	"newadmin/internal/adminapi"
)

const recordCleanupPath = "/manage/v1/records/cleanup"

func chooseRecordCleanup() (string, []string, bool) {
	var dialog *walk.Dialog
	var date *walk.LineEdit
	var gold, currency, revive *walk.CheckBox
	var before string
	var kinds []string
	preview := func() {
		before = strings.TrimSpace(date.Text())
		day, err := time.Parse("2006-01-02", before)
		if err != nil || day.Format("2006-01-02") != before || day.Year() < 1 || before > time.Now().In(chinaTime).Format("2006-01-02") {
			walk.MsgBox(dialog, "日期无效", "请输入 YYYY-MM-DD 格式的日期，最晚可选今天。", walk.MsgBoxIconWarning)
			return
		}
		kinds = nil
		for _, selection := range []struct {
			kind string
			box  *walk.CheckBox
		}{{"gold", gold}, {"currency", currency}, {"revive", revive}} {
			if selection.box.Checked() {
				kinds = append(kinds, selection.kind)
			}
		}
		if len(kinds) == 0 {
			walk.MsgBox(dialog, "请选择流水类型", "至少勾选一项。", walk.MsgBoxIconWarning)
			return
		}
		dialog.Accept()
	}
	result, err := (decl.Dialog{
		AssignTo: &dialog, Title: "清理历史流水", MinSize: decl.Size{Width: 420}, Layout: decl.VBox{},
		Children: []decl.Widget{
			decl.Composite{Layout: decl.HBox{MarginsZero: true}, Children: []decl.Widget{
				decl.Label{Text: "清理此日期之前的记录（北京时间）"},
				decl.LineEdit{AssignTo: &date, Text: time.Now().In(chinaTime).AddDate(0, 0, -90).Format("2006-01-02"), MaxLength: 10, MinSize: decl.Size{Width: 110}},
			}},
			decl.CheckBox{AssignTo: &gold, Text: adminapi.RecordCleanupLabel("gold"), Checked: true},
			decl.CheckBox{AssignTo: &currency, Text: adminapi.RecordCleanupLabel("currency")},
			decl.CheckBox{AssignTo: &revive, Text: adminapi.RecordCleanupLabel("revive")},
			decl.Composite{Layout: decl.HBox{MarginsZero: true}, Children: []decl.Widget{
				decl.HSpacer{}, decl.PushButton{Text: "预览清理范围", OnClicked: preview},
				decl.PushButton{Text: "取消", OnClicked: func() { dialog.Cancel() }},
			}},
		},
	}).Run(applicationWindow)
	if err != nil {
		walk.MsgBox(applicationWindow, "无法打开清理窗口", err.Error(), walk.MsgBoxIconError)
	}
	return before, kinds, err == nil && result == walk.DlgCmdOK
}

func (r *recordsUI) requestCleanup() {
	if currentManager() == nil {
		walk.MsgBox(applicationWindow, "清理历史流水", "请先连接管理服务。", walk.MsgBoxIconWarning)
		return
	}
	before, kinds, accepted := chooseRecordCleanup()
	if !accepted {
		return
	}
	r.cleanup.SetEnabled(false)
	var plan adminapi.RecordCleanupPlan
	var previewClient *adminapi.Client
	query := url.Values{"before": {before}, "kind": kinds}
	managerJob("记录查询", "预览历史清理", func(c *adminapi.Client) error {
		previewClient = c
		var status adminapi.Status
		if err := c.Do(context.Background(), "GET", "/manage/v1/status", "", nil, &status); err != nil {
			return err
		}
		if !slices.Contains(status.Capabilities, "records.cleanup") {
			return fmt.Errorf("当前管理服务未启用历史流水清理。")
		}
		return c.Do(context.Background(), "GET", recordCleanupPath+"?"+query.Encode(), "", nil, &plan)
	}, func(err error) {
		if err != nil {
			r.cleanup.SetEnabled(true)
			walk.MsgBox(applicationWindow, "无法预览清理范围", err.Error(), walk.MsgBoxIconError)
			return
		}
		total, lines := cleanupSummary(plan.Targets)
		if total == 0 {
			r.cleanup.SetEnabled(true)
			walk.MsgBox(applicationWindow, "清理历史流水", "所选范围没有可清理的记录。", walk.MsgBoxIconInformation)
			return
		}
		message := fmt.Sprintf("将删除北京时间 %s 之前的 %d 条流水。\n\n%s\n\n确认后会先把数据库备份到 E:\\codex\\GM历史记录备份。清理会让 SQLite 复用空间，数据库文件大小可能暂时不变。", plan.Before, total, lines)
		if walk.MsgBox(applicationWindow, "确认清理历史流水", message, walk.MsgBoxYesNo|walk.MsgBoxIconWarning|walk.MsgBoxDefButton2) != walk.DlgCmdYes {
			r.cleanup.SetEnabled(true)
			return
		}
		r.runCleanup(previewClient, plan)
	})
}

func cleanupSummary(targets []adminapi.RecordCleanupTarget) (int64, string) {
	var total int64
	lines := make([]string, 0, len(targets))
	for _, target := range targets {
		total += target.Count
		lines = append(lines, fmt.Sprintf("%s %d 条", adminapi.RecordCleanupLabel(target.Kind), target.Count))
	}
	return total, strings.Join(lines, "\n")
}

func (r *recordsUI) runCleanup(previewClient *adminapi.Client, plan adminapi.RecordCleanupPlan) {
	id := adminapi.NewRequestID()
	directory := filepath.Join(`E:\codex\GM历史记录备份`, time.Now().In(chinaTime).Format("20060102_150405")+"_"+id)
	database := filepath.Join(directory, "players.db")
	var result adminapi.RecordCleanupResult
	var submitted bool
	r.cleanup.SetText("正在备份和清理…")
	managerJob("记录查询", "清理历史流水", func(c *adminapi.Client) error {
		if c != previewClient {
			return fmt.Errorf("管理连接已变化，请重新预览。")
		}
		if err := privateDirectory(directory); err != nil {
			return fmt.Errorf("无法创建本机备份目录：%w", err)
		}
		sha, err := c.Download(context.Background(), "/manage/v1/database/snapshot", database)
		if err != nil {
			return fmt.Errorf("清理前备份失败：%w", err)
		}
		if err = checkBackupDatabase(database); err != nil {
			return err
		}
		backup, _ := json.MarshalIndent(struct {
			RequestID string                     `json:"request_id"`
			Endpoint  string                     `json:"endpoint"`
			Database  string                     `json:"database_sha256"`
			Plan      adminapi.RecordCleanupPlan `json:"plan"`
		}{id, c.Endpoint(), sha, plan}, "", "  ")
		if err = os.WriteFile(filepath.Join(directory, "备份校验.json"), backup, 0600); err != nil {
			return err
		}
		if c != currentManager() {
			return fmt.Errorf("备份后管理连接已变化，请重新预览。")
		}
		body := adminapi.RecordCleanupRequest{Plan: plan, BackupSHA256: sha, Confirm: true}
		submitted = true
		err = c.Do(context.Background(), "POST", recordCleanupPath, id, body, &result)
		var failure *adminapi.ClientError
		if errors.As(err, &failure) && failure.Unknown {
			if receiptErr := c.Do(context.Background(), "GET", "/manage/v1/receipts/"+url.PathEscape(id), "", nil, &result); receiptErr == nil {
				return nil
			}
		}
		return err
	}, func(err error) {
		r.cleanup.SetText("清理历史流水")
		r.cleanup.SetEnabled(true)
		if err != nil {
			message := err.Error()
			if submitted {
				message += "\n请求编号 " + id + "\n备份 " + database
			}
			walk.MsgBox(applicationWindow, "清理请求未确认", message, walk.MsgBoxIconWarning)
			return
		}
		total, lines := cleanupSummary(result.Deleted)
		logUIAction("记录查询", "清理历史流水", result.Before, fmt.Sprintf("删除 %d 条，编号 %s", total, id))
		r.queryFirst()
		walk.MsgBox(applicationWindow, "历史流水清理完成", fmt.Sprintf("已删除 %d 条流水。\n\n%s\n\nSQLite 新增可复用空间 %.2f MiB。\n备份 %s", total, lines, float64(result.Reusable)/(1024*1024), database), walk.MsgBoxIconInformation)
	})
}
