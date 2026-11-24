//go:build !windows

package manager

import "context"

func (s *Service) runSystem(context.Context, string) error {
	return fail(503, "windows_required", "系统重启需要Windows")
}
