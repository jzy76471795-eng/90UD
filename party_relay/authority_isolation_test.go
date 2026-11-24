package main

import (
 "bytes"
 "testing"
 "time"
)

func TestCanonicalAuthorityBodyHasFrozenCrossLanguageBytes(t *testing.T) {
 s:=Snapshot{Boot:"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",Version:2,Issued:1234567890000,Lease:5000,Members:[]Member{{2,7,3,true},{1,7,3,true}}}
 expected:=`{"boot":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","version":2,"issued_ms":1234567890000,"lease_ms":5000,"members":[{"id":1,"scope":7,"epoch":3,"active":true},{"id":2,"scope":7,"epoch":3,"active":true}],"pending":[],"mac":""}`
 if string(s.body())!=expected {t.Fatalf("canonical vector mismatch: %s",s.body())}
 other:=s;other.Pending=[]Member{};other.Members=[]Member{s.Members[1],s.Members[0]}
 if !bytes.Equal(s.body(),other.body()) {t.Fatal("row or nil-array representation affects MAC")}
}

func TestAuthorityRevocationPreservesUnrelatedQueueAndReassembly(t *testing.T) {
 r:=newRegistry(make([]byte,32),4)
 for i:=1;i<=4;i++ {if _,err:=r.issue(sessionRequest{Account:uint32(i),Login:randomHex(16),Request:randomHex(16),MTU:512},time.Now());err!=nil {t.Fatal(err)}}
 a:=r.authority;now:=time.Now()
 if err:=a.apply(snapshot(a,1,now,Member{1,9,1,true},Member{2,9,1,true},Member{3,10,1,true},Member{4,10,1,true}),now);err!=nil {t.Fatal(err)}
 for id,s:=range r.byID {
  scope:=uint64(9);if id>=3 {scope=10}
  s.q.put(Packet{H:Header{Source:id,Target:id%4+1,Scope:scope,Epoch:1},Data:[]byte("retained"),At:now})
  frames,err:=s.up.encodeKind(id,id%4+1,scope,1,make([]byte,8192),1)
  if err!=nil {t.Fatal(err)}
  if _,_,err=s.up.decode(frames[0],now,func(Header)bool{return true});err!=nil {t.Fatal(err)}
 }
 if err:=a.apply(snapshot(a,2,now,Member{1,9,1,true},Member{3,10,1,true},Member{4,10,1,true}),now);err!=nil {t.Fatal(err)}
 for id,s:=range r.byID {
  _,queued:=s.q.pop()
  if queued!=(id>=3)||(len(s.up.parts)>0)!=(id>=3) {t.Fatal("revocation affected wrong scope",id)}
 }
}
