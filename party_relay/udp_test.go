package main

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/pem"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func waitUDP(t *testing.T, seconds int, predicate func() bool) {
	t.Helper()
	end := time.Now().Add(time.Duration(seconds) * time.Second)
	for !predicate() {
		if time.Now().After(end) {
			t.Fatal("bounded UDP condition timeout")
		}
		time.Sleep(10 * time.Millisecond)
	}
}
func udpHarness(t *testing.T,limits ...udpIngressLimits) *serviceHarness {
	h := harness(t)
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	if err = h.s.attachUDP(conn, conn.LocalAddr().String(),limits...); err != nil {
		conn.Close()
		t.Fatal(err)
	}
	return h
}
func productTestEdge(t *testing.T, h *serviceHarness, g sessionGrant, actor uint32) (*edgeClient, *net.UDPConn) {
	t.Helper()
	scratch := os.Getenv("TEMP")
	if !filepath.IsAbs(scratch) || strings.ToUpper(filepath.VolumeName(scratch)) != "E:" {
		t.Fatal("test artifacts require absolute E scratch")
	}
	cert := filepath.Join(scratch, "udp-test-ca-"+randomHex(8)+".pem")
	if err := os.WriteFile(cert, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: h.http.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	e, err := dialEdge(ctx, edgeStartup{Grant: g, Relay: h.http.URL, CA: cert})
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	native, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		e.close()
		cancel()
		t.Fatal(err)
	}
	e.nativeExchange(nativeReport{Actor: actor, Socket: 100 + actor, Port: uint16(native.LocalAddr().(*net.UDPAddr).Port), Context: 1})
	failures := make(chan error, 16)
	var workers sync.WaitGroup
	run := func(f func() error) {
		workers.Add(1)
		go func() {
			defer workers.Done()
			if err := f(); err != nil {
				select {
				case failures <- err:
				default:
				}
			}
		}()
	}
	run(func() error { return e.writer(ctx) })
	run(func() error { return e.reader(ctx) })
	run(func() error { return e.poll(ctx, failures) })
	run(func() error {
		ticker := time.NewTicker(20 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return nil
			case <-ticker.C:
				e.mu.RLock()
				n := e.native
				n.Installed = e.published
				e.mu.RUnlock()
				e.nativeExchange(n)
			}
		}
	})
	t.Cleanup(func() { cancel(); e.close(); native.Close(); workers.Wait() })
	return e, native
}
func nativeRoundTrip(t *testing.T, from, to *edgeClient, a, b *net.UDPConn, size int) {
	t.Helper()
	p := bytes.Repeat([]byte{byte(size%251 + 1)}, size)
	from.mu.RLock()
	n := from.nodes[to.cfg.Grant.Member]
	var port int
	if n != nil {
		port = int(n.Route.Local)
	}
	from.mu.RUnlock()
	if port == 0 {
		t.Fatal("native adapter route absent")
	}
	if _, err := a.WriteToUDP(p, &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port}); err != nil {
		t.Fatal(err)
	}
	b.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf := make([]byte, 8193)
	sizeRead, _, err := b.ReadFromUDP(buf)
	if err != nil || !bytes.Equal(p, buf[:sizeRead]) {
		t.Fatalf("opaque native boundary size=%d: %v", size, err)
	}
}
func TestProductUDPTLSReturnProofEndpointAndReplay(t *testing.T) {
	h := udpHarness(t)
	g := h.grant(1)
	e, native := productTestEdge(t, h, g, 101)
	_ = native
	h.bind(g, 1, 101, 71, 10)
	waitUDP(t, 5, func() bool { return e.udpDestination(false) != nil })
	s := h.s.registry.lookup(g.Member)
	s.udp.mu.Lock()
	old := *s.udp.endpoint
	generation := s.udp.generation
	s.udp.mu.Unlock()
	if generation == "" {
		t.Fatal("UDP selected without TLS receipt")
	}
	// Valid encrypted data from a different local port cannot replace an
	// established endpoint, even with this synthetic session's own key.
	stranger, err := net.DialUDP("udp4", nil, h.s.udp.conn.LocalAddr().(*net.UDPAddr))
	if err != nil {
		t.Fatal(err)
	}
	defer stranger.Close()
	frames, err := e.up.encodeKind(g.Member, g.Member, ^uint64(0), 1, bytes.Repeat([]byte{0x37}, 64), 2)
	if err != nil {
		t.Fatal(err)
	}
	before := h.s.udp.rejected.Load()
	stranger.Write(frames[0])
	waitUDP(t, 2, func() bool { return h.s.udp.rejected.Load() > before })
	s.udp.mu.Lock()
	unchanged := sameUDP(s.udp.endpoint, &old)
	s.udp.mu.Unlock()
	if !unchanged {
		t.Fatal("unauthorized UDP endpoint replacement")
	}
	// Rebind the actual product edge. It must repeat TLS + returned UDP proof.
	e.resetUDP()
	waitUDP(t, 6, func() bool { return e.udpDestination(false) != nil })
	s.udp.mu.Lock()
	changed := !sameUDP(s.udp.endpoint, &old) && s.udp.generation != generation
	s.udp.mu.Unlock()
	if !changed {
		t.Fatal("new endpoint not independently confirmed")
	}
	before = h.s.udp.rejected.Load()
	stranger.Write(frames[0])
	waitUDP(t, 2, func() bool { return h.s.udp.rejected.Load() > before })
	// A recorded challenge cannot be used again after its TLS confirmation.
	secret, _ := hex.DecodeString(g.Secret)
	captured := challengeFrame(&relaySession{grant: g, secret: secret}, 1, generation, "")
	before = h.s.udp.rejected.Load()
	stranger.Write(captured)
	waitUDP(t, 2, func() bool { return h.s.udp.rejected.Load() > before })
}
func TestProductUDPFixedAttemptBlackholeMixedLegAndFreshRecovery(t *testing.T) {
	h := udpHarness(t)
	// A bounded, owned application-layer NAT path supplies silent loss without
	// changing firewall/VPN/OS settings. The server still owns a real UDP socket.
	proxy, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	h.s.udp.public = proxy.LocalAddr().String()
	var blocked atomic.Bool
	var blockedMember atomic.Uint32
	var proxyDone sync.WaitGroup
	proxyDone.Add(1)
	go func() {
		defer proxyDone.Done()
		buf := make([]byte, 1501)
		clients := map[uint32]*net.UDPAddr{}
		relay := h.s.udp.conn.LocalAddr().(*net.UDPAddr)
		for {
			size, addr, err := proxy.ReadFromUDP(buf)
			if err != nil {
				return
			}
			raw := buf[:size]
			fromRelay := sameUDP(addr, relay)
			var member uint32
			if len(raw) == 128 && string(raw[:4]) == "RUC1" {
				member = uint32(raw[8])<<24 | uint32(raw[9])<<16 | uint32(raw[10])<<8 | uint32(raw[11])
			} else {
				header, err := parseHeader(raw)
				if err != nil {
					continue
				}
				if fromRelay {
					member = header.Target
				} else {
					member = header.Source
				}
			}
			if blocked.Load() || (blockedMember.Load() != 0 && blockedMember.Load() == member) {
				continue
			}
			if fromRelay {
				if dest := clients[member]; dest != nil {
					proxy.WriteToUDP(raw, dest)
				}
			} else {
				copyAddr := *addr
				clients[member] = &copyAddr
				proxy.WriteToUDP(raw, relay)
			}
		}
	}()
	t.Cleanup(func() { proxy.Close(); proxyDone.Wait() })
	a, b := h.grant(1), h.grant(2)
	h.bind(a, 1, 101, 71, 10)
	h.bind(b, 2, 102, 72, 10)
	ea, na := productTestEdge(t, h, a, 101)
	eb, nb := productTestEdge(t, h, b, 102)
	waitUDP(t, 6, func() bool { return ea.udpDestination(false) != nil && eb.udpDestination(false) != nil })
	var routeMu sync.Mutex
	route := []Member{{ID: a.Member, Scope: 9, Epoch: 1, Active: true}, {ID: b.Member, Scope: 9, Epoch: 1, Active: true}}
	if code, _ := h.routes(route, nil); code != 200 {
		t.Fatal("authority")
	}
	// Renew the actual signed authority and retain real DLL installation gating.
	ctx, cancel := context.WithCancel(context.Background())
	var keeper sync.WaitGroup
	keeper.Add(1)
	go func() {
		defer keeper.Done()
		tick := time.NewTicker(500 * time.Millisecond)
		defer tick.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-tick.C:
				routeMu.Lock()
				h.routes(route, nil)
				routeMu.Unlock()
			}
		}
	}()
	t.Cleanup(func() { cancel(); keeper.Wait() })
	newAttempt := func(scope, epoch uint64, amode, bmode string) {
		routeMu.Lock()
		route = []Member{{ID: a.Member, Scope: scope, Epoch: epoch, Active: true}, {ID: b.Member, Scope: scope, Epoch: epoch, Active: true}}
		code, _ := h.routes(route, nil)
		routeMu.Unlock()
		if code != 200 {
			t.Fatal("new attempt authority")
		}
		waitUDP(t, 6, func() bool {
			ea.mu.RLock()
			ap := ea.remote.Transport
			ea.mu.RUnlock()
			eb.mu.RLock()
			bp := eb.remote.Transport
			eb.mu.RUnlock()
			header := Header{Kind: 1, Source: a.Member, Target: b.Member, Scope: scope, Epoch: epoch}
			return ap.Scope == scope && bp.Scope == scope && ap.Mode == amode && bp.Mode == bmode && ea.permits(header) && eb.permits(header)
		})
	}
	newAttempt(9, 1, "udp", "udp")
	for _, size := range []int{64, 512, 8192} {
		nativeRoundTrip(t, ea, eb, na, nb, size)
		nativeRoundTrip(t, eb, ea, nb, na, size)
	}
	if h.s.udp.nativeReceived.Load() != 6 || h.s.udp.nativeSent.Load() != 6 {
		t.Fatal("native UDP carrier not exercised")
	}
	ea.mu.RLock()
	oldPort := ea.nodes[b.Member].Route.Local
	ea.mu.RUnlock()
	blockedMember.Store(a.Member)
	waitUDP(t, 4, func() bool { return h.s.registry.lookup(a.Member).transport(9, 1, time.Now()).Failed })
	waitUDP(t, 3, func() bool {
		return !ea.permits(Header{Kind: 1, Source: a.Member, Target: b.Member, Scope: 9, Epoch: 1})
	})
	sent := ea.sent.Load()
	na.WriteToUDP([]byte("must not fall back"), &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: int(oldPort)})
	nb.SetReadDeadline(time.Now().Add(150 * time.Millisecond))
	if _, _, err = nb.ReadFromUDP(make([]byte, 64)); err == nil {
		t.Fatal("failed native attempt silently changed carrier")
	}
	if ea.sent.Load() != sent {
		t.Fatal("dropped old packet counted as sent")
	}
	newAttempt(10, 2, "wss", "udp")
	for _, size := range []int{64, 512, 8192} {
		nativeRoundTrip(t, ea, eb, na, nb, size)
		nativeRoundTrip(t, eb, ea, nb, na, size)
	}
	blocked.Store(true)
	waitUDP(t, 4, func() bool { return h.s.registry.lookup(b.Member).transport(10, 2, time.Now()).Failed })
	newAttempt(11, 3, "wss", "wss")
	nativeRoundTrip(t, ea, eb, na, nb, 8192)
	nativeRoundTrip(t, eb, ea, nb, na, 8192)
	blocked.Store(false)
	blockedMember.Store(0)
	waitUDP(t, 6, func() bool { return ea.udpDestination(false) != nil && eb.udpDestination(false) != nil })
	before := h.s.udp.nativeSent.Load()
	nativeRoundTrip(t, ea, eb, na, nb, 8192)
	nativeRoundTrip(t, eb, ea, nb, na, 8192)
	if h.s.udp.nativeSent.Load() != before {
		t.Fatal("same WSS attempt switched to recovered UDP")
	}
	newAttempt(12, 4, "udp", "udp")
	nativeRoundTrip(t, ea, eb, na, nb, 8192)
	nativeRoundTrip(t, eb, ea, nb, na, 8192)
	if h.s.udp.nativeSent.Load() != before+2 {
		t.Fatal("fresh attempt did not use recovered UDP")
	}
	if ea.failed.Load() != 0 || eb.failed.Load() != 0 || !h.s.registry.lookup(a.Member).connected.Load() || !h.s.registry.lookup(b.Member).connected.Load() {
		t.Fatal("path failure killed control session")
	}
}
