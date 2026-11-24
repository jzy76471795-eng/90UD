package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"github.com/gorilla/websocket"
	"strings"
	"testing"
	"time"
)

func queuedProbeEdge() *edgeClient {
	return &edgeClient{cfg: edgeStartup{Grant: sessionGrant{Member: 1}},
		control: newControlQueue(&Budget{limit: 2 * (64 + 512 + 8192)}),
		pending: map[[32]byte]probeSent{}, verified: map[int]time.Time{}, wssVerified: map[int]time.Time{}}
}

func TestProbeBothCarriersFitWithoutConsumerScheduling(t *testing.T) {
	e := queuedProbeEdge()
	if err := e.probeTransport(false); err != nil {
		t.Fatal("first carrier", err)
	}
	if err := e.probeTransport(true); err != nil {
		t.Fatal("legitimate dual-carrier burst closed worker", err)
	}
	if e.control.failed() || len(e.pending) != 6 {
		t.Fatal("dual probe batch lost", len(e.pending))
	}
	if len(e.verified) != 0 || len(e.wssVerified) != 0 {
		t.Fatal("queue admission fabricated verification")
	}
	total := 0
	carriers := map[bool]map[int]bool{false: {}, true: {}}
	for {
		p, ok := e.control.pop()
		if !ok {
			break
		}
		total += len(p.Data)
		carriers[p.UDP][len(p.Data)] = true
	}
	if total != 2*(64+512+8192) || len(carriers[false]) != 3 || len(carriers[true]) != 3 {
		t.Fatal("incomplete probe coverage", total, carriers)
	}
}

func TestProbeCongestionPreservesSessionAndNativeQueue(t *testing.T) {
	e := queuedProbeEdge()
	e.data = newQueue()
	if err := e.probeTransport(false); err != nil {
		t.Fatal(err)
	}
	if err := e.probeTransport(true); err != nil {
		t.Fatal(err)
	}
	if err := e.probeTransport(false); err != nil {
		t.Fatal("replaceable probe overload killed worker", err)
	}
	if e.control.failed() || len(e.pending) != 6 || e.probeDropped.Load() != 3 {
		t.Fatal("probe admission/receipt rollback incorrect", len(e.pending), e.probeDropped.Load())
	}
	if !e.data.put(Packet{H: Header{Kind: 1}, Data: []byte("native"), At: time.Now()}) {
		t.Fatal("probe overload poisoned native queue")
	}
	if len(e.verified) != 0 || len(e.wssVerified) != 0 {
		t.Fatal("dropped measurements granted readiness")
	}
	e.control.clear()
	if e.control.budget.bytes.Load() != 0 {
		t.Fatal("probe budget leaked")
	}
	if err := e.probeTransport(true); err != nil || e.control.failed() {
		t.Fatal("drained queue did not recover", err)
	}
}

func TestProbeAgeAndPendingBoundsDoNotForgeOrClose(t *testing.T) {
	e := queuedProbeEdge()
	old := Packet{H: Header{Kind: 2, Source: 1, Target: 1, Scope: ^uint64(0), Epoch: 1}, Data: make([]byte, 64), At: time.Now().Add(-time.Second)}
	if e.control.putProbe(old) || e.control.failed() || e.control.budget.bytes.Load() != 0 {
		t.Fatal("old probe poisoned queue or budget")
	}
	native := newQueue()
	if native.put(Packet{H: Header{Kind: 1}, Data: old.Data, At: old.At}) || !native.failed() {
		t.Fatal("native stale-data fail-closed changed")
	}
	for i := byte(0); i < 18; i++ {
		e.pending[sha256.Sum256([]byte{i})] = probeSent{At: time.Now(), Size: 64}
	}
	if err := e.probeTransport(false); err != nil || e.control.failed() || len(e.pending) != 18 || e.probeDropped.Load() != 3 {
		t.Fatal("bounded outstanding measurements killed connection", err)
	}
	if len(e.verified) != 0 || len(e.wssVerified) != 0 {
		t.Fatal("pending capacity fabricated receipts")
	}
	e.control.fail()
	if err := e.probeTransport(false); err == nil {
		t.Fatal("explicitly closed queue was silently kept alive")
	}
}

