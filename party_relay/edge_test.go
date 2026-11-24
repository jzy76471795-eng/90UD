package main

import (
	"context"
	"net"
	"testing"
	"time"
)

func nativeEdgeFixture(t *testing.T) (*edgeClient, context.Context, chan error) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	e := &edgeClient{cfg: edgeStartup{Grant: sessionGrant{Boot: "boot", Member: 1}}, nodes: map[uint32]*edgeRoute{}, data: newBudgetQueue(&Budget{limit: 256 * 1024}), down: newCodec(make([]byte, 32), "fixture", 1, 1, 512, &Budget{limit: 512 * 1024})}
	e.native = nativeReport{Actor: 10, Context: 1, Socket: 97, Port: 12345}
	e.nativeAt = time.Now()
	failures := make(chan error, 8)
	t.Cleanup(func() {
		cancel()
		e.mu.Lock()
		for _, n := range e.nodes {
			n.Conn.Close()
		}
		for _, n := range e.retired {
			n.Node.Conn.Close()
		}
		e.mu.Unlock()
	})
	return e, ctx, failures
}
func peerView(version uint64) remoteView {
	return remoteView{Boot: "boot", Member: 1, Binding: binding{Actor: 10}, Version: version, Active: true, Lease: 750, Peers: []localRoute{{Member: 2, Actor: 20, Key: 222, IP: "198.18.0.2", Port: 20002, Scope: 9, Epoch: 1}}}
}
func TestNativeAcknowledgementAndPendingOnlyView(t *testing.T) {
	e, ctx, fail := nativeEdgeFixture(t)
	if err := e.installRemote(ctx, peerView(1), fail); err != nil {
		t.Fatal(err)
	}
	h := Header{Kind: 1, Source: 1, Target: 2, Scope: 9, Epoch: 1}
	if e.permits(h) {
		t.Fatal("data before DLL installation acknowledgement")
	}
	n := e.native
	n.Installed = 1
	if v := e.nativeExchange(n); v.Version != 1 || len(v.Routes) != 1 {
		t.Fatal("native table not published")
	}
	if !e.permits(h) {
		t.Fatal("installed native route rejected")
	}
	old := e.nodes[2]
	if err := e.installRemote(ctx, peerView(2), fail); err != nil {
		t.Fatal(err)
	}
	if e.nodes[2] != old || !e.permits(h) {
		t.Fatal("pending-only view disrupted established route")
	}
	n.Installed = 3
	e.nativeExchange(n)
	if e.permits(h) {
		t.Fatal("future acknowledgement accepted")
	}
	n.Installed = 2
	e.nativeExchange(n)
	v := peerView(3)
	v.Peers[0].Epoch = 2
	if err := e.installRemote(ctx, v, fail); err != nil {
		t.Fatal(err)
	}
	h.Epoch = 2
	if e.permits(h) {
		t.Fatal("new epoch authorized by old installation")
	}
	n.Installed = 3
	e.nativeExchange(n)
	if !e.permits(h) {
		t.Fatal("new epoch acknowledgement failed")
	}
	if len(e.retired) != 1 {
		t.Fatal("old native port was not quarantined")
	}
}
func TestNativeRetirementDropsOldTrafficAndBounds(t *testing.T) {
	e, ctx, fail := nativeEdgeFixture(t)
	sender, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer sender.Close()
	e.native.Port = uint16(sender.LocalAddr().(*net.UDPAddr).Port)
	if err = e.installRemote(ctx, peerView(1), fail); err != nil {
		t.Fatal(err)
	}
	old := e.nodes[2]
	v := peerView(2)
	v.Active = false
	v.Peers = nil
	if err = e.installRemote(ctx, v, fail); err != nil {
		t.Fatal(err)
	}
	for range 3 {
		if _, err = sender.WriteToUDP([]byte("retired"), old.Conn.LocalAddr().(*net.UDPAddr)); err != nil {
			t.Fatal(err)
		}
	}
	time.Sleep(25 * time.Millisecond)
	if _, ok := e.data.pop(); ok {
		t.Fatal("retired datagram forwarded")
	}
	if e.retired[0].Node != old {
		t.Fatal("old listener closed before handoff")
	}
	e.retired[0].At = time.Now().Add(-3 * time.Second)
	e.reap()
	if len(e.retired) != 0 {
		t.Fatal("retired listener leaked")
	}
	if _, err = old.Conn.WriteToUDP([]byte{1}, sender.LocalAddr().(*net.UDPAddr)); err == nil {
		t.Fatal("retired port still open")
	}
	nodes := []*edgeRoute{}
	for range 25 {
		c, x := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
		if x != nil {
			t.Fatal(x)
		}
		nodes = append(nodes, &edgeRoute{Conn: c})
	}
	e.retire(nodes)
	if e.failed.Load() == 0 || len(e.retired) > 24 {
		t.Fatal("retirement unbounded")
	}
}
func TestNativeContextInvalidatesPublishedTable(t *testing.T) {
	e, ctx, fail := nativeEdgeFixture(t)
	if err := e.installRemote(ctx, peerView(1), fail); err != nil {
		t.Fatal(err)
	}
	n := e.native
	n.Installed = 1
	e.nativeExchange(n)
	n.Context = 2
	n.Installed = 0
	v := e.nativeExchange(n)
	if v.Version != 0 || v.Until != 0 || len(v.Routes) != 0 {
		t.Fatal("old table crossed native context")
	}
	if e.permits(Header{Kind: 1, Source: 1, Target: 2, Scope: 9, Epoch: 1}) {
		t.Fatal("old native context authorized")
	}
}
