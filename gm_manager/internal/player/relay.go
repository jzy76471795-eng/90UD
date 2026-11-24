package player

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net"
	"sort"
	"strconv"
	"sync"
	"time"
)

type credential struct{ SessionID, Token string }

func NativeProof(conn net.Conn, c credential) error {
	sid, e := hex.DecodeString(c.SessionID)
	if e != nil || len(sid) != 16 || c.Token == "" {
		return errors.New("原生会话无效")
	}
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	defer conn.SetDeadline(time.Time{})
	prefix := append([]byte("PY90NT2\n"), sid...)
	if e = writeFull(conn, prefix); e != nil {
		return e
	}
	nonce := make([]byte, 32)
	if _, e = io.ReadFull(conn, nonce); e != nil {
		return e
	}
	key := sha256.Sum256([]byte(c.Token))
	mac := hmac.New(sha256.New, key[:])
	mac.Write(prefix)
	mac.Write(nonce)
	proof := mac.Sum(nil)
	defer clear(proof)
	defer clear(key[:])
	return writeFull(conn, proof)
}
func writeFull(w io.Writer, b []byte) error {
	for len(b) > 0 {
		n, e := w.Write(b)
		if e != nil {
			return e
		}
		if n == 0 {
			return io.ErrShortWrite
		}
		b = b[n:]
	}
	return nil
}

type Relay struct {
	listener    net.Listener
	host        string
	port        int
	resolve     func(*net.TCPConn) (credential, error)
	ctx         context.Context
	cancel      context.CancelFunc
	mu          sync.Mutex
	connections map[net.Conn]bool
}

func newRelay(host string, port, localPort int, resolve func(*net.TCPConn) (credential, error)) (*Relay, error) {
	l, e := net.Listen("tcp4", net.JoinHostPort("127.0.0.1", strconv.Itoa(localPort)))
	if e != nil {
		return nil, e
	}
	ctx, cancel := context.WithCancel(context.Background())
	r := &Relay{listener: l, host: host, port: port, resolve: resolve, ctx: ctx, cancel: cancel, connections: map[net.Conn]bool{}}
	go r.accept()
	return r, nil
}
func (r *Relay) accept() {
	for {
		conn, e := r.listener.Accept()
		if e != nil {
			return
		}
		r.mu.Lock()
		if len(r.connections) >= 32 || r.ctx.Err() != nil {
			r.mu.Unlock()
			conn.Close()
			continue
		}
		r.connections[conn] = true
		r.mu.Unlock()
		go r.handle(conn.(*net.TCPConn))
	}
}
func (r *Relay) handle(local *net.TCPConn) {
	defer func() { local.Close(); r.mu.Lock(); delete(r.connections, local); r.mu.Unlock() }()
	var c credential
	var e error
	if r.resolve != nil {
		c, e = r.resolve(local)
		if e != nil {
			return
		}
	}
	ctx, cancel := context.WithTimeout(r.ctx, 5*time.Second)
	defer cancel()
	remote, e := (&net.Dialer{}).DialContext(ctx, "tcp4", net.JoinHostPort(r.host, strconv.Itoa(r.port)))
	if e != nil {
		return
	}
	defer remote.Close()
	r.mu.Lock()
	if r.ctx.Err() != nil {
		r.mu.Unlock()
		return
	}
	r.connections[remote] = true
	r.mu.Unlock()
	defer func() { r.mu.Lock(); delete(r.connections, remote); r.mu.Unlock() }()
	if r.resolve != nil {
		if NativeProof(remote, c) != nil {
			return
		}
	}
	local.SetNoDelay(true)
	remote.(*net.TCPConn).SetNoDelay(true)
	done := make(chan struct{}, 2)
	copyStream := func(to, from *net.TCPConn) {
		_, e := io.Copy(to, from)
		if e != nil {
			to.Close()
			from.Close()
		} else {
			to.CloseWrite()
		}
		done <- struct{}{}
	}
	go copyStream(remote.(*net.TCPConn), local)
	go copyStream(local, remote.(*net.TCPConn))
	<-done
	<-done
}
func (r *Relay) Close() {
	r.cancel()
	r.listener.Close()
	r.mu.Lock()
	defer r.mu.Unlock()
	for c := range r.connections {
		c.Close()
	}
}

type RoutePool struct {
	mu         sync.Mutex
	relays     []*Relay
	leases     map[*RouteLease]bool
	host       string
	ports      []int
	LaunchGate sync.Mutex
}
type RouteLease struct {
	pool    *RoutePool
	game    *Game
	session Session
	once    sync.Once
}

func (p *RoutePool) Acquire(session Session) (*RouteLease, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	s := session.Stage
	if e := s.Validate(); e != nil {
		return nil, e
	}
	if p.leases == nil {
		p.leases = map[*RouteLease]bool{}
	}
	if len(p.leases) >= 2 {
		return nil, errors.New("同一登录器最多运行两个游戏账号")
	}
	ports := []int{7001}
	if s.NativeTunnelVersion == 2 {
		if net.ParseIP(s.ChannelHost).IsLoopback() {
			return nil, errors.New("认证转发不能占用本机服务端相同端口")
		}
		ports = append(ports, s.GamePorts...)
	}
	sort.Ints(ports)
	if len(p.leases) > 0 {
		if p.host != s.ChannelHost || len(p.ports) != len(ports) {
			return nil, errors.New("现有游戏连接配置不同")
		}
		for i := range ports {
			if ports[i] != p.ports[i] {
				return nil, errors.New("现有游戏端口不同")
			}
		}
	} else {
		var relays []*Relay
		for _, port := range ports {
			if s.NativeTunnelVersion == 0 && net.ParseIP(s.ChannelHost).IsLoopback() {
				continue
			}
			var resolve func(*net.TCPConn) (credential, error)
			if s.NativeTunnelVersion == 2 {
				resolve = p.resolve
			}
			r, e := newRelay(s.ChannelHost, port, port, resolve)
			if e != nil {
				for _, v := range relays {
					v.Close()
				}
				return nil, errors.New("本机游戏端口被占用，请先退出其他登录器")
			}
			relays = append(relays, r)
		}
		p.relays = relays
		p.host = s.ChannelHost
		p.ports = ports
	}
	lease := &RouteLease{pool: p, session: session}
	p.leases[lease] = true
	return lease, nil
}
func (l *RouteLease) Bind(g *Game) error {
	l.pool.mu.Lock()
	defer l.pool.mu.Unlock()
	if !l.pool.leases[l] || l.game != nil {
		return errors.New("游戏连接归属无效")
	}
	l.game = g
	return nil
}
func (l *RouteLease) Close() {
	l.once.Do(func() {
		p := l.pool
		p.mu.Lock()
		defer p.mu.Unlock()
		delete(p.leases, l)
		if len(p.leases) == 0 {
			for _, r := range p.relays {
				r.Close()
			}
			p.relays = nil
			p.host = ""
			p.ports = nil
		}
	})
}
func (p *RoutePool) resolve(c *net.TCPConn) (credential, error) {
	peer := c.RemoteAddr().(*net.TCPAddr)
	local := c.LocalAddr().(*net.TCPAddr)
	pid, e := TCPPeerPID(peer.Port, local.Port)
	if e != nil {
		return credential{}, e
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	for l := range p.leases {
		if l.game != nil && l.game.PID == pid && l.game.Alive() {
			return credential{l.session.SessionID, l.session.AccessToken}, nil
		}
	}
	return credential{}, errors.New("本机连接不属于已认证游戏进程")
}
