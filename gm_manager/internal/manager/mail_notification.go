package manager

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"time"
)

// Runs only after the durable receipt commits, including on an idempotent
// retry. Notification never changes the award outcome and needs no DB lock.
func (s *Service) refreshCreatedMails(ctx context.Context, value any) {
	if s.config.ControlEndpointFile == "" {
		return
	}
	var receipt struct {
		RequestID string `json:"request_id"`
		Result    struct {
			Items []struct {
				Account   int `json:"account_id"`
				Character int `json:"character_id"`
			} `json:"items"`
		} `json:"result"`
	}
	raw, err := json.Marshal(value)
	if err != nil || json.Unmarshal(raw, &receipt) != nil {
		return
	}
	bounded, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	snapshot, err := s.game.session(bounded)
	if err != nil || !snapshot.Complete {
		return
	}
	targets := map[int]int{}
	for _, item := range receipt.Result.Items {
		targets[item.Character] = item.Account
	}
	l := &launcherService{manager: s}
	for _, online := range snapshot.Connections {
		if bounded.Err() != nil {
			return
		}
		if targets[online.CharacterID] != online.AccountID || online.AccountID < 1 || online.CharacterID < 1 {
			continue
		}
		digest := sha256.Sum256([]byte(receipt.RequestID))
		rid := "mail-refresh-" + hex.EncodeToString(digest[:])
		_ = l.engine(bounded, "refresh_mailbox", map[string]any{"account_id": online.AccountID, "character_id": online.CharacterID, "request_id": rid}, nil)
	}
}
