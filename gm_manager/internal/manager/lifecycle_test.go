package manager

import (
	"context"
	"net"
	"newadmin/internal/adminapi"
	"sync"
	"testing"
	"time"
)

func TestLifecycleActionsObserveProcessesAndPorts(t *testing.T) {
	s, h := newTestService(t)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	s.config.ServicePorts = []adminapi.Port{{Protocol: "tcp", Port: listener.Addr().(*net.TCPAddr).Port}}
	address := listener.Addr().String()
	listener.Close()
	listener = nil
	defer func() {
		if listener != nil {
			listener.Close()
		}
	}()
	var mu sync.Mutex
	running := false
	calls := []string{}
	s.taskRunner = func(ctx context.Context, command string) (taskState, error) {
		mu.Lock()
		defer mu.Unlock()
		switch command {
		case "start":
			listener, err = net.Listen("tcp", address)
			if err != nil {
				return taskState{}, err
			}
			running = true
			calls = append(calls, command)
		case "stop":
			if listener != nil {
				listener.Close()
				listener = nil
			}
			running = false
			calls = append(calls, command)
		}
		state := taskState{Task: "isolated", State: "Ready"}
		if running {
			state.State = "Running"
			state.RuntimeCount = 2
		}
		return state, nil
	}
	for _, command := range []string{"start", "stop", "restart"} {
		id := "lifecycle-" + command
		body := map[string]any{"command": command, "confirm": true, "force": true}
		status, b := call(t, h, "POST", "/manage/v1/server", id, body)
		if status != 202 {
			t.Fatal(status, b)
		}
		deadline := time.Now().Add(3 * time.Second)
		for {
			_, b = call(t, h, "GET", "/manage/v1/receipts/"+id, "", nil)
			state := b["data"].(map[string]any)["state"]
			if state == "confirmed" {
				break
			}
			if state == "failed" || time.Now().After(deadline) {
				t.Fatal("unconfirmed", b)
			}
			time.Sleep(time.Millisecond)
		}
		if status, b = call(t, h, "POST", "/manage/v1/server", id, body); status != 200 {
			t.Fatal(status, b)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if len(calls) != 4 {
		t.Fatal("replayed OS command", calls)
	}
	if status, _ := call(t, h, "POST", "/manage/v1/server", "arbitrary", map[string]any{"command": "powershell anything", "confirm": true}); status != 400 {
		t.Fatal(status)
	}
}
func TestLifecycleDoesNotClaimReadyFromTaskStateAlone(t *testing.T) {
	s, _ := newTestService(t)
	s.config.ServicePorts = []adminapi.Port{{Protocol: "tcp", Port: 1}}
	s.taskRunner = func(context.Context, string) (taskState, error) {
		return taskState{Task: "isolated", State: "Running", RuntimeCount: 1}, nil
	}
	state, err := s.serverSnapshot(context.Background())
	if err != nil || state.Ready || state.Phase == "running" {
		t.Fatal(state, err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if _, err = s.applyServerAction(ctx, lifecycleRequest{Command: "start", Confirm: true}); err == nil {
		t.Fatal("accepted task without listening services")
	}
}
