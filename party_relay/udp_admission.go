package main

import (
	"errors"
	"sync"
	"time"
)

// Independent anonymous and authenticated budgets keep arbitrary public UDP
// sources from consuming the established clients' cryptographic work budget.
type udpIngressLimits struct {
	UnknownGlobal int `json:"unauth_global_pps"`
	UnknownSource int `json:"unauth_source_pps"`
	BoundGlobal   int `json:"authenticated_global_pps"`
	BoundSession  int `json:"authenticated_session_pps"`
	Sources       int `json:"max_unauth_sources"`
}

func (l udpIngressLimits) normalized() (udpIngressLimits, error) {
	defaults := []int{8192, 128, 32000, 2048, 1024}
	fields := []*int{&l.UnknownGlobal, &l.UnknownSource, &l.BoundGlobal, &l.BoundSession, &l.Sources}
	for i, p := range fields {
		if *p == 0 {
			*p = defaults[i]
		}
		if *p < 1 || *p > 1_000_000 {
			return l, errors.New("invalid UDP ingress budget")
		}
	}
	if l.Sources > 16384 {
		return l, errors.New("UDP source directory exceeds supported budget")
	}
	return l, nil
}

type udpIngress struct {
	mu             sync.Mutex
	limits         udpIngressLimits
	unknown, known requestBucket
	sources        map[string]*requestBucket
	swept          time.Time
}

func newUDPIngress(l udpIngressLimits) (*udpIngress, error) {
	l, err := l.normalized()
	if err != nil {
		return nil, err
	}
	return &udpIngress{limits: l, sources: map[string]*requestBucket{}}, nil
}
func (a *udpIngress) anonymous(ip string, now time.Time) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.swept.IsZero() || now.Sub(a.swept) >= time.Second {
		for key, bucket := range a.sources {
			if now.Sub(bucket.at) > time.Minute {
				delete(a.sources, key)
			}
		}
		a.swept = now
	}
	bucket := a.sources[ip]
	if bucket == nil {
		if len(a.sources) >= a.limits.Sources {
			return false
		}
		bucket = &requestBucket{}
		a.sources[ip] = bucket
	}
	if !bucket.take(now, float64(a.limits.UnknownSource), float64(a.limits.UnknownSource*2)) {
		return false
	}
	return a.unknown.take(now, float64(a.limits.UnknownGlobal), float64(max(1, a.limits.UnknownGlobal/4)))
}
func (a *udpIngress) authenticated(s *relaySession, now time.Time) bool {
	s.udp.mu.Lock()
	allowed := s.udp.ingress.take(now, float64(a.limits.BoundSession), float64(max(1, a.limits.BoundSession/2)))
	s.udp.mu.Unlock()
	if !allowed {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.known.take(now, float64(a.limits.BoundGlobal), float64(max(1, a.limits.BoundGlobal/4)))
}
