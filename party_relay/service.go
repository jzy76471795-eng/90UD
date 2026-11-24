package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"github.com/gorilla/websocket"
	"io"
	"net"
	"net/http"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

type service struct {
	registry                       *registry
	control                        []byte
	guard                          *requestGuard
	rejected, forwarded, congested atomic.Uint64
	closing                        atomic.Bool
	admission                      admission
	inflight                       chan struct{}
	udp                            *relayUDP
	nativeDropped, nativePartial   atomic.Uint64
	probeDropped                   atomic.Uint64
}

func newService(key []byte, max int) *service {
	return &service{registry: newRegistry(key, max), control: key, guard: newRequestGuard(4096), inflight: make(chan struct{}, 64)}
}
func response(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(value)
}
func uniqueJSON(d *json.Decoder, depth int) error {
	if depth > 16 {
		return errors.New("JSON nesting limit")
	}
	t, e := d.Token()
	if e != nil {
		return e
	}
	open, ok := t.(json.Delim)
	if !ok {
		return nil
	}
	if open == '{' {
		seen := map[string]bool{}
		for d.More() {
			key, e := d.Token()
			if e != nil {
				return e
			}
			name, ok := key.(string)
			if !ok || seen[name] {
				return errors.New("duplicate JSON key")
			}
			seen[name] = true
			if len(seen) > 4096 {
				return errors.New("JSON object limit")
			}
			if e = uniqueJSON(d, depth+1); e != nil {
				return e
			}
		}
	} else if open == '[' {
		count := 0
		for d.More() {
			count++
			if count > 4096 {
				return errors.New("JSON array limit")
			}
			if e = uniqueJSON(d, depth+1); e != nil {
				return e
			}
		}
	} else {
		return errors.New("invalid JSON delimiter")
	}
	_, e = d.Token()
	return e
}
func strictJSON(raw []byte, v any) error {
	trim := bytes.TrimSpace(raw)
	if len(trim) == 0 || trim[0] != '{' {
		return errors.New("JSON object required")
	}
	scan := json.NewDecoder(bytes.NewReader(raw))
	if e := uniqueJSON(scan, 0); e != nil {
		return e
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if e := d.Decode(v); e != nil {
		return e
	}
	if d.Decode(&struct{}{}) != io.EOF {
		return errors.New("trailing request value")
	}
	return nil
}
func readBody(w http.ResponseWriter, r *http.Request) ([]byte, error) {
	return io.ReadAll(http.MaxBytesReader(w, r.Body, 64*1024))
}
func (v *service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if v.closing.Load() {
		response(w, 503, map[string]any{"error": "service stopping"})
		return
	}
	if r.TLS == nil {
		response(w, 400, map[string]any{"error": "TLS required"})
		return
	}
	if !v.admission.accept(r.RemoteAddr, time.Now()) {
		w.Header().Set("Connection", "close")
		response(w, 429, map[string]any{"error": "request admission limit"})
		return
	}
	if r.Method == http.MethodGet && r.URL.Path == "/v1/clock" && r.URL.RawQuery == "" {
		response(w, 200, map[string]any{"server_ms": time.Now().UnixMilli()})
		return
	}
	if r.URL.Path == "/v1/edge" && r.Method == http.MethodGet {
		v.websocket(w, r)
		return
	}
	select {
	case v.inflight <- struct{}{}:
		defer func() { <-v.inflight }()
	default:
		response(w, 503, map[string]any{"error": "request capacity"})
		return
	}
	body, e := readBody(w, r)
	if e != nil {
		response(w, 413, map[string]any{"error": "bounded request required"})
		return
	}
	if strings.HasPrefix(r.URL.Path, "/v1/control/") {
		if e = v.guard.verify(r, body, v.control, "control-v1", time.Now()); e != nil {
			v.rejected.Add(1)
			response(w, 401, map[string]any{"error": "control authentication required"})
			return
		}
		v.controlRequest(w, r, body)
		return
	}
	if r.URL.Path == "/v1/edge/state" || r.URL.Path == "/v1/edge/report" || r.URL.Path == "/v1/edge/transport-failed" || r.URL.Path == "/v1/edge/udp/prepare" || r.URL.Path == "/v1/edge/udp/confirm" || r.URL.Path == "/v1/edge/udp/select" {
		id, e1 := strconv.ParseUint(r.Header.Get("X-Relay-Member"), 10, 32)
		sid, e2 := strconv.ParseUint(r.Header.Get("X-Relay-Session"), 10, 64)
		s := v.registry.lookup(uint32(id))
		if e1 != nil || e2 != nil || s == nil || s.closed.Load() || !s.connected.Load() || s.grant.Session != sid || s.guard.verify(r, body, derive(s.secret, "product/edge-http"), "edge-v1", time.Now()) != nil {
			response(w, 401, map[string]any{"error": "edge authentication required"})
			return
		}
		if strings.HasPrefix(r.URL.Path, "/v1/edge/udp/") {
			v.udpControl(w, r, s, body)
			return
		}
		if r.Method == http.MethodGet && r.URL.Path == "/v1/edge/state" {
			response(w, 200, v.edgeState(s))
			return
		}
		if r.Method == http.MethodPost && r.URL.Path == "/v1/edge/transport-failed" {
			var path pathView
			if strictJSON(body, &path) != nil || path.Scope == 0 || path.Epoch == 0 || path.Revision == 0 {
				response(w, 400, map[string]any{"error": "invalid transport failure"})
				return
			}
			response(w, 200, map[string]any{"accepted": s.failTransport(path)})
			return
		}
		if r.Method == http.MethodPost && r.URL.Path == "/v1/edge/report" {
			var report nativeReport
			if strictJSON(body, &report) != nil || len(report.Peers) > 10 {
				response(w, 400, map[string]any{"error": "invalid native report"})
				return
			}
			seen := map[uint32]bool{}
			seenSlots := map[uint32]bool{}
			for _, p := range report.Peers {
				if p.Slot >= 10 || p.Key == 0 || seen[p.Key] || seenSlots[p.Slot] || p.State > 3 || (p.Confirmed && p.State != 0) {
					response(w, 400, map[string]any{"error": "invalid native peer view"})
					return
				}
				seen[p.Key] = true
				seenSlots[p.Slot] = true
			}
			if len(report.Verified) > 3 {
				response(w, 400, map[string]any{"error": "invalid capability view"})
				return
			}
			for size, age := range report.Verified {
				if (size != 64 && size != 512 && size != 8192) || age < 0 || age > 2000 {
					response(w, 400, map[string]any{"error": "expired capability evidence"})
					return
				}
			}
			v.registry.mu.Lock()
			if report.Installed > v.registry.view || report.Actor >= 65535 {
				v.registry.mu.Unlock()
				response(w, 400, map[string]any{"error": "invalid installed native view"})
				return
			}
			s.report.At = time.Now().UnixMilli()
			s.report.Native = report
			v.registry.mu.Unlock()
			response(w, 200, map[string]any{"ok": true})
			return
		}
	}
	response(w, 404, map[string]any{"error": "route not found"})
}
func (v *service) controlRequest(w http.ResponseWriter, r *http.Request, body []byte) {
	reg := v.registry
	switch {
	case r.Method == http.MethodPost && r.URL.Path == "/v1/control/session":
		var request sessionRequest
		if strictJSON(body, &request) != nil {
			response(w, 400, map[string]any{"error": "invalid session request"})
			return
		}
		grant, e := reg.issue(request, time.Now())
		if e != nil {
			response(w, 409, map[string]any{"error": e.Error()})
			return
		}
		response(w, 201, grant)
	case r.Method == http.MethodPost && r.URL.Path == "/v1/control/revoke":
		var request struct {
			Member uint32 `json:"member_id"`
		}
		if strictJSON(body, &request) != nil || request.Member == 0 {
			response(w, 400, map[string]any{"error": "invalid revocation"})
			return
		}
		reg.mutation.Lock()
		reg.revoke(request.Member)
		reg.mutation.Unlock()
		response(w, 200, map[string]any{"revoked": request.Member, "purge_before_ack": true})
	case r.Method == http.MethodPost && r.URL.Path == "/v1/control/bind":
		var request struct {
			Member  uint32  `json:"member_id"`
			Binding binding `json:"binding"`
		}
		if strictJSON(body, &request) != nil {
			response(w, 400, map[string]any{"error": "invalid binding"})
			return
		}
		if e := reg.bind(request.Member, request.Binding); e != nil {
			response(w, 409, map[string]any{"error": e.Error()})
			return
		}
		response(w, 200, map[string]any{"bound": request.Member})
	case r.Method == http.MethodPut && r.URL.Path == "/v1/control/routes":
		var snapshot Snapshot
		if strictJSON(body, &snapshot) != nil || len(snapshot.Members) > reg.max || len(snapshot.Pending) > reg.max || len(snapshot.Members)+len(snapshot.Pending) > 512 {
			response(w, 400, map[string]any{"error": "invalid bounded snapshot"})
			return
		}
		reg.mutation.Lock()
		defer reg.mutation.Unlock()
		counts := map[uint64]int{}
		for _, m := range append(append([]Member{}, snapshot.Members...), snapshot.Pending...) {
			s := reg.lookup(m.ID)
			if s == nil || s.closed.Load() || s.owner.Actor == 0 || s.owner.Key == 0 {
				response(w, 409, map[string]any{"error": "route member has no current native binding"})
				return
			}
		}
		for _, m := range snapshot.Members {
			counts[m.Scope]++
			if counts[m.Scope] > 4 {
				response(w, 409, map[string]any{"error": "scope exceeds four actual native members"})
				return
			}
		}
		pendingCounts := map[uint64]int{}
		for _, m := range snapshot.Pending {
			pendingCounts[m.Scope]++
			if pendingCounts[m.Scope] > 4 {
				response(w, 409, map[string]any{"error": "pending scope exceeds four members"})
				return
			}
		}
		for _, group := range [][]Member{snapshot.Members, snapshot.Pending} {
			channels := map[uint64]uint32{}
			keys := map[[2]uint64]bool{}
			actors := map[[2]uint64]bool{}
			for _, m := range group {
				s := reg.lookup(m.ID)
				prior := channels[m.Scope]
				if prior != 0 && prior != s.owner.Channel {
					response(w, 409, map[string]any{"error": "scope spans native channels"})
					return
				}
				channels[m.Scope] = s.owner.Channel
				key := [2]uint64{m.Scope, uint64(s.owner.Key)}
				actor := [2]uint64{m.Scope, uint64(s.owner.Actor)}
				if keys[key] || actors[actor] {
					response(w, 409, map[string]any{"error": "duplicate native identity in scope"})
					return
				}
				keys[key] = true
				actors[actor] = true
			}
		}
		before := reg.authority.ack()
		if e := reg.authority.apply(snapshot, time.Now()); e != nil {
			response(w, 409, map[string]any{"error": e.Error()})
			return
		}
		after := reg.authority.ack()
		if !reflect.DeepEqual(before["members"], after["members"]) || !reflect.DeepEqual(before["pending"], after["pending"]) {
			reg.mu.Lock()
			reg.view++
			reg.mu.Unlock()
		}
		response(w, 200, v.statusFor([]uint32{}))
	case r.Method == http.MethodPost && r.URL.Path == "/v1/control/status":
		var request struct {
			Members []uint32 `json:"member_ids"`
		}
		if strictJSON(body, &request) != nil || request.Members == nil || len(request.Members) > 16 {
			response(w, 400, map[string]any{"error": "bounded status page required"})
			return
		}
		seen := map[uint32]bool{}
		for _, id := range request.Members {
			if id == 0 || seen[id] {
				response(w, 400, map[string]any{"error": "invalid status member"})
				return
			}
			seen[id] = true
		}
		response(w, 200, v.statusFor(request.Members))
	case r.Method == http.MethodGet && r.URL.Path == "/v1/control/status":
		response(w, 200, v.status())
	default:
		response(w, 404, map[string]any{"error": "control route not found"})
	}
}
func (v *service) status() map[string]any {
	return v.statusFor(nil)
}
func (v *service) statusFor(ids []uint32) map[string]any {
	reg := v.registry
	reg.mu.RLock()
	defer reg.mu.RUnlock()
	sessions := []map[string]any{}
	for id, s := range reg.byID {
		if ids != nil {
			found := false
			for _, allowed := range ids {
				if allowed == id {
					found = true
					break
				}
			}
			if !found {
				continue
			}
		}
		sizes := map[int]int64{}
		for size, at := range s.report.Probe {
			sizes[size] = at
		}
		udpSelected := s.udp != nil && s.udp.destination(time.Now(), false) != nil
		reg.authority.mu.RLock()
		mine := reg.authority.members[id]
		path := s.transport(mine.Scope, mine.Epoch, time.Now())
		reg.authority.mu.RUnlock()
		sessions = append(sessions, map[string]any{"member_id": id, "binding": s.owner, "connected": s.connected.Load(), "closed": s.closed.Load(), "native_report_at_ms": s.report.At, "native": s.report.Native, "probe_received_sizes_ms": sizes, "congested": s.q.failed(), "udp_selected": udpSelected, "transport": path})
	}
	ack := reg.authority.ack()
	if ids != nil {
		delete(ack, "members")
		delete(ack, "pending")
	}
	result := map[string]any{"boot": reg.boot, "route_view": reg.view, "sessions": sessions, "authority": ack, "rejected": v.rejected.Load(), "forwarded": v.forwarded.Load(), "congested": v.congested.Load(), "reassembly_bytes": reg.assemblyBudget.bytes.Load(), "queued_bytes": reg.queueBudget.bytes.Load()}
	result["native_dropped"] = v.nativeDropped.Load()
	result["native_partial"] = v.nativePartial.Load()
	result["probe_dropped"] = v.probeDropped.Load()
	if v.udp != nil {
		result["udp_received"] = v.udp.received.Load()
		result["udp_sent"] = v.udp.sent.Load()
		result["udp_rejected"] = v.udp.rejected.Load()
		result["udp_limited"] = v.udp.limited.Load()
		result["udp_native_received"] = v.udp.nativeReceived.Load()
		result["udp_native_sent"] = v.udp.nativeSent.Load()
	}
	return result
}
func (v *service) edgeState(s *relaySession) map[string]any {
	reg := v.registry
	now := time.Now()
	reg.mu.RLock()
	defer reg.mu.RUnlock()
	peers := []map[string]any{}
	reg.authority.mu.RLock()
	defer reg.authority.mu.RUnlock()
	mine, found := reg.authority.members[s.grant.Member]
	valid := found && mine.Active && now.Before(reg.authority.expires) && now.Sub(reg.authority.heartbeat) < 3*time.Second
	path := pathView{}
	if valid {
		path = s.transport(mine.Scope, mine.Epoch, now)
		valid = !path.Failed
	}
	if valid {
		for id, m := range reg.authority.members {
			if id == mine.ID || !m.Active || m.Scope != mine.Scope || m.Epoch != mine.Epoch {
				continue
			}
			p := reg.byID[id]
			if p != nil && !p.closed.Load() {
				peers = append(peers, map[string]any{"member_id": id, "character_id": p.owner.Actor, "native_key": p.owner.Key, "virtual_ip": virtualIP(id), "virtual_port": 31000, "scope": m.Scope, "epoch": m.Epoch})
			}
		}
	}
	sort.Slice(peers, func(i, j int) bool { return peers[i]["member_id"].(uint32) < peers[j]["member_id"].(uint32) })
	udpEndpoint := ""
	if v.udp != nil {
		udpEndpoint = v.udp.public
	}
	return map[string]any{"boot": reg.boot, "member_id": s.grant.Member, "binding": s.owner, "route_view": reg.view, "active": valid, "peers": peers, "lease_ms": 750, "udp_endpoint": udpEndpoint, "transport": path}
}
func virtualIP(id uint32) string {
	n := id - 1
	return "198." + strconv.Itoa(18+int(n/64516)) + "." + strconv.Itoa(1+int(n/254%254)) + "." + strconv.Itoa(1+int(n%254))
}
func (v *service) websocket(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Origin") != "" || r.URL.RawQuery != "" || r.Header.Get("Sec-WebSocket-Protocol") != "90us-rlv2-v1" {
		response(w, 400, map[string]any{"error": "native edge protocol required"})
		return
	}
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	s, e := v.registry.redeem(token, time.Now())
	if e != nil {
		response(w, 401, map[string]any{"error": "single-use authorization required"})
		return
	}
	upgrade := websocket.Upgrader{ReadBufferSize: 1400, WriteBufferSize: 1400, Subprotocols: []string{"90us-rlv2-v1"}, CheckOrigin: func(r *http.Request) bool { return r.Header.Get("Origin") == "" }}
	conn, e := upgrade.Upgrade(w, r, nil)
	if e != nil {
		v.registry.mutation.Lock()
		v.registry.revoke(s.grant.Member)
		v.registry.mutation.Unlock()
		return
	}
	v.registry.mu.Lock()
	if v.closing.Load() {
		v.registry.mu.Unlock()
		conn.Close()
		return
	}
	s.disconnect = conn.Close
	s.connected.Store(true)
	v.registry.mu.Unlock()
	conn.SetReadLimit(int64(s.grant.MTU))
	done := make(chan struct{})
	defer func() {
		s.connected.Store(false)
		s.closed.Store(true)
		close(done)
		conn.Close()
		s.q.fail()
		s.probes.fail()
		s.up.clear()
		s.down.clear()
	}()
	go v.writeSession(conn, s, done)
	rate := Rate{}
	for {
		conn.SetReadDeadline(time.Now().Add(15 * time.Second))
		kind, b, e := conn.ReadMessage()
		if e != nil {
			return
		}
		if kind != websocket.BinaryMessage || !rate.allow(len(b), time.Now()) {
			v.rejected.Add(1)
			return
		}
		v.inputFrame(s, b, false)
	}
}
func (v *service) inputFrame(s *relaySession, b []byte, udp bool) bool {
	h, p, err := s.up.decode(b, time.Now(), func(h Header) bool {
		if s.closed.Load() || !s.connected.Load() || h.Source != s.grant.Member {
			return false
		}
		if isSessionProbe(h, s.grant.Member) {
			return true
		}
		a := v.registry.authority
		a.mu.RLock()
		defer a.mu.RUnlock()
		return h.Kind == 1 && a.permitsLocked(h, time.Now()) && s.transportPermits(h, udp)
	})
	if err != nil {
		v.rejected.Add(1)
		return false
	}
	if udp {
		u := s.udp
		u.mu.Lock()
		allowed := u.rate.allow(len(b), time.Now())
		if allowed {
			u.lastReceive = time.Now()
		}
		u.mu.Unlock()
		if !allowed {
			return false
		}
	}
	if p == nil {
		return true
	}
	if udp && h.Kind == 1 {
		v.udp.nativeReceived.Add(1)
	}
	target := s
	if !isSessionProbe(h, s.grant.Member) {
		target = v.registry.lookup(h.Target)
		if target == nil || target.closed.Load() || !target.connected.Load() {
			v.rejected.Add(1)
			return false
		}
	}
	q := target.q
	if h.Kind == 2 {
		q = target.probes
		if len(p) == 64 || len(p) == 512 || len(p) == 8192 {
			v.registry.mu.Lock()
			s.report.Probe[len(p)] = time.Now().UnixMilli()
			v.registry.mu.Unlock()
			if udp {
				s.udp.mu.Lock()
				s.udp.probes[len(p)] = time.Now()
				s.udp.mu.Unlock()
			}
		}
	}
	packet := Packet{H: h, Data: p, At: time.Now(), UDP: udp && h.Kind == 2}
	if h.Kind == 2 {
		if !q.putProbe(packet) {
			v.probeDropped.Add(1)
			return false
		}
	} else if !q.put(packet) {
		target.closed.Store(true)
		v.congested.Add(1)
		return false
	}
	v.forwarded.Add(1)
	return true
}
func (v *service) close() {
	v.closing.Store(true)
	if v.udp != nil {
		v.udp.conn.Close()
	}
	type entry struct {
		s          *relaySession
		disconnect func() error
	}
	v.registry.mu.RLock()
	list := []entry{}
	for _, s := range v.registry.byID {
		list = append(list, entry{s, s.disconnect})
	}
	v.registry.mu.RUnlock()
	for _, item := range list {
		s := item.s
		s.closed.Store(true)
		s.q.fail()
		s.probes.fail()
		if item.disconnect != nil {
			item.disconnect()
		}
		s.up.clear()
		s.down.clear()
	}
}
func (v *service) writeSession(conn *websocket.Conn, s *relaySession, done <-chan struct{}) {
	defer conn.Close()
	for {
		var p Packet
		var ok bool
		if p, ok = s.probes.pop(); !ok {
			p, ok = s.q.pop()
		}
		if !ok {
			select {
			case <-done:
				return
			case <-s.probes.wake:
				continue
			case <-s.q.wake:
				continue
			case <-time.After(time.Second):
				if s.closed.Load() {
					return
				}
				continue
			}
		}
		if s.closed.Load() {
			return
		}
		if time.Since(p.At) >= 100*time.Millisecond {
			if isSessionProbe(p.H, s.grant.Member) {
				v.probeDropped.Add(1)
				continue
			}
			v.congested.Add(1)
			s.closed.Store(true)
			return
		}
		if p.H.Kind == 1 {
			v.registry.authority.mu.RLock()
			a := v.registry.authority
			source, target := a.members[p.H.Source], a.members[p.H.Target]
			now := time.Now()
			valid := now.Before(a.expires) && now.Sub(a.heartbeat) < 3*time.Second && source.Active && target.Active && source.Scope == p.H.Scope && target.Scope == p.H.Scope && source.Epoch == p.H.Epoch && target.Epoch == p.H.Epoch
			if !valid {
				a.mu.RUnlock()
				v.rejected.Add(1)
				continue
			}
			e := v.writeFrames(conn, s, p)
			a.mu.RUnlock()
			if e != nil {
				if errors.Is(e, errAttemptPath) {
					continue
				}
				s.closed.Store(true)
				return
			}
		} else if e := v.writeFrames(conn, s, p); e != nil {
			if errors.Is(e, errAttemptPath) {
				continue
			}
			s.closed.Store(true)
			return
		}
	}
}
func (v *service) writeFrames(conn *websocket.Conn, s *relaySession, p Packet) error {
	native := p.H.Kind == 1
	path := pathView{}
	var destination *net.UDPAddr
	if native {
		path = s.transport(p.H.Scope, p.H.Epoch, time.Now())
	}
	if v.udp != nil && (p.UDP || native && path.Mode == "udp") {
		destination = s.udp.destination(time.Now(), p.UDP)
	}
	drop := func(partial bool) {
		if native {
			v.nativeDropped.Add(1)
			if partial {
				v.nativePartial.Add(1)
			}
		} else {
			v.probeDropped.Add(1)
		}
	}
	if native && (path.Failed || path.Mode == "udp" && destination == nil) {
		s.failTransport(path)
		drop(false)
		return errAttemptPath
	}
	if p.UDP && destination == nil {
		drop(false)
		return errAttemptPath
	}
	frames, err := s.down.encodeKind(p.H.Source, p.H.Target, p.H.Scope, p.H.Epoch, p.Data, p.H.Kind)
	if err != nil {
		drop(false)
		return err
	}
	deadline := time.Now().Add(100 * time.Millisecond)
	if destination != nil {
		v.udp.output.SetWriteDeadline(deadline)
	} else {
		conn.SetWriteDeadline(deadline)
	}
	for i, b := range frames {
		if time.Now().After(deadline) {
			drop(i > 0)
			if destination != nil {
				if native {
					s.failTransport(path)
				}
				return errAttemptPath
			}
			return errors.New("native datagram write deadline")
		}
		if destination != nil {
			var n int
			n, err = v.udp.output.WriteToUDP(b, destination)
			if err == nil && n != len(b) {
				err = io.ErrShortWrite
			}
			if err != nil {
				if native {
					s.failTransport(path)
				}
				drop(i > 0)
				return errAttemptPath
			}
			v.udp.sent.Add(1)
		} else {
			if err = conn.WriteMessage(websocket.BinaryMessage, b); err != nil {
				drop(i > 0)
				return err
			}
		}
	}
	if native && destination != nil {
		v.udp.nativeSent.Add(1)
	}
	return nil
}
