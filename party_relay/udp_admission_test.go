package main

import (
	"testing"
	"time"
)

func TestUDPAdmissionSeparatesAnonymousAndEstablishedBudgets(t *testing.T) {
	a, err := newUDPIngress(udpIngressLimits{UnknownGlobal: 8, UnknownSource: 1, BoundGlobal: 8, BoundSession: 4, Sources: 2})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	if !a.anonymous("source-one", now) || !a.anonymous("source-one", now) {
		t.Fatal("anonymous burst")
	}
	for i := 0; i < 10000; i++ {
		if a.anonymous("source-one", now) {
			t.Fatal("source cap bypassed")
		}
	}
	if a.anonymous("source-two", now) {
		t.Fatal("anonymous global cap bypassed")
	}
	live := &relaySession{udp: &sessionUDP{}}
	if !a.authenticated(live, now) {
		t.Fatal("anonymous flood consumed established budget")
	}
	if !a.authenticated(live, now) || a.authenticated(live, now) {
		t.Fatal("established per-session budget")
	}
	if !a.anonymous("source-one", now.Add(time.Second)) {
		t.Fatal("budget did not refill")
	}
}

func TestUDPUnknownSourceDirectoryIsBoundedAndExpires(t *testing.T) {
	a, err := newUDPIngress(udpIngressLimits{UnknownGlobal: 100, UnknownSource: 10, Sources: 2})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	a.anonymous("one", now)
	a.anonymous("two", now)
	if a.anonymous("three", now) || len(a.sources) != 2 {
		t.Fatal("source directory exceeded budget")
	}
	if !a.anonymous("three", now.Add(61*time.Second)) || len(a.sources) != 1 {
		t.Fatal("source directory did not expire")
	}
	for _, limits := range []udpIngressLimits{{UnknownSource: -1}, {Sources: 16385}, {BoundGlobal: 1_000_001}} {
		if _, err := newUDPIngress(limits); err == nil {
			t.Fatal("invalid budget accepted")
		}
	}
}
