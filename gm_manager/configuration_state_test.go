//go:build windows

package main

import (
	"context"
	"net/http/httptest"
	"newadmin/internal/adminapi"
	"newadmin/internal/manager"
	"testing"
)

func testConfigurationClient(t *testing.T) *adminapi.Client {
	t.Helper()
	c, err := adminapi.NewClient("http://127.0.0.1:1", "fixture-token-012345678901234567890123456789", "")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Close)
	return c
}
func TestConfigurationReadMustSucceedOnSameConnection(t *testing.T) {
	var state configurationReadState
	c := testConfigurationClient(t)
	other := testConfigurationClient(t)
	if state.check(c) == nil {
		t.Fatal("unread configuration allowed")
	}
	first := state.begin(nil)
	second := state.begin(nil)
	if state.accept(first, c, nil) {
		t.Fatal("outdated load accepted")
	}
	if !state.accept(second, c, nil) || state.check(c) != nil {
		t.Fatal("valid read not accepted")
	}
	if state.check(other) == nil {
		t.Fatal("read reused after reconnection")
	}
	state.failedSave(&adminapi.ClientError{Status: 409}, nil)
	if state.check(c) == nil {
		t.Fatal("conflicting form remained saveable")
	}
	state.accept(state.sequence, c, nil)
	state.failedSave(&adminapi.ClientError{Unknown: true}, nil)
	if state.check(c) == nil {
		t.Fatal("unknown save outcome remained saveable")
	}
	seq := state.begin(nil)
	state.accept(seq, c, nil)
	state.failedSave(&adminapi.ClientError{Status: 400}, nil)
	if state.check(c) != nil {
		t.Fatal("ordinary validation error lost read state")
	}
}
func TestConfigurationSavePreservesExplicitZeroRevision(t *testing.T) {
	token := "fixture-token-012345678901234567890123456789"
	service, err := manager.New(manager.Config{Listen: "127.0.0.1:0", Token: token, DataDirectory: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	defer service.Close()
	server := httptest.NewServer(service.Handler())
	defer server.Close()
	c, err := adminapi.NewClient(server.URL, token, "")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	var entry adminapi.ConfigEntry
	original := map[string]any{"enabled": true, "title": "原公告", "content": "保留内容"}
	if err = saveConfigWithClient(c, "announcement.launcher", 0, original, &entry); err != nil || entry.Revision != 1 {
		t.Fatal(err, entry)
	}
	stale := map[string]any{"enabled": false, "title": "", "content": ""}
	err = saveConfigWithClient(c, "announcement.launcher", 0, stale, &entry)
	failure, ok := err.(*adminapi.ClientError)
	if !ok || failure.Status != 409 {
		t.Fatal("stale zero revision silently promoted", err)
	}
	if err = c.Do(context.Background(), "GET", "/manage/v1/config/announcement.launcher", "", nil, &entry); err != nil || entry.Revision != 1 || entry.Value["content"] != "保留内容" {
		t.Fatal(err, entry)
	}
}

func TestConfigurationSaveAndReloadCannotShareARevision(t *testing.T) {
	var state configurationReadState
	c := testConfigurationClient(t)
	seq := state.begin(nil)
	state.accept(seq, c, nil)
	saving := state.saveStarted(nil)
	if state.check(c) == nil {
		t.Fatal("second save allowed while first is pending")
	}
	reloading := state.begin(nil)
	state.accept(reloading, c, nil)
	if state.saveFinished(saving, c, nil, nil) {
		t.Fatal("save completion accepted across a reload")
	}
	if state.check(c) == nil {
		t.Fatal("stale reload result remained saveable")
	}
	fresh := state.begin(nil)
	state.accept(fresh, c, nil)
	saving = state.saveStarted(nil)
	if !state.saveFinished(saving, c, nil, nil) || state.check(c) != nil {
		t.Fatal("ordinary successful save did not restore readiness")
	}
}
