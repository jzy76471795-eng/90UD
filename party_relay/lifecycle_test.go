package main

import (
	"strings"
	"testing"
	"time"
)

func TestExpiredUnredeemedAuthorizationsDoNotExhaustDirectory(t *testing.T) {
	r := newRegistry([]byte(strings.Repeat("k", 32)), 2)
	now := time.Now()
	for i := 1; i <= 4200; i++ {
		_, err := r.issue(sessionRequest{Request: randomHex(16), Account: uint32(i), Login: strings.Repeat("a", 32), MTU: 512}, now)
		if err != nil {
			t.Fatalf("grant %d: %v", i, err)
		}
		if len(r.byID) > 2 || len(r.tokens) > 2 || len(r.account) > 2 || len(r.issued) > 6 {
			t.Fatal("expired authorization retention grew", len(r.byID), len(r.issued))
		}
		now = now.Add(61 * time.Second)
	}
	r.mutation.Lock()
	r.reap(now.Add(6 * time.Minute))
	r.mutation.Unlock()
	if len(r.byID) != 0 || len(r.tokens) != 0 || len(r.account) != 0 || len(r.issued) != 0 || len(r.authority.allowed) != 0 {
		t.Fatal("expired directory was not reclaimed")
	}
}

func TestGrantReapingPreservesRedeemedLiveSessionAndRetryIdentity(t *testing.T) {
	r := newRegistry([]byte(strings.Repeat("k", 32)), 2)
	now := time.Now()
	request := sessionRequest{Request: randomHex(16), Account: 1, Login: strings.Repeat("a", 32), MTU: 512}
	grant, err := r.issue(request, now)
	if err != nil {
		t.Fatal(err)
	}
	retry, err := r.issue(request, now.Add(time.Second))
	if err != nil || grant != retry {
		t.Fatal("unused retry changed authorization")
	}
	session, err := r.redeem(grant.Token, now.Add(2*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	session.connected.Store(true)
	if _, err = r.issue(request, now.Add(3*time.Second)); err == nil {
		t.Fatal("consumed authorization was reissued")
	}
	r.mutation.Lock()
	r.reap(now.Add(6 * time.Minute))
	r.mutation.Unlock()
	if r.lookup(grant.Member) != session || session.closed.Load() || len(r.issued) != 0 {
		t.Fatal("history cleanup revoked live session")
	}
}

func TestRetryDirectoryHasBoundAndRecoversAfterExpiry(t *testing.T) {
	r := newRegistry([]byte(strings.Repeat("k", 32)), 1)
	now := time.Now()
	for i := 0; i < 1024; i++ {
		if _, err := r.issue(sessionRequest{Request: randomHex(16), Account: 1, Login: strings.Repeat("a", 32), MTU: 512}, now); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := r.issue(sessionRequest{Request: randomHex(16), Account: 1, Login: strings.Repeat("a", 32), MTU: 512}, now); err == nil {
		t.Fatal("retry directory cap not enforced")
	}
	if len(r.issued) != 1024 {
		t.Fatal("retry directory exceeded cap")
	}
	if _, err := r.issue(sessionRequest{Request: randomHex(16), Account: 2, Login: strings.Repeat("b", 32), MTU: 512}, now.Add(6*time.Minute)); err != nil {
		t.Fatal("expired directory did not recover", err)
	}
}

func TestClosedRedeemedSessionReleasesDirectory(t *testing.T) {
	r := newRegistry([]byte(strings.Repeat("k", 32)), 1)
	now := time.Now()
	g, err := r.issue(sessionRequest{Request: randomHex(16), Account: 1, Login: strings.Repeat("a", 32), MTU: 512}, now)
	if err != nil {
		t.Fatal(err)
	}
	s, err := r.redeem(g.Token, now)
	if err != nil {
		t.Fatal(err)
	}
	s.closed.Store(true)
	if _, err = r.issue(sessionRequest{Request: randomHex(16), Account: 2, Login: strings.Repeat("b", 32), MTU: 512}, now); err != nil {
		t.Fatal(err)
	}
	if r.lookup(g.Member) != nil || len(r.byID) != 1 {
		t.Fatal("closed redeemed session retained")
	}
}
