//go:build windows

package main

import (
	"context"
	"fmt"
	"newadmin/internal/adminapi"
	"time"
)

type remoteServerState struct {
	Task         string `json:"task"`
	State        string `json:"state"`
	Phase        string `json:"phase"`
	RuntimeCount int    `json:"runtime_count"`
	Ready        bool   `json:"ready"`
	StartupError string `json:"startup_error"`
	StartupLog   string `json:"startup_log"`
}

func (s *serverUI) refreshLifecycle() {
	var state remoteServerState
	managerJob("服务器管理", "查询游戏计划任务", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "GET", "/manage/v1/server", "", nil, &state)
	}, func(err error) {
		if err != nil {
			s.output.SetText(s.output.Text() + "\r\n游戏任务：" + err.Error())
			return
		}
		phases := map[string]string{"running": "运行正常", "stopped": "已停止", "starting": "正在启动", "failed": "启动失败", "stale": "任务空转"}
		s.output.SetText(s.output.Text() + fmt.Sprintf("\r\n游戏任务：%s；状态：%s；实际游戏进程：%d；端口就绪：%t", state.Task, phases[state.Phase], state.RuntimeCount, state.Ready))
		if state.StartupError != "" {
			s.output.SetText(s.output.Text() + "\r\n启动错误：" + state.StartupError + "\r\n日志：" + state.StartupLog)
		}
	})
}
func (s *serverUI) operate(command string) {
	names := map[string]string{"start": "启动游戏服务", "stop": "停止游戏服务", "restart": "重启游戏服务", "reboot": "重启服务器系统", "cancel_reboot": "取消系统重启"}
	name := names[command]
	if name == "" {
		return
	}
	if currentManager() == nil {
		s.output.SetText("请先连接管理服务。")
		return
	}
	force := s.force.Checked()
	message := name + "\r\n只操作服务器上配置的游戏计划任务。\r\n"
	if command != "start" {
		if force {
			message += "已选择强制：在线连接会中断，请确认玩家存档风险。"
		} else {
			message += "有玩家在线或在线状态未知时会拒绝停止。"
		}
	}
	if command == "reboot" {
		message = "服务器：" + s.hostname + "\r\n地址：" + currentManager().Endpoint() + "\r\n整个Windows系统将在30秒后重启，所有游戏连接都会中断。确定执行？"
	}
	if command == "cancel_reboot" {
		message = "取消当前Windows系统重启倒计时？"
	}
	if !walkConfirm(applicationWindow, "确认"+name, message) {
		return
	}
	for _, button := range s.operations {
		button.SetEnabled(false)
	}
	id := adminapi.NewRequestID()
	var receipt adminapi.Receipt
	s.output.SetText("正在提交" + name + "，请求：" + id)
	managerJob("服务器管理", name, func(c *adminapi.Client) error {
		ctx, cancel := context.WithTimeout(context.Background(), 115*time.Second)
		defer cancel()
		if err := c.Do(ctx, "POST", "/manage/v1/server", id, map[string]any{"command": command, "confirm": true, "force": force, "hostname": s.hostname}, &receipt); err != nil {
			return err
		}
		for receipt.State == "pending" {
			select {
			case <-ctx.Done():
				return fmt.Errorf("状态未确认，请查询原回执：%s", id)
			case <-time.After(time.Second):
			}
			if err := c.Do(ctx, "GET", "/manage/v1/receipts/"+id, "", nil, &receipt); err != nil {
				return err
			}
		}
		if receipt.State != "confirmed" {
			return fmt.Errorf("%s\n请求：%s", receipt.Message, id)
		}
		return nil
	}, func(err error) {
		for _, button := range s.operations {
			button.SetEnabled(true)
		}
		if err != nil {
			s.output.SetText(err.Error())
			return
		}
		s.output.SetText(name + "已核对完成。\r\n" + receipt.Message + "\r\n请求：" + id)
		logUIAction("服务器管理", name, "固定游戏计划任务", receipt.Message)
		s.refreshLifecycle()
	})
}
