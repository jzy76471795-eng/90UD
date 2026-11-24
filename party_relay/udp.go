package main

// UDP endpoints become usable only after an authenticated TLS request, a
// fixed-size authenticated UDP return challenge, and its receipt over TLS.
// WSS remains connected for control and capability checks. Native payloads fix
// their transport for one scope/epoch; a failed path requires a fresh attempt.
import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"net"
	"net/http"
	"sync"
	"sync/atomic"
	"time"
)

const udpChallengeSize = 128

type sessionUDP struct {
	mu                             sync.Mutex
	challenge, receipt, generation string
	pendingUntil                   time.Time
	candidate, endpoint            *net.UDPAddr
	replies                        int
	selected                       bool
	selectedAt, lastReceive        time.Time
	probes                         map[int]time.Time
	rate                           Rate
	ingress                        requestBucket
}
type relayUDP struct {
	conn                       *net.UDPConn
	output                     datagramOutput
	public                     string
	received, sent, rejected   atomic.Uint64
	nativeReceived, nativeSent atomic.Uint64
	limited                    atomic.Uint64
	admission                  *udpIngress
}

type datagramOutput interface {
	SetWriteDeadline(time.Time) error
	WriteToUDP([]byte,*net.UDPAddr)(int,error)
}

func challengeFrame(s *relaySession, kind byte, challenge, receipt string) []byte {
	b := make([]byte, udpChallengeSize)
	copy(b, "RUC1")
	b[4] = kind
	binary.BigEndian.PutUint32(b[8:], s.grant.Member)
	binary.BigEndian.PutUint64(b[12:], s.grant.Session)
	value, _ := hex.DecodeString(challenge)
	copy(b[20:36], value)
	value, _ = hex.DecodeString(receipt)
	copy(b[44:60], value)
	mac := hmac.New(sha256.New, derive(s.secret, "product/udp-challenge/"+string([]byte{kind})))
	mac.Write(b[:112])
	copy(b[112:], mac.Sum(nil)[:16])
	return b
}
func validChallenge(s *relaySession, b []byte, kind byte) bool {
	if len(b) != udpChallengeSize || string(b[:4]) != "RUC1" || b[4] != kind || binary.BigEndian.Uint32(b[8:]) != s.grant.Member || binary.BigEndian.Uint64(b[12:]) != s.grant.Session {
		return false
	}
	for _, span := range [][]byte{b[5:8], b[36:44], b[60:112]} {
		for _, v := range span {
			if v != 0 {
				return false
			}
		}
	}
	mac := hmac.New(sha256.New, derive(s.secret, "product/udp-challenge/"+string([]byte{kind})))
	mac.Write(b[:112])
	return hmac.Equal(b[112:], mac.Sum(nil)[:16])
}
func sameUDP(a, b *net.UDPAddr) bool {
	return a != nil && b != nil && a.Port == b.Port && a.IP.Equal(b.IP)
}
func (v *service) attachUDP(conn *net.UDPConn, public string, configured ...udpIngressLimits) error {
	addr, err := net.ResolveUDPAddr("udp4", public)
	if err != nil || addr.Port < 1 || addr.Port > 65535 || addr.IP == nil || addr.IP.IsUnspecified() || addr.IP.IsMulticast() || addr.IP.Equal(net.IPv4bcast) {
		return errors.New("invalid public UDP endpoint")
	}
	if v.udp != nil {
		return errors.New("UDP already installed")
	}
	var limits udpIngressLimits
	if len(configured) > 1 {
		return errors.New("one UDP ingress configuration required")
	}
	if len(configured) == 1 {
		limits = configured[0]
	}
	admission, err := newUDPIngress(limits)
	if err != nil {
		return err
	}
	v.udp = &relayUDP{conn: conn, output:conn, public: public, admission: admission}
	go v.readUDP()
	return nil
}
func (v *service) udpControl(w http.ResponseWriter, r *http.Request, s *relaySession, body []byte) {
	if v.udp == nil {
		response(w, 409, map[string]any{"error": "UDP unavailable"})
		return
	}
	u := s.udp
	now := time.Now()
	switch r.URL.Path {
	case "/v1/edge/udp/prepare":
		var request struct{}
		if r.Method != "POST" || strictJSON(body, &request) != nil {
			response(w, 400, map[string]any{"error": "invalid UDP preparation"})
			return
		}
		u.mu.Lock()
		if now.Before(u.pendingUntil) && u.challenge != "" {
			u.mu.Unlock()
			response(w, 409, map[string]any{"error": "UDP preparation pending"})
			return
		}
		u.challenge = randomHex(16)
		u.receipt = randomHex(16)
		u.pendingUntil = now.Add(1500 * time.Millisecond)
		u.candidate = nil
		u.replies = 0
		challenge := u.challenge
		u.mu.Unlock()
		response(w, 200, map[string]any{"challenge": challenge})
	case "/v1/edge/udp/confirm":
		var request struct {
			Challenge string `json:"challenge"`
			Receipt   string `json:"receipt"`
		}
		if r.Method != "POST" || strictJSON(body, &request) != nil {
			response(w, 400, map[string]any{"error": "invalid UDP confirmation"})
			return
		}
		u.mu.Lock()
		if !now.Before(u.pendingUntil) || u.candidate == nil || request.Challenge != u.challenge || len(request.Receipt) != 32 || !hmac.Equal([]byte(request.Receipt), []byte(u.receipt)) {
			u.mu.Unlock()
			response(w, 409, map[string]any{"error": "UDP return path unproven"})
			return
		}
		changed := !sameUDP(u.endpoint, u.candidate)
		if changed {
			u.selected = false
			u.probes = map[int]time.Time{}
		}
		addr := *u.candidate
		u.endpoint = &addr
		u.generation = u.challenge
		u.challenge = ""
		u.receipt = ""
		u.pendingUntil = time.Time{}
		u.candidate = nil
		u.lastReceive = now
		u.mu.Unlock()
		response(w, 200, map[string]any{"changed": changed})
	case "/v1/edge/udp/select":
		var request struct {
			Generation string `json:"generation"`
			Enabled    bool   `json:"enabled"`
		}
		if r.Method != "POST" || strictJSON(body, &request) != nil {
			response(w, 400, map[string]any{"error": "invalid UDP selection"})
			return
		}
		u.mu.Lock()
		valid := request.Generation != "" && request.Generation == u.generation && u.endpoint != nil
		if request.Enabled {
			for _, size := range []int{64, 512, 8192} {
				at := u.probes[size]
				valid = valid && !at.IsZero() && now.Sub(at) < 1500*time.Millisecond
			}
		}
		if !valid {
			u.mu.Unlock()
			response(w, 409, map[string]any{"error": "UDP capability expired"})
			return
		}
		u.selected = request.Enabled
		u.selectedAt = now
		u.mu.Unlock()
		response(w, 200, map[string]any{"selected": request.Enabled})
	default:
		response(w, 404, map[string]any{"error": "UDP control route not found"})
	}
}
func (v *service) readUDP() {
	buffer := make([]byte, 1501)
	for {
		size, addr, err := v.udp.conn.ReadFromUDP(buffer)
		if err != nil {
			return
		}
		b := buffer[:size]
		now := time.Now()
		if len(b) == udpChallengeSize && string(b[:4]) == "RUC1" {
			if !v.udp.admission.anonymous(addr.IP.String(), now) {
				v.udp.limited.Add(1)
				continue
			}
			s := v.registry.lookup(binary.BigEndian.Uint32(b[8:]))
			if s == nil || s.closed.Load() || !s.connected.Load() || !validChallenge(s, b, 1) {
				v.udp.rejected.Add(1)
				continue
			}
			if !hmac.Equal(b[44:60], make([]byte, 16)) {
				v.udp.rejected.Add(1)
				continue
			}
			u := s.udp
			u.mu.Lock()
			if !now.Before(u.pendingUntil) || u.challenge != hex.EncodeToString(b[20:36]) || u.replies >= 3 || (u.candidate != nil && !sameUDP(u.candidate, addr)) {
				u.mu.Unlock()
				v.udp.rejected.Add(1)
				continue
			}
			a := *addr
			u.candidate = &a
			u.replies++
			reply := challengeFrame(s, 2, u.challenge, u.receipt)
			u.mu.Unlock()
			// Same-length response, at most three times per TLS-issued challenge.
			v.udp.conn.SetWriteDeadline(time.Now().Add(100 * time.Millisecond))
			v.udp.conn.WriteToUDP(reply, addr)
			continue
		}
		h, err := parseHeader(b)
		if err != nil {
			if !v.udp.admission.anonymous(addr.IP.String(), now) {
				v.udp.limited.Add(1)
				continue
			}
			v.udp.rejected.Add(1)
			continue
		}
		s := v.registry.lookup(h.Source)
		if s == nil || s.closed.Load() || !s.connected.Load() || h.Session != s.grant.Session {
			if !v.udp.admission.anonymous(addr.IP.String(), now) {
				v.udp.limited.Add(1)
				continue
			}
			v.udp.rejected.Add(1)
			continue
		}
		u := s.udp
		u.mu.Lock()
		allowed := sameUDP(u.endpoint, addr) && u.generation != ""
		u.mu.Unlock()
		if !allowed {
			if !v.udp.admission.anonymous(addr.IP.String(), now) {
				v.udp.limited.Add(1)
				continue
			}
			v.udp.rejected.Add(1)
			continue
		}
		if !v.udp.admission.authenticated(s, now) {
			v.udp.limited.Add(1)
			continue
		}
		// decode authenticates before touching replay/reassembly state. A forged
		// source endpoint cannot replace the TLS-confirmed endpoint.
		if v.inputFrame(s, b, true) {
			v.udp.received.Add(1)
		} else {
			v.udp.rejected.Add(1)
		}
	}
}
func (u *sessionUDP) destination(now time.Time, probe bool) *net.UDPAddr {
	u.mu.Lock()
	defer u.mu.Unlock()
	if u.endpoint == nil || u.generation == "" || now.Sub(u.lastReceive) >= 1500*time.Millisecond {
		return nil
	}
	if !probe && (!u.selected || now.Sub(u.selectedAt) >= time.Second) {
		return nil
	}
	a := *u.endpoint
	return &a
}

