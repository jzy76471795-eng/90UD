//go:build windows

package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
)

var chinaTime = time.FixedZone("Asia/Shanghai", 8*60*60)

func walkConfirm(window walk.Form, title, message string) bool {
	return walk.MsgBox(window, title, message, walk.MsgBoxYesNo|walk.MsgBoxIconQuestion) == walk.DlgCmdYes
}

type textTableModel struct {
	walk.TableModelBase
	rows [][]string
}

func (m *textTableModel) RowCount() int { return len(m.rows) }
func (m *textTableModel) Value(row, col int) interface{} {
	if row < 0 || row >= len(m.rows) || col < 0 || col >= len(m.rows[row]) {
		return nil
	}
	return m.rows[row][col]
}

// Empty endpoints leave the range unrestricted; date-only end values include
// the entire day. All query ranges use the user's Shanghai time zone.
func queryTimeRange(start, end string) (time.Time, time.Time, error) {
	parse := func(value string, endOfDay bool) (time.Time, error) {
		value = strings.TrimSpace(value)
		if value == "" {
			return time.Time{}, nil
		}
		for _, layout := range []string{"2006-01-02 15:04:05", "2006-01-02"} {
			if result, err := time.ParseInLocation(layout, value, chinaTime); err == nil {
				if endOfDay && layout == "2006-01-02" {
					result = result.AddDate(0, 0, 1).Add(-time.Nanosecond)
				}
				return result, nil
			}
		}
		return time.Time{}, fmt.Errorf("时间格式请使用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS。")
	}
	from, err := parse(start, false)
	if err != nil {
		return time.Time{}, time.Time{}, err
	}
	to, err := parse(end, true)
	if err != nil {
		return time.Time{}, time.Time{}, err
	}
	if !from.IsZero() && !to.IsZero() && from.After(to) {
		return time.Time{}, time.Time{}, fmt.Errorf("开始时间不能晚于结束时间。")
	}
	return from, to, nil
}

func inQueryTimeRange(value, from, to time.Time) bool {
	return (from.IsZero() || !value.Before(from)) && (to.IsZero() || !value.After(to))
}

func queryDateRow(start, end **walk.LineEdit) decl.Composite {
	return decl.Composite{
		Layout: decl.HBox{MarginsZero: true, Spacing: 8},
		Children: []decl.Widget{
			decl.Label{Text: "开始时间："}, decl.LineEdit{AssignTo: start, MinSize: decl.Size{Width: 190}, MaxSize: decl.Size{Width: 230}, CueBanner: "YYYY-MM-DD，可留空"},
			decl.Label{Text: "结束时间："}, decl.LineEdit{AssignTo: end, MinSize: decl.Size{Width: 190}, MaxSize: decl.Size{Width: 230}, CueBanner: "YYYY-MM-DD，可留空"},
			decl.Label{Text: "日期或日期时间，按北京时间查询"}, decl.HSpacer{},
		},
	}
}
