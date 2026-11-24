package main

import (
	"errors"
	"github.com/gorilla/websocket"
	"sync"
	"time"
)

type Packet struct {
	H    Header
	Data []byte
	At   time.Time
	UDP  bool // Session probes must return over the transport they measured.
}

// One complete current measurement for each carrier. The old 16 KiB bound
// could not hold a legitimate WSS+UDP burst (17,536 bytes) before a writer ran.
const controlQueueBytes = 2 * (64 + 512 + 8192)

type Queue struct {
	mu                     sync.Mutex
	items                  []Packet
	bytes                  int
	closed                 bool
	wake                   chan struct{}
	budget                 *Budget
	limitCount, limitBytes int
}

func newQueue() *Queue                { return &Queue{wake: make(chan struct{}, 1)} }
func newBudgetQueue(b *Budget) *Queue { q := newQueue(); q.budget = b; return q }
func newControlQueue(b *Budget) *Queue {
	q := newBudgetQueue(b)
	q.limitCount = 32
	q.limitBytes = controlQueueBytes
	return q
}
func (q *Queue) dropLocked() {
	if q.budget != nil {
		q.budget.bytes.Add(-int64(q.bytes))
	}
	q.items = nil
	q.bytes = 0
}
func (q *Queue) put(p Packet) bool {
	return q.offer(p, false)
}

// Capability probes are replaceable measurements, never native game data.
// A lost measurement cannot grant freshness; a later real echo is required.
func (q *Queue) putProbe(p Packet) bool {
	if p.H.Source == 0 || !isSessionProbe(p.H, p.H.Source) {
		return false
	}
	return q.offer(p, true)
}

func (q *Queue) offer(p Packet, expendable bool) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	count, bytes := q.limitCount, q.limitBytes
	if count == 0 {
		count = 256
	}
	if bytes == 0 {
		bytes = 256 * 1024
	}
	if q.closed || len(q.items) >= count || q.bytes+len(p.Data) > bytes || (!p.At.IsZero() && time.Since(p.At) >= 100*time.Millisecond) || (q.budget != nil && !q.budget.reserve(int64(len(p.Data)))) {
		if expendable {
			return false
		}
		q.closed = true
		q.dropLocked()
		select {
		case q.wake <- struct{}{}:
		default:
		}
		return false
	}
	q.items = append(q.items, p)
	q.bytes += len(p.Data)
	select {
	case q.wake <- struct{}{}:
	default:
	}
	return true
}
func (q *Queue) pop() (Packet, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.closed || len(q.items) == 0 {
		return Packet{}, false
	}
	p := q.items[0]
	q.items[0] = Packet{}
	q.items = q.items[1:]
	q.bytes -= len(p.Data)
	if q.budget != nil {
		q.budget.bytes.Add(-int64(len(p.Data)))
	}
	return p, true
}
func (q *Queue) clear()       { q.mu.Lock(); q.dropLocked(); q.mu.Unlock() }
func (q *Queue) failed() bool { q.mu.Lock(); defer q.mu.Unlock(); return q.closed }
func (q *Queue) fail() {
	q.mu.Lock()
	q.closed = true
	q.dropLocked()
	q.mu.Unlock()
	select {
	case q.wake <- struct{}{}:
	default:
	}
}

type Rate struct {
	tokens float64
	at     time.Time
}

func (r *Rate) allow(n int, now time.Time) bool {
	if r.at.IsZero() {
		r.at = now
		r.tokens = 256 * 1024
	}
	r.tokens = min(256*1024, r.tokens+now.Sub(r.at).Seconds()*256*1024)
	r.at = now
	if float64(n) > r.tokens {
		return false
	}
	r.tokens -= float64(n)
	return true
}

// One reader and one writer own each WSS connection; message limits preserve boundaries.
type WSS struct{ conn *websocket.Conn }

func (w *WSS) read() ([]byte, error) {
	kind, b, e := w.conn.ReadMessage()
	if e != nil {
		return nil, e
	}
	if kind != websocket.BinaryMessage {
		return nil, errors.New("binary relay frame required")
	}
	return b, nil
}
func (w *WSS) write(b []byte) error {
	w.conn.SetWriteDeadline(time.Now().Add(100 * time.Millisecond))
	return w.conn.WriteMessage(websocket.BinaryMessage, b)
}
