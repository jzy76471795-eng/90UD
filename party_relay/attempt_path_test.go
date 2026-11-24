package main

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"
)

func selectedUDP(now time.Time,port int)*sessionUDP {
	return &sessionUDP{endpoint:&net.UDPAddr{IP:net.IPv4(127,0,0,1),Port:port},generation:"authenticated",selected:true,selectedAt:now,lastReceive:now}
}
func TestFixedPathExpiryRebindAndStaleFailureIsolation(t *testing.T) {
	now:=time.Now();s:=&relaySession{udp:selectedUDP(now,10001)}
	first:=s.transport(8,1,now)
	if first.Mode!="udp"||first.Failed {t.Fatal("verified initial UDP not chosen")}
	s.udp.endpoint.Port=10002
	failed:=s.transport(8,1,now)
	if !failed.Failed||failed.Revision==first.Revision {t.Fatal("endpoint change did not abort attempt")}
	s.udp.endpoint.Port=10001
	if !s.transport(8,1,now).Failed {t.Fatal("old attempt revived")}
	fresh:=s.transport(9,2,now)
	if fresh.Failed||fresh.Mode!="udp"||s.failTransport(first) {t.Fatal("stale failure poisoned fresh attempt")}
	if !s.transport(9,2,now.Add(2*time.Second)).Failed {t.Fatal("expired UDP lease silently used WSS")}
}
func TestFixedWSSPathDoesNotAdoptLaterUDP(t *testing.T) {
	s:=&relaySession{};now:=time.Now()
	first:=s.transport(3,1,now);s.udp=selectedUDP(now,10001)
	if got:=s.transport(3,1,now);got!=first||got.Mode!="wss" {t.Fatal("live WSS attempt switched carrier")}
	if s.transport(4,2,now).Mode!="udp" {t.Fatal("fresh attempt could not choose UDP")}
}
type failAfterOneDatagram struct{calls int}
func(*failAfterOneDatagram)SetWriteDeadline(time.Time)error{return nil}
func(w *failAfterOneDatagram)WriteToUDP(b []byte,_ *net.UDPAddr)(int,error){w.calls++;if w.calls>1{return 0,errors.New("owned injected second-fragment failure")};return len(b),nil}
func TestPartialUDPNativeWriteFailsAttemptWithoutCountingSuccess(t *testing.T) {
	r:=newRegistry(make([]byte,32),2)
	g,err:=r.issue(sessionRequest{Account:1,Login:randomHex(16),Request:randomHex(16),MTU:512},time.Now())
	if err!=nil {t.Fatal(err)}
	s:=r.lookup(g.Member);s.connected.Store(true);s.udp=selectedUDP(time.Now(),10001)
	output:=&failAfterOneDatagram{}
	v:=&service{registry:r,udp:&relayUDP{output:output}}
	p:=Packet{H:Header{Kind:1,Source:2,Target:g.Member,Scope:5,Epoch:1},Data:make([]byte,8192),At:time.Now()}
	if err=v.writeFrames(nil,s,p);!errors.Is(err,errAttemptPath) {t.Fatal("partial UDP failure hidden",err)}
	if output.calls!=2||v.nativeDropped.Load()!=1||v.nativePartial.Load()!=1||v.udp.nativeSent.Load()!=0||s.closed.Load() {t.Fatal("partial failure counters or control lifetime incorrect")}
	if !s.transport(5,1,time.Now()).Failed {t.Fatal("failed native attempt remained authorized")}
	if err=v.writeFrames(nil,s,p);!errors.Is(err,errAttemptPath)||output.calls!=2 {t.Fatal("failed attempt attempted fallback/retry")}
}
func TestEdgeUDPWriteFailureRetainsControlAndDoesNotCountSent(t *testing.T) {
	e,parent,fail:=nativeEdgeFixture(t)
	e.up=newCodec(make([]byte,32),"fixture",1,1,512,&Budget{limit:512*1024})
	e.control=newControlQueue(&Budget{limit:16*1024})
	v:=peerView(1);v.Transport=pathView{Scope:9,Epoch:1,Mode:"udp",Revision:1}
	if err:=e.installRemote(parent,v,fail);err!=nil {t.Fatal(err)}
	n:=e.native;n.Installed=1;e.nativeExchange(n)
	conn,err:=net.DialUDP("udp4",nil,&net.UDPAddr{IP:net.IPv4(127,0,0,1),Port:12345})
	if err!=nil {t.Fatal(err)};conn.Close()
	e.udp=&edgeUDP{conn:conn,generation:"authenticated",selected:true,selectedAt:time.Now(),verified:map[int]time.Time{64:time.Now(),512:time.Now(),8192:time.Now()}}
	ctx,cancel:=context.WithCancel(parent);done:=make(chan error,1)
	go func(){done<-e.writer(ctx)}()
	e.data.put(Packet{H:Header{Kind:1,Source:1,Target:2,Scope:9,Epoch:1},Data:make([]byte,8192),At:time.Now()})
	deadline:=time.Now().Add(time.Second)
	for e.nativeDropped.Load()==0&&time.Now().Before(deadline){time.Sleep(time.Millisecond)}
	e.mu.RLock();failed:=e.pathFailed;e.mu.RUnlock()
	if !failed||e.sent.Load()!=0||e.nativeDropped.Load()!=1 {cancel();t.Fatal("edge reported successful UDP failure")}
	select{case err:= <-done:cancel();t.Fatal("path failure closed writer",err);default:}
	cancel();<-done
}
