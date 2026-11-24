package adminapi

import "time"

const Version = "1.1.2-direct"

type Error struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Unknown bool   `json:"unknown,omitempty"`
}
type Envelope struct {
	OK        bool   `json:"ok"`
	Data      any    `json:"data,omitempty"`
	Error     *Error `json:"error,omitempty"`
	RequestID string `json:"request_id,omitempty"`
}
type Item struct {
	ID       int64 `json:"id"`
	Quantity int64 `json:"quantity"`
}
type Reward struct {
	Items  []Item `json:"items"`
	Points int64  `json:"points"`
	Tokens int64  `json:"tokens"`
	Gold   int64  `json:"gold"`
}
type Account struct {
	ID             int    `json:"id"`
	Name           string `json:"name"`
	Points         int64  `json:"points"`
	Tokens         int64  `json:"tokens"`
	CharacterCount int    `json:"character_count"`
	OnlineStatus   string `json:"online_status"`
	LoginIP        string `json:"login_ip"`
	MachineID      string `json:"machine_id"`
	LastLogin      string `json:"last_login"`
	Banned         bool   `json:"banned"`
	BanKnown       bool   `json:"ban_known"`
	AccountBlocked bool   `json:"account_blocked"`
	IPBlocked      bool   `json:"ip_blocked"`
	MachineBlocked bool   `json:"machine_blocked"`
}
type Character struct {
	ID           int    `json:"id"`
	AccountID    int    `json:"account_id"`
	Name         string `json:"name"`
	AccountName  string `json:"account_name"`
	Level        int    `json:"level"`
	Job          string `json:"job"`
	Gold         *int64 `json:"gold"`
	OnlineStatus string `json:"online_status"`
}
type Page[T any] struct {
	Items    []T    `json:"items"`
	Total    int    `json:"total"`
	Page     int    `json:"page"`
	PageSize int    `json:"page_size"`
	Source   string `json:"source,omitempty"`
}
type Port struct {
	Name      string `json:"name"`
	Protocol  string `json:"protocol"`
	Port      int    `json:"port"`
	Listening string `json:"listening"`
	Reachable string `json:"reachable"`
}
type Status struct {
	WriteMode        string    `json:"write_mode,omitempty"`
	Version          string    `json:"version"`
	Language         string    `json:"language"`
	OS               string    `json:"os"`
	Hostname         string    `json:"hostname"`
	UptimeSeconds    int64     `json:"uptime_seconds"`
	MemoryBytes      uint64    `json:"memory_bytes"`
	Goroutines       int       `json:"goroutines"`
	PID              int       `json:"pid"`
	ServerRoot       string    `json:"server_root"`
	Database         string    `json:"database"`
	DatabaseState    string    `json:"database_state"`
	GameState        string    `json:"game_state"`
	OnlineAccounts   *int      `json:"online_accounts"`
	OnlineCharacters *int      `json:"online_characters"`
	Ports            []Port    `json:"ports"`
	Capabilities     []string  `json:"capabilities"`
	CheckedAt        time.Time `json:"checked_at"`
}
type Receipt struct {
	DatabaseCommitted bool      `json:"database_committed,omitempty"`
	RequestID         string    `json:"request_id"`
	Action            string    `json:"action"`
	State             string    `json:"state"`
	Message           string    `json:"message"`
	Result            any       `json:"result,omitempty"`
	AppliedToGame     bool      `json:"applied_to_game"`
	CreatedAt         time.Time `json:"created_at"`
}
type Action struct {
	Name        string `json:"name"`
	Target      string `json:"target,omitempty"`
	Scope       string `json:"scope,omitempty"`
	AccountID   int    `json:"account_id,omitempty"`
	CharacterID int    `json:"character_id,omitempty"`
	Currency    string `json:"currency,omitempty"`
	Amount      int64  `json:"amount,omitempty"`
	Body        string `json:"body,omitempty"`
	Reward      Reward `json:"reward,omitempty"`
	Confirm     bool   `json:"confirm"`
}
type Code struct {
	Code      string    `json:"code"`
	Name      string    `json:"name"`
	Kind      string    `json:"kind"`
	Reward    Reward    `json:"reward"`
	State     string    `json:"state"`
	CreatedAt time.Time `json:"created_at"`
}
type CodeRequest struct {
	Kind     string `json:"kind"`
	Name     string `json:"name"`
	Quantity int    `json:"quantity"`
	Reward   Reward `json:"reward"`
}
type ConfigEntry struct {
	DatabaseCommitted bool           `json:"database_committed,omitempty"`
	Section           string         `json:"section"`
	Revision          int64          `json:"revision"`
	Value             map[string]any `json:"value"`
	AppliedToGame     bool           `json:"applied_to_game"`
	UpdatedAt         time.Time      `json:"updated_at"`
}
type Audit struct {
	ID        int64     `json:"id"`
	At        time.Time `json:"at"`
	Actor     string    `json:"actor"`
	Module    string    `json:"module"`
	Action    string    `json:"action"`
	Target    string    `json:"target"`
	Result    string    `json:"result"`
	RequestID string    `json:"request_id"`
	Detail    string    `json:"detail"`
}
type Record struct {
	Code      string     `json:"code,omitempty"`
	Name      string     `json:"name,omitempty"`
	Reward    *Reward    `json:"reward,omitempty"`
	At        time.Time  `json:"at"`
	Kind      string     `json:"kind"`
	Account   string     `json:"account"`
	Character string     `json:"character"`
	Direction string     `json:"direction"`
	Content   string     `json:"content"`
	Place     string     `json:"place"`
	Result    string     `json:"result"`
	Start     *time.Time `json:"start,omitempty"`
	End       *time.Time `json:"end,omitempty"`
	ClearMS   int64      `json:"clear_ms"`
}