func TestEdgeWriterDropsExpiredProbeButStillFailsExpiredNative(t *testing.T) {
	e := queuedProbeEdge()
	e.data = newQueue()
	p := Packet{H: Header{Kind: 2, Source: 1, Target: 1, Scope: ^uint64(0), Epoch: 1}, Data: make([]byte, 64), At: time.Now()}
	e.pending[sha256.Sum256(p.Data)] = probeSent{Size: 64, At: p.At}
	if !e.control.putProbe(p) {
		t.Fatal("setup probe")
	}
	e.control.mu.Lock()
	e.control.items[0].At = time.Now().Add(-time.Second)
	e.control.mu.Unlock()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- e.writer(ctx) }()
	await(t, func() bool { return e.probeDropped.Load() == 1 })
	select {
	case err := <-done:
		t.Fatal("stale probe stopped writer", err)
	default:
	}
	e.mu.RLock()
	pending := len(e.pending)
	verified := len(e.verified)
	e.mu.RUnlock()
	if pending != 0 || verified != 0 || e.control.failed() {
		t.Fatal("stale probe retained authority or closed queue")
	}
	// Force a queued native packet to age before waking the actual writer.
	e.data.mu.Lock()
	e.data.items = []Packet{{H: Header{Kind: 1}, Data: []byte{1}, At: time.Now().Add(-time.Second)}}
	e.data.bytes = 1
	e.data.mu.Unlock()
	e.data.wake <- struct{}{}
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "bounded edge upstream") {
			t.Fatal("native expired packet did not fail", err)
		}
	case <-time.After(time.Second):
		t.Fatal("native failure timed out")
	}
}

func TestServiceStaleProbeKeepsRealAuthenticatedWSSUsable(t *testing.T) {
	h := harness(t)
	g := h.grant(71)
	s := h.s.registry.lookup(g.Member)
	packet := Packet{H: Header{Kind: 2, Source: g.Member, Target: g.Member, Scope: ^uint64(0), Epoch: 1}, Data: make([]byte, 64), At: time.Now()}
	if !s.probes.putProbe(packet) {
		t.Fatal("setup queue")
	}
	s.probes.mu.Lock()
	s.probes.items[0].At = time.Now().Add(-time.Second)
	s.probes.mu.Unlock()
	edge := h.edge(g)
	payload := bytes.Repeat([]byte{0x5a}, 64)
	frames, err := edge.up.encodeKind(g.Member, g.Member, ^uint64(0), 1, payload, 2)
	if err != nil {
		t.Fatal(err)
	}
	for _, frame := range frames {
		if err = edge.c.WriteMessage(websocket.BinaryMessage, frame); err != nil {
			t.Fatal(err)
		}
	}
	if !bytes.Equal(edge.receive(t), payload) || s.closed.Load() || !s.connected.Load() || h.s.probeDropped.Load() != 1 {
		t.Fatal("stale probe broke authenticated echo/session")
	}
}

func TestServiceProbeQueueOverloadRetainsAuthenticatedMember(t *testing.T) {
	h := harness(t)
	g := h.grant(72)
	s := h.s.registry.lookup(g.Member)
	if redeemed, err := h.s.registry.redeem(g.Token, time.Now()); err != nil || redeemed != s {
		t.Fatal("authenticated grant redemption", err)
	}
	s.connected.Store(true) // Isolated decoder test; the WSS lifetime is covered above.
	for _, udp := range []bool{false, true} {
		for _, size := range []int{64, 512, 8192} {
			if !s.probes.putProbe(Packet{H: Header{Kind: 2, Source: g.Member, Target: g.Member, Scope: ^uint64(0), Epoch: 1}, Data: make([]byte, size), At: time.Now(), UDP: udp}) {
				t.Fatal("legitimate probe capacity")
			}
		}
	}
	secret, err := hex.DecodeString(g.Secret)
	if err != nil {
		t.Fatal(err)
	}
	label := fmt.Sprintf("product/boot-%s/member-%d/to-relay", g.Boot, g.Member)
	up := newCodec(secret, label, g.Session, 1, g.MTU, &Budget{limit: 512 * 1024})
	frames, err := up.encodeKind(g.Member, g.Member, ^uint64(0), 1, bytes.Repeat([]byte{0x33}, 64), 2)
	if err != nil {
		t.Fatal(err)
	}
	for _, frame := range frames {
		if h.s.inputFrame(s, frame, false) {
			t.Fatal("overflow echo was admitted")
		}
	}
	if s.closed.Load() || s.probes.failed() || s.q.failed() || h.s.probeDropped.Load() != 1 || h.s.congested.Load() != 0 {
		t.Fatal("measurement overload retired member or poisoned native queue", s.closed.Load(), s.probes.failed(), s.q.failed(), h.s.probeDropped.Load(), h.s.congested.Load())
	}
	s.probes.clear()
	if h.s.registry.queueBudget.bytes.Load() != 0 {
		t.Fatal("global queue budget leaked")
	}
}
