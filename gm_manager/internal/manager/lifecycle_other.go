//go:build !windows

package manager

import "context"

func (s *Service) runTask(context.Context, string) (taskState, error) {
	return taskState{}, fail(503, "windows_required", "游戏计划任务需要 Windows")
}
