package main

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"
)

type binding struct {
	Account uint32 `json:"account_id"`
	Login   string `json:"login_instance"`
	Actor   uint32 `json:"character_id"`
	Key     uint32 `json:"native_key"`
	Channel uint32 `json:"channel_id"`
}
type sessionRequest struct {
	Request string `json:"request_id"`
	Account uint32 `json:"account_id"`
	Login   string `json:"login_instance"`
	MTU     int    `json:"mtu"`
}
type sessionGrant struct {
	Boot    string `json:"boot"`
	Member  uint32 `json:"member_id"`
	Session uint64 `json:"session_id"`
	Secret  string `json:"secret"`
	Token   string `json:"launch_token"`
	Expires int64  `json:"expires_ms"`
	MTU     int    `json:"mtu"`
}
type nativePeer struct {
	Slot      uint32 `json:"slot"`
	Key       uint32 `json:"key"`
	State     uint32 `json:"state"`
	Confirmed bool   `json:"incoming_confirmed"`
}
type nativeReport struct {
	Actor     uint32        `json:"character_id"`
	Socket    uint32        `json:"socket"`
	Port      uint16        `json:"native_port"`
	Context   uint32        `json:"context_generation"`
	Installed uint64        `json:"installed_route_view"`
	Peers     []nativePeer  `json:"peers"`
	Verified  map[int]int64 `json:"verified_sizes_age_ms"`
}
type reported struct {
	At     int64         `json:"at_ms"`
	Native nativeReport  `json:"native"`
	Probe  map[int]int64 `json:"probe_sizes_ms"`
}
type relaySession struct {
	grant      sessionGrant
	owner      binding
	secret     []byte
	tokenHash  [32]byte
	created    time.Time
	tokenUsed  bool
	closed     atomic.Bool
	connected  atomic.Bool
	up, down   *Codec
	q, probes  *Queue
	send       sync.Mutex
	guard      *requestGuard
	report     reported
	disconnect func() error
	udp        *sessionUDP
	path       attemptPath
}
type registry struct {
	mu                          sync.RWMutex
	mutation                    sync.Mutex
	boot                        string
	next                        uint32
	view                        uint64
	max                         int
	byID                        map[uint32]*relaySession
	tokens                      map[[32]byte]*relaySession
	account                     map[uint32]uint32
	issued                      map[string]issuedRequest
	authority                   *Authority
	assemblyBudget, queueBudget *Budget
}
type issuedRequest struct {
	fingerprint [32]byte
	member      uint32
	expires     int64
}

// Called with mutation held. Reap before admitting a new grant so abandoned
// one-use authorizations cannot permanently occupy the session directory.
func (r *registry) reap(now time.Time) {
	r.mu.Lock()
	for request, prior := range r.issued {
		if now.UnixMilli() >= prior.expires {
			delete(r.issued, request)
		}
	}
	expired := make([]uint32, 0)
	for id, s := range r.byID {
		if s.closed.Load() || (!s.tokenUsed && now.UnixMilli() >= s.grant.Expires) {
			expired = append(expired, id)
		}
	}
	r.mu.Unlock()
	for _, id := range expired {
		r.revoke(id)
	}
}