type edgeUDP struct {
	mu                                        sync.Mutex
	conn                                      *net.UDPConn
	endpoint, pending, receipt, generation    string
	preparedAt, sentAt, selectedAt, lastProbe time.Time
	selected                                  bool
	verified                                  map[int]time.Time
}

func (e *edgeClient) udpDestination(probe bool) *net.UDPConn {
	u := e.udp
	if u == nil {
		return nil
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	if u.conn == nil || u.generation == "" {
		return nil
	}
	if !probe {
		if !u.selected || time.Since(u.selectedAt) >= time.Second {
			return nil
		}
		for _, size := range []int{64, 512, 8192} {
			at := u.verified[size]
			if at.IsZero() || time.Since(at) >= 1500*time.Millisecond {
				return nil
			}
		}
	}
	return u.conn
}
func (e *edgeClient) resetUDP() {
	if e.udp == nil {
		return
	}
	u := e.udp
	u.mu.Lock()
	conn := u.conn
	u.conn = nil
	u.selected = false
	u.generation = ""
	u.pending = ""
	u.receipt = ""
	u.verified = map[int]time.Time{}
	u.preparedAt = time.Time{}
	u.mu.Unlock()
	if conn != nil {
		conn.Close()
	}
}
func (e *edgeClient) udpReader(ctx context.Context, conn *net.UDPConn) {
	b := make([]byte, 1501)
	for ctx.Err() == nil {
		conn.SetReadDeadline(time.Now().Add(250 * time.Millisecond))
		size, err := conn.Read(b)
		if err != nil {
			if n, ok := err.(net.Error); ok && n.Timeout() {
				continue
			}
			u := e.udp
			u.mu.Lock()
			if u.conn == conn {
				u.conn = nil
				u.selected = false
				u.generation = ""
				u.pending = ""
				u.receipt = ""
				u.verified = map[int]time.Time{}
				u.preparedAt = time.Time{}
			}
			u.mu.Unlock()
			conn.Close()
			return
		}
		raw := b[:size]
		if len(raw) == udpChallengeSize && string(raw[:4]) == "RUC1" {
			s := &relaySession{grant: e.cfg.Grant, secret: e.secret}
			if validChallenge(s, raw, 2) {
				u := e.udp
				u.mu.Lock()
				if u.conn == conn && u.pending == hex.EncodeToString(raw[20:36]) {
					u.receipt = hex.EncodeToString(raw[44:60])
				}
				u.mu.Unlock()
			}
			continue
		}
		if err = e.acceptFrame(raw, true); err != nil {
			e.failed.Add(1)
			return
		}
	}
}
func (e *edgeClient) udpStep(ctx context.Context, endpoint string) {
	if endpoint == "" {
		return
	}
	u := e.udp
	if u == nil {
		return
	}
	u.mu.Lock()
	conn := u.conn
	priorEndpoint := u.endpoint
	u.mu.Unlock()
	if conn == nil || priorEndpoint != endpoint {
		e.resetUDP()
		addr, err := net.ResolveUDPAddr("udp4", endpoint)
		if err != nil || addr.Port < 1 || addr.IP == nil || addr.IP.IsUnspecified() || addr.IP.IsMulticast() || addr.IP.Equal(net.IPv4bcast) {
			return
		}
		conn, err = net.DialUDP("udp4", nil, addr)
		if err != nil {
			return
		}
		u.mu.Lock()
		u.conn = conn
		u.endpoint = endpoint
		u.mu.Unlock()
		go e.udpReader(ctx, conn)
	}
	now := time.Now()
	u.mu.Lock()
	pending, receipt := u.pending, u.receipt
	prepared := u.preparedAt
	u.mu.Unlock()
	if pending != "" && receipt != "" {
		var confirmed struct {
			Changed bool `json:"changed"`
		}
		if e.rpc(ctx, "POST", "/v1/edge/udp/confirm", map[string]any{"challenge": pending, "receipt": receipt}, &confirmed) == nil {
			u.mu.Lock()
			u.generation = pending
			u.pending = ""
			u.receipt = ""
			if confirmed.Changed {
				u.selected = false
				u.verified = map[int]time.Time{}
				u.lastProbe = time.Time{}
			}
			u.mu.Unlock()
		}
	}
	if now.Sub(prepared) >= 2*time.Second {
		var response struct {
			Challenge string `json:"challenge"`
		}
		if e.rpc(ctx, "POST", "/v1/edge/udp/prepare", struct{}{}, &response) == nil {
			value, err := hex.DecodeString(response.Challenge)
			if err == nil && len(value) == 16 {
				u.mu.Lock()
				u.pending = response.Challenge
				u.receipt = ""
				u.preparedAt = now
				u.sentAt = time.Time{}
				u.mu.Unlock()
			}
		}
	}
	u.mu.Lock()
	pending = u.pending
	send := pending != "" && u.receipt == "" && now.Sub(u.sentAt) >= 250*time.Millisecond && now.Sub(u.preparedAt) < 1250*time.Millisecond
	if send {
		u.sentAt = now
	}
	u.mu.Unlock()
	if send {
		conn.SetWriteDeadline(now.Add(100 * time.Millisecond))
		conn.Write(challengeFrame(&relaySession{grant: e.cfg.Grant, secret: e.secret}, 1, pending, ""))
	}
	u.mu.Lock()
	generation := u.generation
	// Leave multiple poll ticks before the 1250ms capability expiry. A 1s
	// interval plus a 250ms tick can otherwise expire at the exact send tick,
	// withdrawing a healthy fixed path before its fresh echo can arrive.
	probe := generation != "" && now.Sub(u.lastProbe) >= 500*time.Millisecond
	if probe {
		u.lastProbe = now
	}
	preferred := generation != ""
	for _, size := range []int{64, 512, 8192} {
		at := u.verified[size]
		preferred = preferred && !at.IsZero() && now.Sub(at) < 1250*time.Millisecond
	}
	selectNow := generation != "" && (preferred != u.selected || preferred && now.Sub(u.selectedAt) >= 500*time.Millisecond)
	u.mu.Unlock()
	if probe {
		_ = e.probeTransport(true)
	}
	if selectNow {
		err := e.rpc(ctx, "POST", "/v1/edge/udp/select", map[string]any{"generation": generation, "enabled": preferred}, nil)
		u.mu.Lock()
		u.selected = err == nil && preferred
		if u.selected {
			u.selectedAt = time.Now()
		}
		u.mu.Unlock()
	}
}
