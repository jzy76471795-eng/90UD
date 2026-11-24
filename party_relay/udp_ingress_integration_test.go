package main

import (
 "net"
 "testing"
 "time"
)

func TestAnonymousUDPNoiseCannotConsumeEstablishedNativeBudget(t *testing.T) {
 h:=udpHarness(t,udpIngressLimits{UnknownGlobal:64,UnknownSource:4})
 a,b:=h.grant(1),h.grant(2)
 h.bind(a,1,101,71,10);h.bind(b,2,102,72,10)
 ea,na:=productTestEdge(t,h,a,101);eb,nb:=productTestEdge(t,h,b,102)
 waitUDP(t,7,func()bool{return ea.udpDestination(false)!=nil&&eb.udpDestination(false)!=nil})
 if code,_:=h.routes([]Member{{ID:a.Member,Scope:19,Epoch:1,Active:true},{ID:b.Member,Scope:19,Epoch:1,Active:true}},nil);code!=200 {t.Fatal("authority")}
 header:=Header{Kind:1,Source:a.Member,Target:b.Member,Scope:19,Epoch:1}
 waitUDP(t,3,func()bool{return ea.permits(header)&&eb.permits(header)})
 noise,err:=net.DialUDP("udp4",nil,h.s.udp.conn.LocalAddr().(*net.UDPAddr))
 if err!=nil {t.Fatal(err)}
 defer noise.Close()
 done:=make(chan struct{})
 go func(){defer close(done);for i:=0;i<600;i++ {noise.Write(make([]byte,128));time.Sleep(time.Millisecond)}}()
 before:=h.s.udp.nativeSent.Load()
 for i:=0;i<25;i++ {nativeRoundTrip(t,ea,eb,na,nb,64);nativeRoundTrip(t,eb,ea,nb,na,64);time.Sleep(20*time.Millisecond)}
 <-done
 if h.s.udp.limited.Load()<100 {t.Fatal("anonymous admission not exercised")}
 if h.s.udp.nativeSent.Load()!=before+50 {t.Fatal("anonymous noise disrupted native carrier")}
 if h.s.registry.lookup(a.Member).transport(19,1,time.Now()).Failed || h.s.registry.lookup(b.Member).transport(19,1,time.Now()).Failed {
 for _,s:=range []*relaySession{h.s.registry.lookup(a.Member),h.s.registry.lookup(b.Member)} {
 s.udp.mu.Lock();t.Logf("member=%d selected=%t selected_age=%v receive_age=%v",s.grant.Member,s.udp.selected,time.Since(s.udp.selectedAt),time.Since(s.udp.lastReceive));s.udp.mu.Unlock()
 }
 t.Fatal("noise changed established attempt")
 }
}