func newRegistry(key []byte, maxSessions int) *registry {
	r := &registry{boot: randomHex(16), max: maxSessions, byID: map[uint32]*relaySession{}, tokens: map[[32]byte]*relaySession{}, account: map[uint32]uint32{}, issued: map[string]issuedRequest{}, assemblyBudget: &Budget{limit: 8 * 1024 * 1024}, queueBudget: &Budget{limit: 16 * 1024 * 1024}}
	r.authority = newAuthority(r.boot, key, nil, r.purge)
	return r
}
func (r *registry) purge(ids []uint32) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	for _, id := range ids {
		s:=r.byID[id]
		if s==nil {continue}
		s.q.clear()
		s.probes.clear()
		s.up.clear()
		s.down.clear()
	}
}
func (r *registry) revoke(id uint32) {
	r.mu.RLock()
	s := r.byID[id]
	r.mu.RUnlock()
	if s == nil {
		return
	}
	s.closed.Store(true)
	r.authority.mu.Lock()
	if old, ok := r.authority.members[id]; ok {
		r.authority.tombstones[id] = old.Epoch
		delete(r.authority.members, id)
	}
	delete(r.authority.pending, id)
	delete(r.authority.allowed, id)
	r.authority.mu.Unlock()
	s.q.fail()
	s.probes.fail()
	s.up.clear()
	s.down.clear()
	r.mu.Lock()
	delete(r.byID, id)
	delete(r.tokens, s.tokenHash)
	if r.account[s.owner.Account] == id {
		delete(r.account, s.owner.Account)
	}
	r.view++
	r.mu.Unlock()
}
func (r *registry) issue(v sessionRequest, now time.Time) (sessionGrant, error) {
	r.mutation.Lock()
	defer r.mutation.Unlock()
	if v.Account == 0 || len(v.Login) < 16 || len(v.Login) > 128 || len(v.Request) < 16 || len(v.Request) > 128 || v.MTU < 512 || v.MTU > 1400 {
		return sessionGrant{}, errors.New("invalid authenticated login binding")
	}
	r.reap(now)
	r.mu.RLock()
	raw, _ := json.Marshal(v)
	fingerprint := sha256.Sum256(raw)
	if prior, ok := r.issued[v.Request]; ok {
		s := r.byID[prior.member]
		if prior.fingerprint != fingerprint {
			r.mu.RUnlock()
			return sessionGrant{}, errors.New("request identity conflict")
		}
		if s == nil || s.closed.Load() || s.tokenUsed || now.UnixMilli() >= s.grant.Expires {
			r.mu.RUnlock()
			return sessionGrant{}, errors.New("original authorization is no longer redeemable")
		}
		grant := s.grant
		r.mu.RUnlock()
		return grant, nil
	}
	old := r.account[v.Account]
	count := len(r.byID)
	next := r.next
	retryCount := len(r.issued)
	r.mu.RUnlock()
	if retryCount >= max(1024, r.max*4) {
		return sessionGrant{}, errors.New("authorization retry cache capacity; retry after expiry")
	}
	if (count >= r.max && old == 0) || next >= 65535 {
		return sessionGrant{}, errors.New("session capacity")
	}
	if old != 0 {
		r.revoke(old)
	}
	secretHex := randomHex(32)
	secret, _ := hex.DecodeString(secretHex)
	seed, _ := hex.DecodeString(randomHex(8))
	sid := binary.BigEndian.Uint64(seed)
	if sid == 0 {
		return sessionGrant{}, errors.New("invalid entropy")
	}
	r.mu.Lock()
	r.next++
	id := r.next
	grant := sessionGrant{Boot: r.boot, Member: id, Session: sid, Secret: secretHex, Token: randomHex(32), Expires: now.Add(time.Minute).UnixMilli(), MTU: v.MTU}
	label := fmt.Sprintf("product/boot-%s/member-%d", r.boot, id)
	s := &relaySession{grant: grant, owner: binding{Account: v.Account, Login: v.Login}, secret: secret, tokenHash: sha256.Sum256([]byte(grant.Token)), created: now,
		up: newCodec(secret, label+"/to-relay", sid, 1, v.MTU, r.assemblyBudget), down: newCodec(secret, label+"/from-relay", sid, 1, v.MTU, r.assemblyBudget),
		q: newBudgetQueue(r.queueBudget), probes: newControlQueue(r.queueBudget), guard: newEdgeGuard(), report: reported{Probe: map[int]int64{}}}
	s.udp = &sessionUDP{probes: map[int]time.Time{}}
	r.byID[id] = s
	r.tokens[s.tokenHash] = s
	r.account[v.Account] = id
	// Request replay protection exceeds the authenticated HTTP time window.
	// A fresh launcher authorization uses a fresh random request ID.
	r.issued[v.Request] = issuedRequest{fingerprint, id, now.Add(5 * time.Minute).UnixMilli()}
	r.mu.Unlock()
	r.authority.mu.Lock()
	r.authority.allowed[id] = true
	r.authority.mu.Unlock()
	return grant, nil
}
func (r *registry) redeem(token string, now time.Time) (*relaySession, error) {
	if len(token) != 64 {
		return nil, errors.New("authentication required")
	}
	hash := sha256.Sum256([]byte(token))
	r.mu.Lock()
	defer r.mu.Unlock()
	s := r.tokens[hash]
	if s == nil || s.closed.Load() || s.tokenUsed || now.UnixMilli() >= s.grant.Expires {
		return nil, errors.New("expired or consumed launch authorization")
	}
	s.tokenUsed = true
	delete(r.tokens, hash)
	return s, nil
}
func (r *registry) lookup(id uint32) *relaySession {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.byID[id]
}
func (r *registry) bind(id uint32, v binding) error {
	r.mutation.Lock()
	defer r.mutation.Unlock()
	s := r.lookup(id)
	if s == nil || s.closed.Load() || v.Account != s.owner.Account || v.Login != s.owner.Login || v.Actor == 0 || v.Actor >= 65535 || v.Key == 0 || v.Channel == 0 {
		return errors.New("native binding does not match authenticated login")
	}
	r.authority.mu.RLock()
	_, active := r.authority.members[id]
	r.authority.mu.RUnlock()
	if active && s.owner != v {
		return errors.New("revoke active routes before character replacement")
	}
	r.mu.Lock()
	if s.owner != v {
		s.owner = v
		r.view++
	}
	r.mu.Unlock()
	return nil
}
func isSessionProbe(h Header, id uint32) bool {
	return h.Kind == 2 && h.Source == id && h.Target == id && h.Scope == ^uint64(0) && h.Epoch == 1
}
