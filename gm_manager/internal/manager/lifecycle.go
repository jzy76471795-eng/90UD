package manager

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"newadmin/internal/adminapi"
)

type taskState struct {
	Task         string `json:"task"`
	State        string `json:"state"`
	RuntimeCount int    `json:"runtime_count"`
	LastResult   int64  `json:"last_result"`
	LastRun      int64  `json:"last_run"`
}
type serverState struct {
	taskState
	Phase        string          `json:"phase"`
	Ready        bool            `json:"ready"`
	Ports        []adminapi.Port `json:"ports"`
	StartupError string          `json:"startup_error,omitempty"`
	StartupLog   string          `json:"startup_log,omitempty"`
}
type lifecycleRequest struct {
	Hostname string `json:"hostname,omitempty"`
	Command  string `json:"command"`
	Confirm  bool   `json:"confirm"`
	Force    bool   `json:"force,omitempty"`
}

func (s *Service) serverSnapshot(ctx context.Context) (serverState, error) {
	state, err := s.taskRunner(ctx, "status")
	if err != nil {
		return serverState{}, err
	}
	out := serverState{taskState: state, Phase: "stopped", Ports: []adminapi.Port{}}
	all := true
	ports := s.config.ServicePorts
	if len(ports) == 0 {
		ports = []adminapi.Port{{Name: "登录", Protocol: "tcp", Port: 17605}, {Name: "频道", Protocol: "tcp", Port: 7001}, {Name: "游戏", Protocol: "tcp", Port: 10052}}
	}
	for _, p := range ports {
		if p.Port == 17890 {
			continue
		}
		p.Listening = "unreachable"
		dialer := net.Dialer{Timeout: 100 * time.Millisecond}
		c, e := dialer.DialContext(ctx, "tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(p.Port)))
		if e == nil {
			p.Listening = "listening"
			c.Close()
		} else {
			all = false
		}
		out.Ports = append(out.Ports, p)
	}
	out.Ready = all && len(out.Ports) > 0 && state.RuntimeCount > 0
	if out.Ready {
		out.Phase = "running"
	} else if state.RuntimeCount > 0 || state.State == "Running" && time.Now().Unix()-state.LastRun < 20 {
		out.Phase = "starting"
	} else if state.State == "Running" {
		out.Phase = "stale"
	}
	logs, _ := filepath.Glob(filepath.Join(s.config.ServerRoot, "server_proto", "log", "launch_game_session_*.out.log"))
	var newest string
	var stamp time.Time
	for _, p := range logs {
		if info, e := os.Stat(p); e == nil && info.ModTime().After(stamp) {
			stamp = info.ModTime()
			newest = p
		}
	}
	if newest != "" && stamp.Unix() >= state.LastRun-2 {
		if f, e := os.Open(newest); e == nil {
			defer f.Close()
			info, _ := f.Stat()
			offset := max(int64(0), info.Size()-16384)
			f.Seek(offset, 0)
			raw, _ := io.ReadAll(io.LimitReader(f, 16384))
			for _, line := range strings.Split(string(raw), "\n") {
				if strings.Contains(line, "preload/warmup failed:") || strings.HasPrefix(line, "RuntimeError:") || strings.HasPrefix(line, "ImportError:") || strings.HasPrefix(line, "ModuleNotFoundError:") {
					out.StartupError = string([]rune(line)[:min(400, len([]rune(line)))])
					out.StartupLog = newest
				}
			}
		}
	}
	if !out.Ready && state.RuntimeCount == 0 && out.StartupError != "" {
		out.Phase = "failed"
	}
	return out, nil
}
func (s *Service) serverStatus(w http.ResponseWriter, r *http.Request) {
	state, err := s.serverSnapshot(r.Context())
	if err != nil {
		writeError(w, err, "")
		return
	}
	write(w, 200, adminapi.Envelope{OK: true, Data: state})
}

func (s *Service) serverAction(w http.ResponseWriter, r *http.Request) {
	var body lifecycleRequest
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	if !body.Confirm || body.Command != "start" && body.Command != "stop" && body.Command != "restart" && body.Command != "reboot" && body.Command != "cancel_reboot" {
		writeError(w, fail(400, "invalid_server_action", "只接受已确认的启动、停止或重启"), "")
		return
	}
	id := r.Header.Get("Idempotency-Key")
	if body.Command == "reboot" || body.Command == "cancel_reboot" {
		hostname, _ := os.Hostname()
		if !s.config.AllowSystemReboot || body.Hostname != hostname {
			writeError(w, fail(403, "system_reboot_disabled", "系统重启未启用或目标机器身份不匹配"), "")
			return
		}
	}
	if len(id) < 1 || len(id) > 100 || strings.ContainsAny(id, "\r\n\x00") {
		writeError(w, fail(400, "request_id_required", "操作必须带请求编号"), id)
		return
	}
	raw, _ := json.Marshal(body)
	sum := sha256.Sum256(append([]byte("server.action\n"), raw...))
	fingerprint := hex.EncodeToString(sum[:])
	var reply adminapi.Envelope
	fresh := false
	if !s.lifecycleMu.TryLock() {
		writeError(w, fail(409, "server_busy", "已有服务器操作执行中，请先核对原回执"), id)
		return
	}
	err := s.store.transaction(r.Context(), func(tx *sql.Tx) error {
		prior, found, e := readRequest(r.Context(), tx, id)
		if e != nil {
			return e
		}
		if found {
			if prior.Fingerprint != fingerprint {
				return fail(409, "request_conflict", "请求编号已用于其他操作")
			}
			reply = prior.Reply
			return nil
		}
		fresh = true
		reply = adminapi.Envelope{OK: true, RequestID: id, Data: adminapi.Receipt{RequestID: id, Action: "server." + body.Command, State: "pending", Message: "操作已登记，正在核对真实进程和端口状态", CreatedAt: time.Now().UTC()}}
		return saveRequest(r.Context(), tx, id, savedRequest{Fingerprint: fingerprint, Status: 200, Reply: reply}, false)
	})
	if err != nil || !fresh {
		s.lifecycleMu.Unlock()
		if err != nil {
			writeError(w, err, id)
		} else {
			write(w, 200, reply)
		}
		return
	}
	go func() {
		defer s.lifecycleMu.Unlock()
		ctx, cancel := context.WithTimeout(context.Background(), 100*time.Second)
		defer cancel()
		state, e := s.applyServerAction(ctx, body)
		receipt := adminapi.Receipt{RequestID: id, Action: "server." + body.Command, State: "confirmed", AppliedToGame: e == nil, CreatedAt: time.Now().UTC(), Result: state, Message: "实际任务、运行进程和端口状态已核对"}
		if e != nil {
			receipt.State = "failed"
			receipt.Message = e.Error()
		}
		final := adminapi.Envelope{OK: true, RequestID: id, Data: receipt}
		if e == nil && state.Phase == "reboot_scheduled" {
			receipt.Message = "Windows已接受30秒后的系统重启，可在倒计时内取消"
			receipt.AppliedToGame = false
		}
		if e == nil && state.Phase == "reboot_cancelled" {
			receipt.Message = "Windows已确认取消系统重启"
			receipt.AppliedToGame = false
		}
		final.Data = receipt
		_ = s.store.transaction(context.Background(), func(tx *sql.Tx) error {
			if e := saveRequest(context.Background(), tx, id, savedRequest{Fingerprint: fingerprint, Status: 200, Reply: final}, true); e != nil {
				return e
			}
			return appendAudit(context.Background(), tx, "服务器管理", receipt.Action, s.config.GameTaskName, receipt.State, id, receipt.Message)
		})
	}()
	write(w, 202, reply)
}
func (s *Service) applyServerAction(ctx context.Context, body lifecycleRequest) (serverState, error) {
	if body.Command == "reboot" || body.Command == "cancel_reboot" {
		phase := "reboot_cancelled"
		if body.Command == "reboot" {
			phase = "reboot_scheduled"
		}
		err := s.systemRunner(ctx, body.Command)
		return serverState{Phase: phase}, err
	}
	if body.Command != "start" && !body.Force {
		snap, e := s.game.session(ctx)
		if e == nil && snap.Complete && len(snap.Connections) > 0 {
			return serverState{}, fail(409, "players_online", "仍有在线连接，请让玩家离线，或明确选择强制停止")
		}
		if e != nil || !snap.Complete {
			if state, e2 := s.serverSnapshot(ctx); e2 != nil || state.RuntimeCount > 0 {
				return serverState{}, fail(409, "online_unknown", "不能确认玩家连接，未停止；强制操作需另外勾选确认")
			}
		}
	}
	wait := func(running bool) (serverState, error) {
		for {
			state, e := s.serverSnapshot(ctx)
			if e != nil {
				return state, e
			}
			portsDown := true
			for _, port := range state.Ports {
				if port.Listening == "listening" {
					portsDown = false
				}
			}
			if running && state.Ready || !running && state.RuntimeCount == 0 && state.State != "Running" && portsDown {
				if !running {
					state.Phase = "stopped"
				}
				return state, nil
			}
			if running && state.Phase == "failed" {
				return state, fail(409, "startup_failed", state.StartupError)
			}
			select {
			case <-ctx.Done():
				return state, fail(504, "server_state_unconfirmed", "未在时间内确认目标状态，请刷新任务和端口；不要重复操作")
			case <-time.After(500 * time.Millisecond):
			}
		}
	}
	if body.Command == "stop" || body.Command == "restart" {
		if _, e := s.taskRunner(ctx, "stop"); e != nil {
			return serverState{}, e
		}
		if state, e := wait(false); e != nil || body.Command == "stop" {
			return state, e
		}
	}
	if _, e := s.taskRunner(ctx, "start"); e != nil {
		return serverState{}, e
	}
	return wait(true)
}
