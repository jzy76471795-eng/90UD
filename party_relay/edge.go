package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/gorilla/websocket"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type edgeStartup struct {
	Grant         sessionGrant `json:"grant"`
	Relay         string       `json:"relay_url"`
	CA            string       `json:"ca_file"`
	Pipe          string       `json:"pipe_name"`
	Parent        uint32       `json:"parent_pid"`
	ParentCreated uint64       `json:"parent_created"`
	ParentIPC     bool         `json:"parent_native_ipc"`
}
type remoteView struct {
	Boot      string       `json:"boot"`
	Member    uint32       `json:"member_id"`
	Binding   binding      `json:"binding"`
	Version   uint64       `json:"route_view"`
	Active    bool         `json:"active"`
	Peers     []localRoute `json:"peers"`
	Lease     int64        `json:"lease_ms"`
	UDP       string       `json:"udp_endpoint"`
	Transport pathView     `json:"transport"`
}
type probeSent struct {
	Size int
	At   time.Time
	UDP  bool
}
type edgeRoute struct {
	Route      localRoute
	Conn       *net.UDPConn
	NativePort uint16
	Context    uint32
	Actor      uint32
}
type retiredRoute struct {
	Node *edgeRoute
	At   time.Time
}
type edgeClient struct {
	cfg                                        edgeStartup
	conn                                       *websocket.Conn
	http                                       *http.Client
	offset                                     time.Duration
	secret                                     []byte
	up, down                                   *Codec
	data, control                              *Queue
	mu                                         sync.RWMutex
	native                                     nativeReport
	remote                                     remoteView
	deadline                                   time.Time
	nodes                                      map[uint32]*edgeRoute
	pending                                    map[[32]byte]probeSent
	verified                                   map[int]time.Time
	wssVerified                                map[int]time.Time
	pathFailed                                 bool
	nativeAt                                   time.Time
	published                                  uint64
	activeVersion                              uint64
	retired                                    []retiredRoute
	udp                                        *edgeUDP
	sent, received, rejected, failed           atomic.Uint64
	nativeDropped, nativePartial, probeDropped atomic.Uint64
}

