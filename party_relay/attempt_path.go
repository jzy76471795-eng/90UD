package main

// A native attempt fixes each authenticated edge leg independently. Capability
// probes remain available on both transports, but cannot switch a live attempt.
import (
	"errors"
	"sync"
	"time"
)

var errAttemptPath = errors.New("fixed attempt transport unavailable")

type pathView struct {
	Scope    uint64 `json:"scope"`
	Epoch    uint64 `json:"epoch"`
	Mode     string `json:"mode"`
	Revision uint64 `json:"revision"`
	Failed   bool   `json:"failed"`
}
type attemptPath struct {
	mu       sync.Mutex
	view     pathView
	endpoint string
}

func (s *relaySession) transport(scope, epoch uint64, now time.Time) pathView {
	p := &s.path
	p.mu.Lock()
	defer p.mu.Unlock()
	if scope == 0 || epoch == 0 {
		return pathView{}
	}
	endpoint := ""
	if s.udp != nil {
		if addr := s.udp.destination(now, false); addr != nil {
			endpoint = addr.String()
		}
	}
	if p.view.Scope != scope || p.view.Epoch != epoch {
		mode := "wss"
		if endpoint != "" {
			mode = "udp"
		}
		p.view = pathView{Scope: scope, Epoch: epoch, Mode: mode, Revision: p.view.Revision + 1}
		p.endpoint = endpoint
	} else if p.view.Mode == "udp" && endpoint != p.endpoint && !p.view.Failed {
		p.view.Failed = true
		p.view.Revision++
	}
	return p.view
}
func (s *relaySession) failTransport(v pathView) bool {
	p := &s.path
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.view.Scope != v.Scope || p.view.Epoch != v.Epoch || p.view.Revision != v.Revision {
		return false
	}
	if !p.view.Failed {
		p.view.Failed = true
		p.view.Revision++
	}
	return true
}
func (s *relaySession) transportPermits(h Header, udp bool) bool {
	p := s.transport(h.Scope, h.Epoch, time.Now())
	return !p.Failed && (p.Mode == "udp") == udp
}

// Caller holds e.mu. A local failure is sticky until a fresh scope/epoch, even
// if a delayed HTTP view for the old attempt arrives afterwards.
func (e *edgeClient) failTransportLocked() {
	e.pathFailed = true
}
