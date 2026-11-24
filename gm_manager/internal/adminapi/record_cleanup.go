package adminapi

import "time"

type RecordCleanupTarget struct {
	Kind  string `json:"kind"`
	Count int64  `json:"count"`
	MaxID int64  `json:"max_id"`
}

type RecordCleanupPlan struct {
	Before    string                `json:"before"`
	Targets   []RecordCleanupTarget `json:"targets"`
	ExpiresAt time.Time             `json:"expires_at"`
	Proof     string                `json:"proof"`
}

type RecordCleanupRequest struct {
	Plan         RecordCleanupPlan `json:"plan"`
	BackupSHA256 string            `json:"backup_sha256"`
	Confirm      bool              `json:"confirm"`
}

type RecordCleanupResult struct {
	Before       string                `json:"before"`
	Deleted      []RecordCleanupTarget `json:"deleted"`
	Reusable     int64                 `json:"reusable_bytes"`
	BackupSHA256 string                `json:"backup_sha256"`
}

// Currency changes share one ledger and therefore one retention selection.
func RecordCleanupLabel(kind string) string {
	switch kind {
	case "gold":
		return "金币余额流水"
	case "currency":
		return "点券、代币、欢乐币余额流水"
	case "revive":
		return "复活币流水"
	default:
		return kind
	}
}
