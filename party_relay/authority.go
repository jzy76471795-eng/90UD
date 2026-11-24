package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"sort"
	"sync"
	"time"
)

type Member struct {
	ID     uint32 `json:"id"`
	Scope  uint64 `json:"scope"`
	Epoch  uint64 `json:"epoch"`
	Active bool   `json:"active"`
}
type Snapshot struct {
	Boot    string   `json:"boot"`
	Version uint64   `json:"version"`
	Issued  int64    `json:"issued_ms"`
	Lease   int64    `json:"lease_ms"`
	Members []Member `json:"members"`
	Pending []Member `json:"pending"`
	MAC     string   `json:"mac"`
}

func (s Snapshot) body() []byte {
	// Authority MAC v1: fixed struct field order, decimal integers, ASCII boot,
	// explicit empty arrays, ID-sorted rows and an empty MAC string.
	s.MAC=""
	s.Members=append([]Member{},s.Members...)
	s.Pending=append([]Member{},s.Pending...)
	sort.Slice(s.Members,func(i,j int)bool{return s.Members[i].ID<s.Members[j].ID})
	sort.Slice(s.Pending,func(i,j int)bool{return s.Pending[i].ID<s.Pending[j].ID})
	b,_:=json.Marshal(s);return b
}
func (s *Snapshot) sign(key []byte) {
	m := hmac.New(sha256.New, key)
	m.Write(s.body())
	s.MAC = hex.EncodeToString(m.Sum(nil))
}

type Authority struct {
	mu                 sync.RWMutex
	boot               string
	version            uint64
	expires, heartbeat time.Time
	members            map[uint32]Member
	pending            map[uint32]Member
	tombstones         map[uint32]uint64
	key                []byte
	allowed            map[uint32]bool
	purge              func([]uint32)
}

func newAuthority(boot string, key []byte, ids []uint32, purge func([]uint32)) *Authority {
	a := &Authority{boot: boot, key: key, members: map[uint32]Member{}, tombstones: map[uint32]uint64{}, allowed: map[uint32]bool{}, purge: purge}
	for _, id := range ids {
		a.allowed[id] = true
	}
	return a
}
func (a *Authority) apply(s Snapshot, now time.Time) error {
	m := hmac.New(sha256.New, a.key)
	m.Write(s.body())
	sig, e := hex.DecodeString(s.MAC)
	if e != nil || !hmac.Equal(sig, m.Sum(nil)) || s.Boot != a.boot || s.Lease <= 0 || s.Lease > 5000 || s.Issued > now.UnixMilli()+250 || now.UnixMilli()-s.Issued > 1000 || now.UnixMilli()-s.Issued >= s.Lease {
		return errors.New("untrusted/expired authority snapshot")
	}
	changed := false
	affected:=[]uint32{}
	a.mu.Lock()
	defer func() {
		a.mu.Unlock()
		if changed && a.purge != nil {
			a.purge(affected)
		}
	}()
	if s.Version <= a.version {
		return errors.New("stale authority version")
	}
	next := map[uint32]Member{}
	for _, r := range s.Members {
		old := a.members[r.ID]
		if !a.allowed[r.ID] || r.Scope == 0 || r.Epoch == 0 || next[r.ID].ID != 0 || r.Epoch <= a.tombstones[r.ID] || (old.ID != 0 && (r.Epoch < old.Epoch || (r.Epoch == old.Epoch && r.Scope != old.Scope))) {
			return errors.New("invalid/reused route generation")
		}
		next[r.ID] = r
	}
	// Pending reservations are ACKed with the same authenticated lease, but
	// never consulted by the data path. Existing parties keep their live route.
	pending := map[uint32]Member{}
	for _, r := range s.Pending {
		old := a.members[r.ID]
		if !a.allowed[r.ID] || r.Scope == 0 || r.Epoch == 0 || r.Active || pending[r.ID].ID != 0 || r.Epoch <= a.tombstones[r.ID] || (old.ID != 0 && r.Epoch <= old.Epoch) {
			return errors.New("invalid pending route reservation")
		}
		pending[r.ID] = r
	}
	changedScopes:=map[[2]uint64]bool{}
	for id, old := range a.members {
		if next[id] != old {
			if next[id].ID == 0 || next[id].Scope != old.Scope || next[id].Epoch != old.Epoch || (old.Active && !next[id].Active) {
				a.tombstones[id] = old.Epoch
			}
			changed = true
			changedScopes[[2]uint64{old.Scope,old.Epoch}]=true
		}
	}
	for id,old:=range a.members {
		if changedScopes[[2]uint64{old.Scope,old.Epoch}] {affected=append(affected,id)}
	}
	// Purge affected old scopes before ACK. Other teams retain their queues and
	// in-progress reassemblies. The global authority lock is released first.
	a.members = next
	a.pending = pending
	a.version = s.Version
	a.heartbeat = now
	a.expires = now.Add(time.Duration(min(s.Lease, s.Issued+s.Lease-now.UnixMilli())) * time.Millisecond)
	return nil
}
func (a *Authority) permits(h Header, now time.Time) bool {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.permitsLocked(h, now)
}
func (a *Authority) permitsLocked(h Header, now time.Time) bool {
	s, t := a.members[h.Source], a.members[h.Target]
	return now.Before(a.expires) && now.Sub(a.heartbeat) < 3*time.Second && h.Source != h.Target && s.ID != 0 && t.ID != 0 && s.Active && t.Active && s.Scope == t.Scope && s.Epoch == t.Epoch && h.Scope == s.Scope && h.Epoch == s.Epoch
}
func (a *Authority) route(source, target uint32, now time.Time) (Member, bool) {
	a.mu.RLock()
	s := a.members[source]
	a.mu.RUnlock()
	return s, a.permits(Header{Source: source, Target: target, Scope: s.Scope, Epoch: s.Epoch}, now)
}
func (a *Authority) ack() map[string]any {
	a.mu.RLock()
	defer a.mu.RUnlock()
	members, pending := map[uint32]Member{}, map[uint32]Member{}
	maxEpoch := uint64(0)
	for k, v := range a.members {
		members[k] = v
		maxEpoch = max(maxEpoch, v.Epoch)
	}
	for k, v := range a.pending {
		pending[k] = v
		maxEpoch = max(maxEpoch, v.Epoch)
	}
	for _, epoch := range a.tombstones {
		maxEpoch = max(maxEpoch, epoch)
	}
	return map[string]any{"boot": a.boot, "version": a.version, "members": members, "pending": pending, "max_epoch": maxEpoch, "expires_ms": a.expires.UnixMilli(), "revocation_purge_before_ack": true}
}
