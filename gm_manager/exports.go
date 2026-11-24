//go:build windows

package main

import (
	"encoding/csv"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

func exportRows(name string, header []string, rows [][]string) (string, error) {
	if len(rows) == 0 {
		return "", fmt.Errorf("当前查询没有可导出的记录")
	}
	directory := filepath.Join(localDataDirectory(), "导出")
	if err := os.MkdirAll(directory, 0700); err != nil {
		return "", err
	}
	path := filepath.Join(directory, name+"_"+time.Now().In(chinaTime).Format("20060102_150405.000")+".csv")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return "", err
	}
	defer f.Close()
	if _, err = f.WriteString("\xef\xbb\xbf"); err != nil {
		return "", err
	}
	writer := csv.NewWriter(f)
	safe := func(row []string) []string {
		out := make([]string, len(row))
		for i, value := range row {
			trimmed := strings.TrimSpace(value)
			if len(trimmed) > 0 && strings.ContainsRune("=+-@", rune(trimmed[0])) {
				value = "'" + value
			}
			out[i] = value
		}
		return out
	}
	if err = writer.Write(header); err != nil {
		return "", err
	}
	for _, row := range rows {
		if err = writer.Write(safe(row)); err != nil {
			return "", err
		}
	}
	writer.Flush()
	if err = writer.Error(); err != nil {
		return "", err
	}
	if err = f.Sync(); err != nil {
		return "", err
	}
	return path, nil
}
func (r *recordsUI) export() {
	path, err := exportRows("游戏记录本页", []string{"时间", "类型", "账号", "角色", "方向", "数量或内容", "地图或副本", "通关时间", "结果"}, r.model.rows)
	if err != nil {
		r.detail.SetText(err.Error())
		return
	}
	r.detail.SetText("本页已导出：\r\n" + path)
}
func (a *operationLogUI) export() {
	path, err := exportRows("操作日志本页", []string{"时间", "操作人或来源", "模块", "动作", "目标", "结果"}, a.model.rows)
	if err != nil {
		a.detail.SetText(err.Error())
		return
	}
	a.detail.SetText("本页已导出：\r\n" + path)
}
func (r *recordsUI) queryFirst() { r.pageNumber = 1; r.query() }
