//go:build windows

package main

import (
	"fmt"
	"github.com/lxn/walk"
	"newadmin/internal/adminapi"
)

// A read is bound to the exact connection, including a valid empty revision zero.
type configurationReadState struct {
	client   *adminapi.Client
	loaded   bool
	sequence uint64
	saving   bool
}

func (s *configurationReadState) begin(button *walk.PushButton) uint64 {
	s.client = nil
	s.loaded = false
	s.sequence++
	if button != nil {
		button.SetEnabled(false)
	}
	return s.sequence
}
func (s *configurationReadState) accept(sequence uint64, client *adminapi.Client, button *walk.PushButton) bool {
	if sequence != s.sequence || client == nil {
		return false
	}
	s.client = client
	s.loaded = true
	if button != nil && !s.saving {
		button.SetEnabled(true)
	}
	return true
}
func (s *configurationReadState) check(client *adminapi.Client) error {
	if s.saving {
		return fmt.Errorf("配置正在保存，请等待结果。")
	}
	if !s.loaded || client == nil || s.client != client {
		return fmt.Errorf("请先成功读取当前连接的配置，再保存修改。")
	}
	return nil
}
func (s *configurationReadState) saveStarted(button *walk.PushButton) uint64 {
	s.saving = true
	if button != nil {
		button.SetEnabled(false)
	}
	return s.sequence
}
func (s *configurationReadState) saveFinished(sequence uint64, client *adminapi.Client, err error, button *walk.PushButton) bool {
	s.saving = false
	if sequence != s.sequence || client != s.client {
		s.begin(button)
		return false
	}
	s.failedSave(err, button)
	if s.loaded && button != nil {
		button.SetEnabled(true)
	}
	return true
}
func (s *configurationReadState) failedSave(err error, button *walk.PushButton) {
	if failure, ok := err.(*adminapi.ClientError); ok && (failure.Status == 409 || failure.Unknown) {
		s.begin(button)
	}
}
