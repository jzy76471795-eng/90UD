//go:build windows

package main

import (
	"context"
	"fmt"
	"github.com/lxn/walk"
	"newadmin/internal/adminapi"
	"strings"
)

func showJobStatus(section string, output *walk.TextEdit) {
	var result struct {
		Enabled bool `json:"worker_enabled"`
		Items   []struct {
			Section   string `json:"section"`
			Enabled   bool   `json:"enabled"`
			Delivered int    `json:"delivered"`
			Last      string `json:"last_delivery"`
			Error     string `json:"last_error"`
		} `json:"items"`
	}
	managerJob("系统", "查询规则执行状态", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "GET", "/manage/v1/jobs?section="+section, "", nil, &result)
	}, func(err error) {
		if err != nil {
			output.SetText(err.Error())
			return
		}
		lines := []string{fmt.Sprintf("Go任务执行器运行：%t", result.Enabled)}
		if len(result.Items) == 0 {
			lines = append(lines, "还未保存并启用本规则；默认不自动发奖。")
		} else {
			for _, item := range result.Items {
				lines = append(lines, fmt.Sprintf("规则：%s；启用：%t；已提交：%d次\r\n最近提交：%s\r\n最近错误：%s", item.Section, item.Enabled, item.Delivered, item.Last, item.Error))
			}
		}
		output.SetText(strings.Join(lines, "\r\n"))
	})
}