func edgeTLS(path string) (*tls.Config, error) {
	pem, e := os.ReadFile(path)
	if e != nil {
		return nil, errors.New("trusted CA unavailable")
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return nil, errors.New("invalid trusted CA")
	}
	return &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}, nil
}
func dialEdge(ctx context.Context, cfg edgeStartup) (*edgeClient, error) {
	base, e := url.Parse(cfg.Relay)
	if e != nil || base.Scheme != "https" || base.Host == "" || base.User != nil || base.Path != "" && base.Path != "/" || base.RawQuery != "" || base.Fragment != "" {
		return nil, errors.New("invalid relay TLS endpoint")
	}
	if cfg.Grant.Member == 0 || cfg.Grant.Session == 0 || cfg.Grant.MTU < 512 || cfg.Grant.MTU > 1400 || len(cfg.Grant.Token) != 64 || len(cfg.Grant.Boot) != 32 {
		return nil, errors.New("invalid single-use startup")
	}
	secret, e := hex.DecodeString(cfg.Grant.Secret)
	if e != nil || len(secret) != 32 {
		return nil, errors.New("invalid session key")
	}
	tlsConfig, e := edgeTLS(cfg.CA)
	if e != nil {
		return nil, e
	}
	client := &http.Client{Transport: &http.Transport{TLSClientConfig: tlsConfig, MaxIdleConnsPerHost: 2, IdleConnTimeout: 15 * time.Second}, Timeout: 2 * time.Second}
	at := time.Now()
	request, _ := http.NewRequestWithContext(ctx, "GET", strings.TrimRight(cfg.Relay, "/")+"/v1/clock", nil)
	response, e := client.Do(request)
	if e != nil {
		return nil, errors.New("relay TLS clock unavailable")
	}
	body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
	response.Body.Close()
	var clock struct {
		Now int64 `json:"server_ms"`
	}
	if response.StatusCode != 200 || strictJSON(body, &clock) != nil || clock.Now <= 0 {
		return nil, errors.New("relay clock authentication failed")
	}
	offset := time.UnixMilli(clock.Now).Sub(at.Add(time.Since(at) / 2))
	endpoint := *base
	endpoint.Scheme = "wss"
	endpoint.Path = "/v1/edge"
	headers := http.Header{}
	headers.Set("Authorization", "Bearer "+cfg.Grant.Token)
	dial := websocket.Dialer{TLSClientConfig: tlsConfig, Subprotocols: []string{"90us-rlv2-v1"}, HandshakeTimeout: 5 * time.Second, ReadBufferSize: cfg.Grant.MTU, WriteBufferSize: cfg.Grant.MTU}
	connection, resp, e := dial.DialContext(ctx, endpoint.String(), headers)
	if resp != nil && resp.Body != nil {
		resp.Body.Close()
	}
	if e != nil {
		return nil, errors.New("single-use edge connection failed")
	}
	connection.SetReadLimit(int64(cfg.Grant.MTU))
	label := fmt.Sprintf("product/boot-%s/member-%d", cfg.Grant.Boot, cfg.Grant.Member)
	budget := &Budget{limit: 512 * 1024}
	edge := &edgeClient{cfg: cfg, conn: connection, http: client, offset: offset, secret: secret, up: newCodec(secret, label+"/to-relay", cfg.Grant.Session, 1, cfg.Grant.MTU, budget), down: newCodec(secret, label+"/from-relay", cfg.Grant.Session, 1, cfg.Grant.MTU, budget), data: newBudgetQueue(&Budget{limit: 256 * 1024}), control: newControlQueue(&Budget{limit: controlQueueBytes}), nodes: map[uint32]*edgeRoute{}, pending: map[[32]byte]probeSent{}, verified: map[int]time.Time{}}
	edge.udp = &edgeUDP{verified: map[int]time.Time{}}
	edge.wssVerified = map[int]time.Time{}
	return edge, nil
}
func (e *edgeClient) rpc(ctx context.Context, method, path string, payload any, result any) error {
	var body []byte
	if payload != nil {
		body, _ = json.Marshal(payload)
	}
	request, _ := http.NewRequestWithContext(ctx, method, strings.TrimRight(e.cfg.Relay, "/")+path, bytes.NewReader(body))
	signRequestAt(request, body, derive(e.secret, "product/edge-http"), "edge-v1", time.Now().Add(e.offset))
	request.Header.Set("X-Relay-Member", fmt.Sprint(e.cfg.Grant.Member))
	request.Header.Set("X-Relay-Session", fmt.Sprint(e.cfg.Grant.Session))
	response, err := e.http.Do(request)
	if err != nil {
		return errors.New("edge control unavailable")
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, 64*1024+1))
	if err != nil || len(raw) > 64*1024 || response.StatusCode != 200 {
		return errors.New("edge control refused")
	}
	if result != nil && strictJSON(raw, result) != nil {
		return errors.New("invalid edge control response")
	}
	return nil
}
func (e *edgeClient) permits(h Header) bool {
	if isSessionProbe(h, e.cfg.Grant.Member) {
		return true
	}
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.permitsLocked(h)
}
func (e *edgeClient) permitsLocked(h Header) bool {
	if h.Kind != 1 || e.pathFailed || e.remote.Transport.Failed || e.failed.Load() != 0 || e.published == 0 || e.native.Installed < e.activeVersion || e.native.Installed > e.published || !e.remote.Active || time.Now().After(e.deadline) || time.Since(e.nativeAt) > time.Second || e.native.Port == 0 || e.native.Actor != e.remote.Binding.Actor || e.native.Context == 0 {
		return false
	}
	peer := h.Source
	if peer == e.cfg.Grant.Member {
		peer = h.Target
	} else if h.Target != e.cfg.Grant.Member {
		return false
	}
	n := e.nodes[peer]
	return n != nil && n.Context == e.native.Context && n.Actor == e.native.Actor && n.NativePort == e.native.Port && n.Route.Scope == h.Scope && n.Route.Epoch == h.Epoch
}
func (e *edgeClient) nativeExchange(native nativeReport) localView {
	e.mu.Lock()
	changed := native.Context != e.native.Context || native.Actor != e.native.Actor || native.Socket != e.native.Socket || native.Port != e.native.Port
	old := []*edgeRoute{}
	if changed {
		for _, n := range e.nodes {
			old = append(old, n)
		}
		e.nodes = map[uint32]*edgeRoute{}
		e.deadline = time.Time{}
		e.published = 0
		e.activeVersion = 0
	}
	e.native = native
	e.nativeAt = time.Now()
	routes := []localRoute{}
	until := uint64(0)
	if e.published != 0 && !e.pathFailed && !e.remote.Transport.Failed && e.failed.Load() == 0 && e.remote.Active && time.Now().Before(e.deadline) && native.Actor == e.remote.Binding.Actor && native.Context != 0 {
		until = ticks() + uint64(max(0, time.Until(e.deadline).Milliseconds()))
		for _, n := range e.nodes {
			routes = append(routes, n.Route)
		}
	}
	view := localView{Context: native.Context, Version: e.published, Until: until, Required: true, Routes: routes}
	e.mu.Unlock()
	if changed {
		e.data.clear()
		e.down.clear()
	}
	e.retire(old)
	return view
}
func (e *edgeClient) installRemote(ctx context.Context, v remoteView, failures chan<- error) error {
	if v.Boot != e.cfg.Grant.Boot || v.Member != e.cfg.Grant.Member || len(v.Peers) > 3 || v.Lease < 1 || v.Lease > 1000 {
		return errors.New("relay view binding mismatch")
	}
	e.mu.RLock()
	native := e.native
	oldRemote := e.remote
	current := map[uint32]*edgeRoute{}
	for id, n := range e.nodes {
		current[id] = n
	}
	e.mu.RUnlock()
	if v.Version < oldRemote.Version {
		return errors.New("relay view rolled back")
	}
	desired := map[uint32]*edgeRoute{}
	fresh := []*edgeRoute{}
	closeFresh := func() {
		for _, n := range fresh {
			n.Conn.Close()
		}
	}
	enabled := v.Active && native.Actor == v.Binding.Actor && native.Actor != 0 && native.Context != 0 && native.Port != 0 && native.Socket != 0 && native.Socket != ^uint32(0)
	if enabled {
		for _, p := range v.Peers {
			if p.Member == e.cfg.Grant.Member || desired[p.Member] != nil {
				closeFresh()
				return errors.New("duplicate local peer")
			}
			base := p
			base.Local = 0
			if old := current[p.Member]; old != nil && old.Context == native.Context && old.NativePort == native.Port && old.Actor == native.Actor {
				prior := old.Route
				prior.Local = 0
				if reflect.DeepEqual(prior, base) {
					desired[p.Member] = old
					continue
				}
			}
			conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
			if err != nil {
				closeFresh()
				return err
			}
			p.Local = uint16(conn.LocalAddr().(*net.UDPAddr).Port)
			node := &edgeRoute{Route: p, Conn: conn, NativePort: native.Port, Context: native.Context, Actor: native.Actor}
			desired[p.Member] = node
			fresh = append(fresh, node)
		}
	}
	validation := localView{Game: gameIdentity{PID: 1, Created: 1}, Context: native.Context, Version: v.Version, Until: 1, Required: true}
	for _, n := range desired {
		validation.Routes = append(validation.Routes, n.Route)
	}
	if _, err := encodeLocalView(validation); err != nil {
		closeFresh()
		return err
	}
	e.mu.Lock()
	if e.native.Context != native.Context || e.native.Actor != native.Actor || e.native.Port != native.Port || e.native.Socket != native.Socket {
		e.mu.Unlock()
		closeFresh()
		return nil
	}
	old := e.nodes
	changed := len(old) != len(desired)
	for id, n := range old {
		if desired[id] != n {
			changed = true
		}
	}
	e.nodes = desired
	if v.Transport.Scope != e.remote.Transport.Scope || v.Transport.Epoch != e.remote.Transport.Epoch {
		e.pathFailed = false
	}
	e.remote = v
	e.deadline = time.Now().Add(time.Duration(v.Lease) * time.Millisecond)
	if changed {
		e.published = 0
		e.activeVersion = v.Version
	}
	e.mu.Unlock()
	if changed {
		e.data.clear()
		e.down.clear()
	}
	e.mu.Lock()
	if e.native.Context == native.Context && e.native.Actor == native.Actor && e.native.Port == native.Port && e.remote.Version == v.Version && (!v.Active || enabled) {
		e.published = v.Version
	}
	e.mu.Unlock()
	removed := []*edgeRoute{}
	for id, n := range old {
		if desired[id] != n {
			removed = append(removed, n)
		}
	}
	e.retire(removed)
	for _, n := range fresh {
		go func(n *edgeRoute) {
			if err := e.readNative(ctx, n); err != nil {
				select {
				case failures <- err:
				case <-ctx.Done():
				}
			}
		}(n)
	}
	return nil
}
func (e *edgeClient) readNative(ctx context.Context, n *edgeRoute) error {
	buffer := make([]byte, maxNative+1)
	for ctx.Err() == nil {
		n.Conn.SetReadDeadline(time.Now().Add(250 * time.Millisecond))
		size, source, err := n.Conn.ReadFromUDP(buffer)
		if err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue
			}
			e.mu.RLock()
			current := e.nodes[n.Route.Member] == n
			e.mu.RUnlock()
			if !current {
				return nil
			}
			return errors.New("native route socket closed")
		}
		if size < 1 || size > maxNative || !source.IP.Equal(net.IPv4(127, 0, 0, 1)) || source.Port != int(n.NativePort) {
			e.rejected.Add(1)
			continue
		}
		h := Header{Kind: 1, Source: e.cfg.Grant.Member, Target: n.Route.Member, Scope: n.Route.Scope, Epoch: n.Route.Epoch}
		e.mu.RLock()
		valid := e.nodes[n.Route.Member] == n && e.permitsLocked(h)
		e.mu.RUnlock()
		if !valid {
			e.rejected.Add(1)
			continue
		}
		if !e.data.put(Packet{H: h, Data: append([]byte{}, buffer[:size]...), At: time.Now()}) {
			e.failed.Add(1)
			return errors.New("edge upstream congestion")
		}
	}
	return ctx.Err()
}
func (e *edgeClient) reader(ctx context.Context) error {
	for ctx.Err() == nil {
		e.conn.SetReadDeadline(time.Now().Add(15 * time.Second))
		kind, b, err := e.conn.ReadMessage()
		if err != nil {
			return errors.New("edge transport closed")
		}
		if kind != websocket.BinaryMessage {
			return errors.New("unexpected edge message")
		}
		if err = e.acceptFrame(b, false); err != nil {
			return err
		}
	}
	return ctx.Err()
}
func (e *edgeClient) acceptFrame(b []byte, udp bool) error {
	h, p, err := e.down.decode(b, time.Now(), func(h Header) bool {
		if isSessionProbe(h, e.cfg.Grant.Member) {
			return true
		}
		e.mu.RLock()
		defer e.mu.RUnlock()
		return e.permitsLocked(h) && ((e.remote.Transport.Mode == "udp") == udp)
	})
	if err != nil {
		e.rejected.Add(1)
		return nil
	}
	if p == nil {
		return nil
	}
	if isSessionProbe(h, e.cfg.Grant.Member) {
		hash := sha256.Sum256(p)
		e.mu.Lock()
		sent, ok := e.pending[hash]
		if ok && sent.UDP == udp && sent.Size == len(p) && time.Since(sent.At) < 2*time.Second {
			e.verified[len(p)] = time.Now()
			if !udp {
				e.wssVerified[len(p)] = time.Now()
			}
			delete(e.pending, hash)
			if udp && e.udp != nil {
				e.udp.mu.Lock()
				e.udp.verified[len(p)] = time.Now()
				e.udp.mu.Unlock()
			}
		} else {
			e.rejected.Add(1)
		}
		e.mu.Unlock()
		return nil
	}
	e.mu.RLock()
	node := e.nodes[h.Source]
	valid := node != nil && e.permitsLocked(h)
	if valid {
		node.Conn.SetWriteDeadline(time.Now().Add(100 * time.Millisecond))
		_, err = node.Conn.WriteToUDP(p, &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: int(e.native.Port)})
	}
	e.mu.RUnlock()
	if !valid {
		e.rejected.Add(1)
		return nil
	}
	if err != nil {
		return errors.New("native delivery failed")
	}
	e.received.Add(1)
	return nil
}
func (e *edgeClient) writer(ctx context.Context) error {
	rate := Rate{}
	for ctx.Err() == nil {
		p, ok := e.control.pop()
		if !ok {
			p, ok = e.data.pop()
		}
		if !ok {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-e.control.wake:
				continue
			case <-e.data.wake:
				continue
			case <-time.After(time.Second):
				continue
			}
		}
		if time.Since(p.At) >= 100*time.Millisecond || !rate.allow(len(p.Data), time.Now()) {
			if isSessionProbe(p.H, e.cfg.Grant.Member) {
				e.dropProbe(p)
				continue
			}
			return errors.New("bounded edge upstream failed")
		}
		native := p.H.Kind == 1
		// Keep the route read lock across writes to preserve the retirement fence.
		// A path error is committed after releasing it, then reported over TLS.
		mode := "wss"
		if native {
			e.mu.RLock()
			if !e.permitsLocked(p.H) {
				e.mu.RUnlock()
				e.rejected.Add(1)
				continue
			}
			mode = e.remote.Transport.Mode
		}
		udp := (*net.UDPConn)(nil)
		if p.UDP || native && mode == "udp" {
			udp = e.udpDestination(p.UDP)
		}
		pathError := (p.UDP || native && mode == "udp") && udp == nil
		written := 0
		var err error
		if !pathError {
			var frames [][]byte
			frames, err = e.up.encodeKind(p.H.Source, p.H.Target, p.H.Scope, p.H.Epoch, p.Data, p.H.Kind)
			if err == nil {
				deadline := time.Now().Add(100 * time.Millisecond)
				if udp != nil {
					udp.SetWriteDeadline(deadline)
				} else {
					e.conn.SetWriteDeadline(deadline)
				}
				for _, b := range frames {
					if time.Now().After(deadline) {
						err = errors.New("native datagram write deadline")
						pathError = udp != nil
						break
					}
					if udp != nil {
						var n int
						n, err = udp.Write(b)
						if err == nil && n != len(b) {
							err = io.ErrShortWrite
						}
						if err != nil {
							pathError = true
							break
						}
					} else {
						if err = e.conn.WriteMessage(websocket.BinaryMessage, b); err != nil {
							break
						}
					}
					written++
				}
			}
		}
		if native {
			e.mu.RUnlock()
		}
		if pathError || err != nil {
			if native {
				e.nativeDropped.Add(1)
				if written > 0 {
					e.nativePartial.Add(1)
				}
				if pathError {
					e.mu.Lock()
					if e.remote.Transport.Scope == p.H.Scope && e.remote.Transport.Epoch == p.H.Epoch {
						e.failTransportLocked()
					}
					e.mu.Unlock()
				}
			} else {
				e.probeDropped.Add(1)
			}
			if pathError {
				continue
			}
			return errors.New("edge write deadline failed")
		}
		if native {
			e.sent.Add(1)
		}
	}
	return ctx.Err()
}
func (e *edgeClient) probe() error {
	return e.probeTransport(false)
}

