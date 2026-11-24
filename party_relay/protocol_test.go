package main

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"github.com/gorilla/websocket"
	"net/http/httptest"
	"testing"
	"time"
)

func codecs(mtu int) (*Codec, *Codec, *Budget) {
	b := &Budget{limit: 8 * 1024 * 1024}
	key := bytes.Repeat([]byte{0x5a}, 32)
	return newCodec(key, "up", 1, 1, mtu, b), newCodec(key, "up", 1, 1, mtu, b), b
}
func TestPendingReservationDoesNotReplaceOrAuthorizeData(t *testing.T) {
	now := time.Now()
	key := bytes.Repeat([]byte{4}, 32)
	a := newAuthority("pending-test", key, []uint32{1, 2, 3}, nil)
	s := Snapshot{Boot: a.boot, Version: 1, Issued: now.UnixMilli(), Lease: 5000,
		Members: []Member{{ID: 1, Scope: 1, Epoch: 1, Active: true}, {ID: 2, Scope: 1, Epoch: 1, Active: true}},
		Pending: []Member{{ID: 1, Scope: 2, Epoch: 2}, {ID: 2, Scope: 2, Epoch: 2}, {ID: 3, Scope: 2, Epoch: 2}}}
	s.sign(key)
	if e := a.apply(s, now); e != nil {
		t.Fatal(e)
	}
	if !a.permits(Header{Source: 1, Target: 2, Scope: 1, Epoch: 1}, now) {
		t.Fatal("existing route replaced by prepare")
	}
	if a.permits(Header{Source: 1, Target: 3, Scope: 2, Epoch: 2}, now) {
		t.Fatal("reservation authorized game data")
	}
	s.Version++
	s.Pending = nil
	s.sign(key)
	if e := a.apply(s, now); e != nil {
		t.Fatal(e)
	}
	if !a.permits(Header{Source: 2, Target: 1, Scope: 1, Epoch: 1}, now) {
		t.Fatal("prepare cancel revoked existing route")
	}
	s.Version++
	s.Pending = []Member{{ID: 1, Scope: 2, Epoch: 1}}
	s.sign(key)
	if e := a.apply(s, now); e == nil {
		t.Fatal("stale reservation accepted")
	}
}
func TestFragmentReorderReplay(t *testing.T) {
	tx, rx, b := codecs(512)
	p := bytes.Repeat([]byte{7, 2, 5, 0}, 2048)
	frames, e := tx.encode(1, 2, 4, 6, p)
	if e != nil {
		t.Fatal(e)
	}
	if len(frames) < 2 {
		t.Fatal("not fragmented")
	}
	var got []byte
	for i := len(frames) - 1; i >= 0; i-- {
		if len(frames[i]) > 512 {
			t.Fatal("MTU exceeded")
		}
		_, out, e := rx.decode(frames[i], time.Now(), func(Header) bool { return true })
		if e != nil {
			t.Fatal(e)
		}
		if out != nil {
			got = out
		}
	}
	if !bytes.Equal(p, got) || b.bytes.Load() != 0 {
		t.Fatal("datagram boundary or budget")
	}
	if _, _, e = rx.decode(frames[0], time.Now(), func(Header) bool { return true }); e == nil {
		t.Fatal("replay")
	}
}
func TestAuthBeforeStateAndWrongGeneration(t *testing.T) {
	tx, rx, b := codecs(512)
	frames, _ := tx.encode(1, 2, 4, 6, []byte{1, 2, 3})
	corrupt := append([]byte{}, frames[0]...)
	corrupt[40] ^= 1
	if _, _, e := rx.decode(corrupt, time.Now(), func(Header) bool { return true }); e == nil {
		t.Fatal("auth")
	}
	if b.bytes.Load() != 0 || rx.rx.high != 0 {
		t.Fatal("unauth allocation/replay change")
	}
	if _, _, e := rx.decode(frames[0], time.Now(), func(Header) bool { return false }); e == nil {
		t.Fatal("scope")
	}
	if rx.rx.high != 0 {
		t.Fatal("unauthorized replay change")
	}
	if _, _, e := rx.decode(frames[0], time.Now(), func(Header) bool { return true }); e != nil {
		t.Fatal(e)
	}
}
func TestBoundsTimeoutAndRevokeClear(t *testing.T) {
	tx, rx, b := codecs(128)
	if _, e := tx.encode(1, 2, 1, 1, make([]byte, 2049)); e == nil {
		t.Fatal("64 fragment cap")
	}
	tx, rx, b = codecs(512)
	now := time.Now()
	for i := 0; i < 8; i++ {
		frames, _ := tx.encode(1, 2, 1, 1, make([]byte, 8192))
		if _, _, e := rx.decode(frames[0], now, func(Header) bool { return true }); e != nil {
			t.Fatal(e)
		}
	}
	frames, _ := tx.encode(1, 2, 1, 1, make([]byte, 8192))
	if _, _, e := rx.decode(frames[0], now, func(Header) bool { return true }); e == nil {
		t.Fatal("incomplete datagram cap")
	}
	rx.mu.Lock()
	rx.prune(now.Add(time.Second))
	rx.mu.Unlock()
	if b.bytes.Load() != 0 {
		t.Fatal("expiry leak")
	}
	frames, _ = tx.encode(1, 2, 1, 1, make([]byte, 8192))
	rx.decode(frames[0], now.Add(time.Second), func(Header) bool { return true })
	rx.clear()
	if b.bytes.Load() != 0 {
		t.Fatal("revoke leak")
	}
}
func TestGlobalBudget(t *testing.T) {
	tx, rx, b := codecs(512)
	b.limit = 8191
	frames, _ := tx.encode(1, 2, 1, 1, make([]byte, 8192))
	if _, _, e := rx.decode(frames[0], time.Now(), func(Header) bool { return true }); e == nil || b.bytes.Load() != 0 {
		t.Fatal("global memory bound")
	}
}
func TestKeySeparation(t *testing.T) {
	tx, _, _ := codecs(512)
	frames, _ := tx.encode(1, 2, 1, 1, []byte{1})
	for _, change := range []string{"label", "session", "key"} {
		_, rx, _ := codecs(512)
		switch change {
		case "label":
			rx.label = "down"
		case "session":
			rx.session = 2
		case "key":
			rx.keyEpoch = 2
		}
		if _, _, e := rx.decode(frames[0], time.Now(), func(Header) bool { return true }); e == nil {
			t.Fatal(change)
		}
	}
}
func TestReplayWindow(t *testing.T) {
	w := Window{}
	for _, n := range []uint64{1, 3, 2, 1050} {
		if !w.accept(n) {
			t.Fatal(n)
		}
	}
	if w.accept(0) || w.accept(2) || w.accept(1050) || !w.accept(1049) {
		t.Fatal("window")
	}
	w = Window{}
	if !w.accept(^uint64(0)) || !w.accept(^uint64(0)-1023) || w.accept(^uint64(0)-1024) {
		t.Fatal("overflow")
	}
}
func snapshot(a *Authority, version uint64, now time.Time, members ...Member) Snapshot {
	s := Snapshot{Boot: a.boot, Version: version, Issued: now.UnixMilli(), Lease: 5000, Members: members}
	s.sign(a.key)
	return s
}
func TestAuthorityRevocationEpochAndLease(t *testing.T) {
	now := time.Now()
	purges := 0
	a := newAuthority("boot", bytes.Repeat([]byte{1}, 32), []uint32{1, 2, 3, 4}, func(_ []uint32) { purges++ })
	h := Header{Source: 1, Target: 2, Scope: 9, Epoch: 1}
	if a.permits(h, now) {
		t.Fatal("startup trusted disk")
	}
	s := snapshot(a, 1, now, Member{1, 9, 1, true}, Member{2, 9, 1, true}, Member{3, 10, 1, true}, Member{4, 10, 1, true})
	if e := a.apply(s, now); e != nil || !a.permits(h, now) {
		t.Fatal(e)
	}
	if a.permits(Header{Source: 1, Target: 3, Scope: 9, Epoch: 1}, now) {
		t.Fatal("cross team")
	}
	if a.apply(s, now) == nil {
		t.Fatal("snapshot replay")
	}
	s = snapshot(a, 2, now, Member{1, 9, 1, true})
	if e := a.apply(s, now); e != nil || a.permits(h, now) || purges != 1 {
		t.Fatal("revoke", e, purges)
	}
	if a.apply(snapshot(a, 3, now, Member{1, 9, 1, true}, Member{2, 9, 1, true}), now) == nil {
		t.Fatal("old epoch revived")
	}
	if e := a.apply(snapshot(a, 3, now, Member{1, 9, 2, true}, Member{2, 9, 2, true}), now); e != nil {
		t.Fatal(e)
	}
	h.Epoch = 2
	if !a.permits(h, now) || a.permits(h, now.Add(3*time.Second)) {
		t.Fatal("control heartbeat timeout")
	}
	bad := snapshot(a, 4, now, Member{1, 9, 2, true}, Member{2, 9, 2, true})
	bad.Boot = "another"
	bad.sign(a.key)
	if a.apply(bad, now) == nil {
		t.Fatal("boot reuse")
	}
}
func TestQueueBoundsAndIsolation(t *testing.T) {
	slow, normal := newQueue(), newQueue()
	p := Packet{Data: make([]byte, 1024), At: time.Now()}
	for i := 0; i < 256; i++ {
		if !slow.put(p) {
			t.Fatal(i)
		}
	}
	if slow.put(p) || !slow.failed() {
		t.Fatal("unbounded queue")
	}
	if !normal.put(p) {
		t.Fatal("other member blocked")
	}
	q := newQueue()
	if q.put(Packet{Data: []byte{1}, At: time.Now().Add(-101 * time.Millisecond)}) {
		t.Fatal("old queue accepted")
	}
	q = newQueue()
	if q.put(Packet{Data: make([]byte, 256*1024+1), At: time.Now()}) {
		t.Fatal("byte bound")
	}
}
func TestTLSStrictAndUpgradeFailure(t *testing.T) {
	server := httptest.NewTLSServer(nil)
	defer server.Close()
	d := websocket.Dialer{TLSClientConfig: &tls.Config{RootCAs: x509.NewCertPool(), MinVersion: tls.VersionTLS12}, HandshakeTimeout: time.Second}
	if c, _, e := d.Dial("wss"+server.URL[5:], nil); e == nil {
		c.Close()
		t.Fatal("untrusted certificate accepted")
	}
	pool := x509.NewCertPool()
	pool.AddCert(server.Certificate())
	d.TLSClientConfig.RootCAs = pool
	if c, response, e := d.Dial("wss"+server.URL[5:], nil); e == nil {
		c.Close()
		t.Fatal("non-upgrade accepted")
	} else if response == nil || response.StatusCode != 404 {
		t.Fatal("not upgrade failure", e)
	}
}
