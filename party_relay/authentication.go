package main

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

type requestBucket struct {
	tokens float64
	at     time.Time
}

func (b *requestBucket) take(now time.Time, rate, burst float64) bool {
	if b.at.IsZero() {
		b.at = now
		b.tokens = burst
	}
	b.tokens = min(burst, b.tokens+now.Sub(b.at).Seconds()*rate)
	b.at = now
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

type admission struct {
	mu      sync.Mutex
	global  requestBucket
	sources map[string]*requestBucket
}

func (a *admission) accept(remote string, now time.Time) bool {
	ip, _, e := net.SplitHostPort(remote)
	if e != nil {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.global.take(now, 8192, 2048) {
		return false
	}
	if a.sources == nil {
		a.sources = map[string]*requestBucket{}
	}
	b := a.sources[ip]
	if b == nil {
		for k, v := range a.sources {
			if now.Sub(v.at) > time.Minute {
				delete(a.sources, k)
			}
		}
		if len(a.sources) >= 1024 {
			return false
		}
		b = &requestBucket{}
		a.sources[ip] = b
	}
	return b.take(now, 128, 256)
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, e := rand.Read(b); e != nil {
		panic(e)
	}
	return hex.EncodeToString(b)
}

func requestMAC(key []byte, domain, method, path, issued, nonce string, body []byte) string {
	sum := sha256.Sum256(body)
	text := strings.Join([]string{domain, method, path, issued, nonce, hex.EncodeToString(sum[:])}, "\n")
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(text))
	return hex.EncodeToString(mac.Sum(nil))
}

type requestGuard struct {
	mu                 sync.Mutex
	used               map[string]int64
	limit              int
	past, future, keep int64
}

func newRequestGuard(limit int) *requestGuard {
	return &requestGuard{used: make(map[string]int64), limit: limit, past: 1000, future: 250, keep: 2000}
}
func newEdgeGuard() *requestGuard {
	g := newRequestGuard(128)
	g.past = 5000
	g.future = 1000
	g.keep = 6000
	return g
}
func (g *requestGuard) verify(r *http.Request, body, key []byte, domain string, now time.Time) error {
	if len(key) != 32 || r.URL.RawQuery != "" {
		return errors.New("authentication required")
	}
	issued, nonce, sig := r.Header.Get("X-Relay-Issued"), r.Header.Get("X-Relay-Nonce"), r.Header.Get("X-Relay-MAC")
	at, e := strconv.ParseInt(issued, 10, 64)
	if e != nil || at > now.UnixMilli()+g.future || at < now.UnixMilli()-g.past {
		return errors.New("expired authentication")
	}
	raw, e := hex.DecodeString(nonce)
	if e != nil || len(raw) != 16 || hex.EncodeToString(raw) != nonce {
		return errors.New("invalid nonce")
	}
	expected := requestMAC(key, domain, r.Method, r.URL.EscapedPath(), issued, nonce, body)
	got, e := hex.DecodeString(sig)
	want, _ := hex.DecodeString(expected)
	if e != nil || !hmac.Equal(got, want) {
		return errors.New("authentication required")
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	for n, t := range g.used {
		if now.UnixMilli()-t > g.keep {
			delete(g.used, n)
		}
	}
	if _, ok := g.used[nonce]; ok {
		return errors.New("replayed request")
	}
	if len(g.used) >= g.limit {
		return errors.New("authentication capacity")
	}
	g.used[nonce] = at
	return nil
}
func signRequest(r *http.Request, body, key []byte, domain string) {
	signRequestAt(r, body, key, domain, time.Now())
}
func signRequestAt(r *http.Request, body, key []byte, domain string, now time.Time) {
	issued := fmt.Sprint(now.UnixMilli())
	nonce := randomHex(16)
	r.Header.Set("X-Relay-Issued", issued)
	r.Header.Set("X-Relay-Nonce", nonce)
	r.Header.Set("X-Relay-MAC", requestMAC(key, domain, r.Method, r.URL.EscapedPath(), issued, nonce, body))
}
