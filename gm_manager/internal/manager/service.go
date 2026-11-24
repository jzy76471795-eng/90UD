package manager

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"newadmin/internal/adminapi"
)

type Config struct {
	LauncherListen      string          `json:"launcher_listen,omitempty"`
	LauncherCertificate string          `json:"launcher_certificate,omitempty"`
	LauncherKey         string          `json:"launcher_key,omitempty"`
	LauncherPublicHost  string          `json:"launcher_public_host,omitempty"`
	ManagerTaskName     string          `json:"manager_task_name,omitempty"`
	ManagerDirectory    string          `json:"manager_directory,omitempty"`
	AllowSystemReboot   bool            `json:"allow_system_reboot,omitempty"`
	GameTaskName        string          `json:"game_task_name,omitempty"`
	DirectDatabase      bool            `json:"direct_database,omitempty"`
	EnableJobs          bool            `json:"enable_jobs,omitempty"`
	Listen              string          `json:"listen"`
	Token               string          `json:"token"`
	DataDirectory       string          `json:"data_directory"`
	TLSCertificate      string          `json:"tls_certificate,omitempty"`
	TLSKey              string          `json:"tls_key,omitempty"`
	ServerRoot          string          `json:"server_root,omitempty"`
	GameDatabase        string          `json:"game_database,omitempty"`
	ControlEndpointFile string          `json:"control_endpoint_file,omitempty"`
	ServicePorts        []adminapi.Port `json:"service_ports,omitempty"`
}
type Service struct {
	config       Config
	store        *stateStore
	reader       gameReader
	game         *gameControl
	started      time.Time
	requestMu    sync.Mutex
	active       map[string]bool
	cancel       context.CancelFunc
	done         chan struct{}
	lifecycleMu  sync.Mutex
	updateMu     sync.Mutex
	shutdown     chan struct{}
	shutdownOnce sync.Once
	taskRunner   func(context.Context, string) (taskState, error)
	systemRunner func(context.Context, string) error
}
type apiFailure struct {
	Status        int
	Code, Message string
	Unknown       bool
}