func (e *edgeClient) dropProbe(p Packet) {
	e.mu.Lock()
	delete(e.pending, sha256.Sum256(p.Data))
	e.mu.Unlock()
	e.probeDropped.Add(1)
}

func (e *edgeClient) probeTransport(udp bool) error {
	if e.control.failed() {
		return errors.New("probe queue closed")
	}
	now := time.Now()
	e.mu.Lock()
	for hash, p := range e.pending {
		if now.Sub(p.At) >= 2*time.Second {
			delete(e.pending, hash)
		}
	}
	e.mu.Unlock()
	for _, size := range []int{64, 512, 8192} {
		p := make([]byte, size)
		if _, err := rand.Read(p); err != nil {
			return err
		}
		hash := sha256.Sum256(p)
		e.mu.Lock()
		if len(e.pending) >= 18 {
			e.mu.Unlock()
			e.probeDropped.Add(1)
			continue
		}
		e.pending[hash] = probeSent{Size: size, At: now, UDP: udp}
		e.mu.Unlock()
		packet := Packet{H: Header{Kind: 2, Source: e.cfg.Grant.Member, Target: e.cfg.Grant.Member, Scope: ^uint64(0), Epoch: 1}, Data: p, At: now, UDP: udp}
		if !e.control.putProbe(packet) {
			e.dropProbe(packet)
			if e.control.failed() {
				return errors.New("probe queue closed")
			}
		}
	}
	return nil
}
func (e *edgeClient) poll(ctx context.Context, failures chan<- error) error {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	lastProbe := time.Time{}
	for ctx.Err() == nil {
		e.reap()
		if e.failed.Load() != 0 {
			return errors.New("native retirement bound")
		}
		if time.Since(lastProbe) >= time.Second {
			if err := e.probe(); err != nil {
				return err
			}
			lastProbe = time.Now()
		}
		e.mu.RLock()
		failedPath := e.pathFailed
		path := e.remote.Transport
		e.mu.RUnlock()
		if failedPath && path.Scope != 0 && path.Revision != 0 && !path.Failed {
			_ = e.rpc(ctx, "POST", "/v1/edge/transport-failed", path, nil)
		}
		var view remoteView
		if err := e.rpc(ctx, "GET", "/v1/edge/state", nil, &view); err == nil {
			if err = e.installRemote(ctx, view, failures); err != nil {
				return err
			}
			e.udpStep(ctx, view.UDP)
		}
		e.mu.RLock()
		report := e.native
		report.Verified = map[int]int64{}
		verified := e.verified
		if e.remote.Active && e.remote.Transport.Mode == "wss" {
			verified = e.wssVerified
		}
		if e.remote.Active && e.remote.Transport.Mode == "udp" && e.udp != nil {
			e.udp.mu.Lock()
			verified = map[int]time.Time{}
			for size, at := range e.udp.verified {
				verified[size] = at
			}
			e.udp.mu.Unlock()
			if e.udpDestination(false) == nil {
				verified = map[int]time.Time{}
			}
		}
		for size, at := range verified {
			if age := time.Since(at).Milliseconds(); age >= 0 && age <= 2000 {
				report.Verified[size] = age
			}
		}
		e.mu.RUnlock()
		if report.Installed <= view.Version {
			_ = e.rpc(ctx, "POST", "/v1/edge/report", report, nil)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
	return ctx.Err()
}
func (e *edgeClient) close() {
	e.resetUDP()
	e.conn.Close()
	e.data.fail()
	e.control.fail()
	e.mu.Lock()
	nodes := e.nodes
	retired := e.retired
	e.retired = nil
	e.nodes = map[uint32]*edgeRoute{}
	e.deadline = time.Time{}
	e.mu.Unlock()
	for _, n := range nodes {
		n.Conn.Close()
	}
	for _, n := range retired {
		n.Node.Conn.Close()
	}
	e.up.clear()
	e.down.clear()
	e.http.CloseIdleConnections()
}

// Retired loopback listeners discard old-generation traffic during the bounded
// native table handoff. Keeping them briefly open avoids ICMP resetting the
// game's shared socket. They never authorize or forward a retired route.
func (e *edgeClient) retire(nodes []*edgeRoute) {
	if len(nodes) == 0 {
		return
	}
	e.mu.Lock()
	overflow := len(e.retired)+len(nodes) > 24
	if !overflow {
		for _, n := range nodes {
			e.retired = append(e.retired, retiredRoute{n, time.Now()})
		}
	} else {
		e.failed.Add(1)
	}
	e.mu.Unlock()
	if overflow {
		for _, n := range nodes {
			n.Conn.Close()
		}
	}
}
func (e *edgeClient) reap() {
	e.mu.Lock()
	keep := e.retired[:0]
	closeNodes := []*edgeRoute{}
	for _, n := range e.retired {
		if time.Since(n.At) >= 2*time.Second {
			closeNodes = append(closeNodes, n.Node)
		} else {
			keep = append(keep, n)
		}
	}
	e.retired = keep
	e.mu.Unlock()
	for _, n := range closeNodes {
		n.Conn.Close()
	}
}
func readPrivateLine(reader *bufio.Reader, v any) error {
	b, e := reader.ReadSlice('\n')
	if e != nil || len(b) > 32768 {
		return errors.New("bounded private startup required")
	}
	if strictJSON(b, v) != nil {
		return errors.New("invalid private startup")
	}
	return nil
}
func runEdgeFromStdin(parent context.Context, input io.Reader, readyPath string) error {
	reader := bufio.NewReaderSize(input, 32769)
	var cfg edgeStartup
	if err := readPrivateLine(reader, &cfg); err != nil {
		return err
	}
	created, err := processCreated(cfg.Parent)
	if err != nil || cfg.Parent != uint32(os.Getppid()) || created != cfg.ParentCreated {
		return errors.New("launcher process identity mismatch")
	}
	if cfg.ParentIPC {
		return runParentEdge(parent, cfg, reader, readyPath)
	}
	bridge, err := newPipeBridge(cfg.Pipe)
	if err != nil {
		return errors.New("private native pipe unavailable")
	}
	defer bridge.Close()
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	edge, err := dialEdge(ctx, cfg)
	if err != nil {
		return err
	}
	defer edge.close()
	failures := make(chan error, 8)
	run := func(f func() error) {
		go func() {
			if err := f(); err != nil {
				select {
				case failures <- err:
				case <-ctx.Done():
				}
			}
		}()
	}
	run(func() error { return edge.writer(ctx) })
	run(func() error { return edge.reader(ctx) })
	run(func() error { return edge.poll(ctx, failures) })
	run(func() error { return bridge.serve(ctx, edge.nativeExchange) })
	run(func() error {
		var identity gameIdentity
		if err := readPrivateLine(reader, &identity); err != nil {
			return err
		}
		if err := bridge.bind(identity); err != nil {
			return err
		}
		if _, err := reader.ReadByte(); err == io.EOF {
			return errors.New("launcher private channel closed")
		}
		return errors.New("unexpected launcher command")
	})
	if readyPath != "" {
		b, _ := json.Marshal(map[string]any{"pid": os.Getpid(), "role": "edge", "transport": "wss", "member_id": cfg.Grant.Member, "native_pipe_ready": true})
		if os.WriteFile(readyPath, b, 0600) != nil {
			return errors.New("edge readiness report unavailable")
		}
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case err := <-failures:
		return err
	}
}
