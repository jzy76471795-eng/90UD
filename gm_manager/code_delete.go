//go:build windows

package main

import (
	"context"
	"slices"

	"github.com/lxn/walk"
	"newadmin/internal/adminapi"
)

func selectCodeOnRightClick(table *walk.TableView, x, y int, button walk.MouseButton) {
	if button == walk.RightButton {
		table.SetCurrentIndex(table.IndexAt(x, y))
	}
}

func withoutCode(codes []string, deleted string) []string {
	return slices.DeleteFunc(codes, func(code string) bool { return code == deleted })
}

func (m *textTableModel) removeCode(deleted string) {
	m.rows = slices.DeleteFunc(m.rows, func(row []string) bool { return row[1] == deleted })
	m.PublishRowsReset()
}

func deleteManagedCode(module, kind, code string, status *walk.Label, refresh func()) {
	requestID := adminapi.NewRequestID()
	var receipt adminapi.Receipt
	managerJob(module, "删除选中码", func(client *adminapi.Client) error {
		return client.Do(context.Background(), "POST", "/manage/v1/codes/delete", requestID, map[string]string{"kind": kind, "code": code}, &receipt)
	}, func(err error) {
		if err != nil {
			status.SetText(err.Error())
			return
		}
		refresh()
		status.SetText(receipt.Message)
	})
}