func (e *apiFailure) Error() string { return e.Message }
func fail(status int, code, message string) error {
	return &apiFailure{Status: status, Code: code, Message: message}
}
func New(config Config) (*Service, error) {
	if len(config.Token) < 32 {
		return nil, fmt.Errorf("管理令牌至少32个字符")
	}
	if config.DataDirectory == "" || !filepath.IsAbs(config.DataDirectory) {
		return nil, fmt.Errorf("必须设置绝对数据目录")
	}
	if config.Listen == "" {
		config.Listen = "127.0.0.1:17890"
	}
	if gameInfo, err := os.Stat(config.GameDatabase); err == nil {
		if metaInfo, err := os.Stat(filepath.Join(config.DataDirectory, "management.db")); err == nil && os.SameFile(gameInfo, metaInfo) {
			return nil, fmt.Errorf("游戏数据库与管理数据库不能使用同一个文件")
		}
	}
	host, _, err := net.SplitHostPort(config.Listen)
	if err != nil {
		return nil, fmt.Errorf("监听地址无效")
	}
	ip := net.ParseIP(host)
	if (ip == nil || !ip.IsLoopback()) && (config.TLSCertificate == "" || config.TLSKey == "") {
		return nil, fmt.Errorf("非本机监听必须配置TLS证书和私钥")
	}
	var store *stateStore
	if config.DirectDatabase {
		store, err = openDirectStore(config.GameDatabase)
	} else {
		store, err = openStore(config.DataDirectory)
	}
	if err != nil {
		return nil, err
	}
	if config.DirectDatabase {
		if err = initializeDirect(context.Background(), store); err != nil {
			store.db.Close()
			return nil, err
		}
	}
	game := &gameControl{endpointFile: config.ControlEndpointFile, client: &http.Client{Timeout: 4 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}
	s := &Service{config: config, store: store, reader: gameReader{database: config.GameDatabase, game: game}, game: game, started: time.Now(), active: map[string]bool{}}
	s.taskRunner = s.runTask
	s.systemRunner = s.runSystem
	s.shutdown = make(chan struct{})
	if config.DirectDatabase && config.EnableJobs {
		s.startJobs()
	}
	return s, nil
}
func (s *Service) Close() {
	if s.cancel != nil {
		s.cancel()
		<-s.done
	}
	_ = s.store.db.Close()
}
func WriteInitialConfig(path string) error {
	if _, err := os.Stat(path); err == nil {
		return fmt.Errorf("配置已存在，未覆盖")
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return err
	}
	config := Config{Listen: "127.0.0.1:17890", Token: hex.EncodeToString(key), DataDirectory: filepath.Join(filepath.Dir(path), "数据")}
	raw, _ := json.MarshalIndent(config, "", "  ")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	_, err = f.Write(raw)
	closeErr := f.Close()
	if err != nil {
		return err
	}
	return closeErr
}
func (s *Service) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		write(w, 200, adminapi.Envelope{OK: true, Data: map[string]any{"service": "go-gm-manager", "version": adminapi.Version, "pid": os.Getpid()}})
	})
	mux.HandleFunc("GET /manage/v1/status", s.status)
	mux.HandleFunc("GET /manage/v1/server", s.serverStatus)
	mux.HandleFunc("GET /manage/v1/pvf/releases", s.pvfReleases)
	mux.HandleFunc("POST /manage/v1/pvf/releases", s.publishPVF)
	mux.HandleFunc("GET /manage/v1/deployment", s.deploymentStatus)
	mux.HandleFunc("GET /manage/v1/jobs", s.jobsStatus)
	mux.HandleFunc("GET /manage/v1/deployment/binary", s.downloadBinary)
	mux.HandleFunc("GET /manage/v1/deployment/config", s.downloadConfig)
	mux.HandleFunc("GET /manage/v1/deployment/runtime", s.downloadRuntime)
	mux.HandleFunc("PUT /manage/v1/deployment/update", s.retiredUpdater)
	mux.HandleFunc("GET /manage/v1/database/snapshot", s.downloadSnapshot)
	mux.HandleFunc("GET /manage/v1/pvf/source", s.downloadPVF)
	mux.HandleFunc("POST /manage/v1/server", s.serverAction)
	mux.HandleFunc("GET /manage/v1/accounts", s.accounts)
	mux.HandleFunc("GET /manage/v1/characters", s.characters)
	mux.HandleFunc("GET /manage/v1/config/{section}", s.getConfig)
	mux.HandleFunc("PUT /manage/v1/config/{section}", s.putConfig)
	mux.HandleFunc("POST /manage/v1/native/coupon", s.nativeCoupon)
	mux.HandleFunc("GET /manage/v1/codes", s.codes)
	mux.HandleFunc("POST /manage/v1/codes", s.createCodes)
	mux.HandleFunc("POST /manage/v1/codes/state", s.codeState)
	mux.HandleFunc("POST /manage/v1/codes/delete", s.deleteCode)
	mux.HandleFunc("POST /manage/v1/announcements", s.sendAnnouncement)
	mux.HandleFunc("GET /manage/v1/audit", s.audit)
	mux.HandleFunc("GET /manage/v1/records", s.records)
	mux.HandleFunc("GET /manage/v1/records/cleanup", s.previewRecordCleanup)
	mux.HandleFunc("POST /manage/v1/records/cleanup", s.cleanRecords)
	mux.HandleFunc("POST /manage/v1/actions", s.action)
	mux.HandleFunc("POST /manage/v1/preview", s.directPreview)
	mux.HandleFunc("GET /manage/v1/receipts/{id}", s.receipt)
	mux.HandleFunc("GET /manage/v1/launcher/patches", s.launcherAdminPatches)
	mux.HandleFunc("POST /manage/v1/launcher/patches/upload", s.launcherAdminUpload)
	mux.HandleFunc("POST /manage/v1/launcher/patches/publish", s.launcherAdminPublish)
	mux.HandleFunc("POST /manage/v1/launcher/resource-signatures", s.launcherAdminSign)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if r.URL.Path != "/health" {
			token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			if !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") || subtle.ConstantTimeCompare([]byte(token), []byte(s.config.Token)) != 1 {
				writeError(w, fail(401, "unauthorized", "管理认证失败"), "")
				return
			}
		}
		mux.ServeHTTP(w, r)
	})
}
func write(w http.ResponseWriter, status int, body adminapi.Envelope) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
func writeError(w http.ResponseWriter, err error, id string) {
	a := &apiFailure{Status: 500, Code: "internal_error", Message: "管理服务未完成请求，请核对日志"}
	_ = errors.As(err, &a)
	write(w, a.Status, adminapi.Envelope{OK: false, Error: &adminapi.Error{Code: a.Code, Message: a.Message, Unknown: a.Unknown}, RequestID: id})
}
func decode(w http.ResponseWriter, r *http.Request, out any) error {
	r.Body = http.MaxBytesReader(w, r.Body, 2<<20)
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	if err := d.Decode(out); err != nil {
		return fail(400, "invalid_json", "请求格式错误或含有不支持的字段")
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return fail(400, "invalid_json", "请求必须只包含一个JSON对象")
	}
	return nil
}
func pageParams(r *http.Request) (int, int, error) {
	p, size := 1, 50
	var err error
	if value := r.URL.Query().Get("page"); value != "" {
		p, err = strconv.Atoi(value)
		if err != nil || p < 1 || p > 1000000 {
			return 0, 0, fail(400, "invalid_page", "页码无效")
		}
	}
	if value := r.URL.Query().Get("page_size"); value != "" {
		size, err = strconv.Atoi(value)
		if err != nil || size < 1 || size > 200 {
			return 0, 0, fail(400, "invalid_page", "每页数量须为1..200")
		}
	}
	return p, size, nil
}
func paginate[T any](items []T, p, size int) adminapi.Page[T] {
	total := len(items)
	start := min((p-1)*size, total)
	end := min(start+size, total)
	return adminapi.Page[T]{Items: items[start:end], Total: total, Page: p, PageSize: size}
}
func keywordMatch(q string, values ...string) bool {
	q = strings.ToLower(strings.TrimSpace(q))
	for _, v := range values {
		if strings.Contains(strings.ToLower(v), q) {
			return true
		}
	}
	return false
}
func (s *Service) mutate(w http.ResponseWriter, r *http.Request, body any, module, action, target string, fn func(*sql.Tx) (any, error)) {
	s.mutateAfter(w, r, body, module, action, target, fn, nil)
}
func (s *Service) mutateAfter(w http.ResponseWriter, r *http.Request, body any, module, action, target string, fn func(*sql.Tx) (any, error), after func(any)) {
	id := r.Header.Get("Idempotency-Key")
	if len(id) < 1 || len(id) > 128 || strings.ContainsAny(id, "\r\n\x00") {
		writeError(w, fail(400, "request_id_required", "操作必须带有效请求编号"), id)
		return
	}
	raw, _ := json.Marshal(body)
	sum := sha256.Sum256(append([]byte(r.Method+" "+r.URL.Path+"\n"), raw...))
	fingerprint := hex.EncodeToString(sum[:])
	var reply adminapi.Envelope
	status := 200
	err := s.store.transaction(r.Context(), func(tx *sql.Tx) error {
		prior, ok, err := readRequest(r.Context(), tx, id)
		if err != nil {
			return err
		}
		if ok {
			if prior.Fingerprint != fingerprint {
				return fail(409, "request_conflict", "请求编号已用于其他内容")
			}
			reply, status = prior.Reply, prior.Status
			return nil
		}
		value, err := fn(tx)
		if err != nil {
			return err
		}
		reply = adminapi.Envelope{OK: true, Data: value, RequestID: id}
		if err = saveRequest(r.Context(), tx, id, savedRequest{Fingerprint: fingerprint, Status: 200, Reply: reply}, false); err != nil {
			return err
		}
		if err = appendAudit(r.Context(), tx, module, action, target, "管理服务已保存", id, "数据库提交；游戏客户端刷新状态由回执单独说明"); err != nil {
			return err
		}
		if s.config.DirectDatabase {
			_, err = tx.ExecContext(r.Context(), "INSERT INTO gmkit_audit(action,summary,actor,request_id,outcome,created_at) VALUES(?,?,?,?,?,?)", action, target, "go-gm-manager", id, "database_committed", gameStamp())
		}
		return err
	})
	if err != nil {
		writeError(w, err, id)
		return
	}
	if after != nil && reply.OK {
		after(reply.Data)
	}
	write(w, status, reply)
}
func (s *Service) status(w http.ResponseWriter, r *http.Request) {
	var memory runtime.MemStats
	runtime.ReadMemStats(&memory)
	hostname, _ := os.Hostname()
	result := adminapi.Status{Version: adminapi.Version, Language: "go", OS: runtime.GOOS + "/" + runtime.GOARCH, Hostname: hostname, UptimeSeconds: int64(time.Since(s.started).Seconds()), MemoryBytes: memory.Alloc, Goroutines: runtime.NumGoroutine(), PID: os.Getpid(), ServerRoot: s.config.ServerRoot, Database: s.config.GameDatabase, DatabaseState: "unconfigured", GameState: "unknown", Ports: []adminapi.Port{}, CheckedAt: time.Now().UTC(), Capabilities: []string{"status", "config", "codes", "audit", "accounts.query", "characters.query", "records.query"}}
	if s.config.DirectDatabase {
		result.WriteMode = "direct_sqlite"
		result.Capabilities = append(result.Capabilities, "database.direct", "currency.credit", "mail.send", "account.ban", "account.unban", "codes.game_database", "records.cleanup")
	}
	if s.config.GameDatabase != "" {
		db, err := s.reader.open()
		if err == nil {
			err = db.PingContext(r.Context())
			db.Close()
		}
		if err == nil {
			result.DatabaseState = "read_only_connected"
			if s.config.DirectDatabase {
				result.DatabaseState = "read_write_connected"
			}
		} else {
			result.DatabaseState = "unavailable"
		}
	}
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	session, err := s.game.session(ctx)
	if err == nil && session.Complete {
		a, c := map[int]bool{}, map[int]bool{}
		for _, row := range session.Connections {
			a[row.AccountID] = true
			if row.CharacterID > 0 {
				c[row.CharacterID] = true
			}
		}
		ac, cc := len(a), len(c)
		result.OnlineAccounts = &ac
		result.OnlineCharacters = &cc
		result.GameState = "connected"
		result.Capabilities = append(result.Capabilities, "currency.credit.online")
	}
	for _, port := range s.config.ServicePorts {
		port.Reachable = "not_checked"
		port.Listening = "unknown"
		if port.Protocol == "tcp" && port.Port > 0 && port.Port <= 65535 {
			conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port.Port)), 150*time.Millisecond)
			if err == nil {
				port.Listening = "listening"
				conn.Close()
			} else {
				port.Listening = "unreachable"
			}
		}
		result.Ports = append(result.Ports, port)
	}
	write(w, 200, adminapi.Envelope{OK: true, Data: result})
}
