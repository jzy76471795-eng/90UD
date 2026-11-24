package manager

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"newadmin/internal/adminapi"
)

func TestNativeWriteDoesNotHoldUpIndependentStatusRead(t *testing.T) {
	s, h := newTestService(t)
	entered, release := make(chan struct{}), make(chan struct{})
	attachGame(t, s, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			gameSessionReply(w)
			return
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		close(entered)
		<-release
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "request_id": body["request_id"], "result": map[string]any{"character_id": 7, "amount": 10, "balance": 110}})
	})
	client, err := adminapi.NewClient(h.URL, s.config.Token, "")
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	writeDone := make(chan error, 1)
	go func() {
		writeDone <- client.Do(context.Background(), "POST", "/manage/v1/actions", "parallel-write", adminapi.Action{Name: "currency.credit", Currency: "gold", CharacterID: 7, Amount: 10, Confirm: true}, nil)
	}()
	select {
	case <-entered:
	case <-time.After(2 * time.Second):
		close(release)
		t.Fatal("write did not reach game")
	}
	readDone := make(chan error, 1)
	go func() {
		var status adminapi.Status
		readDone <- client.Do(context.Background(), "GET", "/manage/v1/status", "", nil, &status)
	}()
	select {
	case err = <-readDone:
		if err != nil {
			t.Error(err)
		}
	case <-time.After(500 * time.Millisecond):
		t.Error("independent read queued behind game mutation")
	}
	close(release)
	if err = <-writeDone; err != nil {
		t.Fatal(err)
	}
}
