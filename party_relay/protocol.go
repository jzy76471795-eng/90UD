// Product RLV2 wire format. Listener exposure is explicit deployment config.
package main

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"sync"
	"sync/atomic"
	"time"
)

const headerSize = 80
const tagSize = 16
const maxNative = 8192 // A bounded opaque native datagram, fragmented at outer MTU.
const maxFragments = 64

var errWire = errors.New("invalid authenticated relay frame")

type Header struct {
	Session, Scope, Epoch, KeyEpoch uint64
	Source, Target                  uint32
	Sequence, Datagram              uint64
	Total, Offset                   uint32
	Index, Count, Length            uint16
	Kind                            uint8
}

func (h Header) bytes() []byte {
	b := make([]byte, headerSize)
	copy(b, "RLV2")
	b[4] = 2
	b[5] = h.Kind
	if b[5] == 0 {
		b[5] = 1
	}
	binary.BigEndian.PutUint16(b[6:], headerSize)
	for i, n := range []uint64{h.Session, h.Scope, h.Epoch, h.KeyEpoch} {
		binary.BigEndian.PutUint64(b[8+8*i:], n)
	}
	binary.BigEndian.PutUint32(b[40:], h.Source)
	binary.BigEndian.PutUint32(b[44:], h.Target)
	binary.BigEndian.PutUint64(b[48:], h.Sequence)
	binary.BigEndian.PutUint64(b[56:], h.Datagram)
	binary.BigEndian.PutUint32(b[64:], h.Total)
	binary.BigEndian.PutUint32(b[68:], h.Offset)
	binary.BigEndian.PutUint16(b[72:], h.Index)
	binary.BigEndian.PutUint16(b[74:], h.Count)
	binary.BigEndian.PutUint16(b[76:], h.Length)
	return b
}
func parseHeader(b []byte) (Header, error) {
	if len(b) < headerSize+tagSize || string(b[:4]) != "RLV2" || b[4] != 2 || (b[5] != 1 && b[5] != 2) || binary.BigEndian.Uint16(b[6:]) != headerSize || binary.BigEndian.Uint16(b[78:]) != 0 {
		return Header{}, errWire
	}
	h := Header{Session: binary.BigEndian.Uint64(b[8:]), Scope: binary.BigEndian.Uint64(b[16:]), Epoch: binary.BigEndian.Uint64(b[24:]), KeyEpoch: binary.BigEndian.Uint64(b[32:]), Source: binary.BigEndian.Uint32(b[40:]), Target: binary.BigEndian.Uint32(b[44:]), Sequence: binary.BigEndian.Uint64(b[48:]), Datagram: binary.BigEndian.Uint64(b[56:]), Total: binary.BigEndian.Uint32(b[64:]), Offset: binary.BigEndian.Uint32(b[68:]), Index: binary.BigEndian.Uint16(b[72:]), Count: binary.BigEndian.Uint16(b[74:]), Length: binary.BigEndian.Uint16(b[76:])}
	if int(h.Length)+headerSize+tagSize != len(b) {
		return h, errWire
	}
	h.Kind = b[5]
	return h, nil
}
func derive(secret []byte, label string) []byte {
	x := hmac.New(sha256.New, []byte("90US-isolated-RLV2-HKDF"))
	x.Write(secret)
	y := hmac.New(sha256.New, x.Sum(nil))
	y.Write([]byte(label))
	y.Write([]byte{1})
	return y.Sum(nil)
}
func aead(secret []byte, label string) cipher.AEAD {
	b, e := aes.NewCipher(derive(secret, label))
	if e != nil {
		panic(e)
	}
	a, e := cipher.NewGCM(b)
	if e != nil {
		panic(e)
	}
	return a
}
func nonce(h Header) []byte {
	b := make([]byte, 12)
	copy(b, []byte("RLV2"))
	binary.BigEndian.PutUint64(b[4:], h.Sequence)
	return b
}

type Window struct {
	high uint64
	seen map[uint64]bool
}

func (w *Window) accept(n uint64) bool {
	if n == 0 || (n <= w.high && w.high-n >= 1024) || w.seen[n] {
		return false
	}
	if w.seen == nil {
		w.seen = make(map[uint64]bool)
	}
	w.seen[n] = true
	if n > w.high {
		w.high = n
	}
	for old := range w.seen {
		if w.high >= old && w.high-old >= 1024 {
			delete(w.seen, old)
		}
	}
	return true
}

type AssemblyKey struct {
	Source, Target         uint32
	Scope, Epoch, Datagram uint64
}
type assembly struct {
	header   Header
	bytes    []byte
	seen     uint64
	received int
	created  time.Time
}
type Budget struct {
	bytes atomic.Int64
	limit int64
}

func (b *Budget) reserve(n int64) bool {
	for {
		v := b.bytes.Load()
		if n > b.limit-v {
			return false
		}
		if b.bytes.CompareAndSwap(v, v+n) {
			return true
		}
	}
}

type Codec struct {
	mu                sync.Mutex
	secret            []byte
	label             string
	session, keyEpoch uint64
	mtu               int
	tx                uint64
	rx                Window
	parts             map[AssemblyKey]*assembly
	memory            int
	global            *Budget
}

func newCodec(secret []byte, label string, session, keyEpoch uint64, mtu int, budget *Budget) *Codec {
	return &Codec{secret: secret, label: label, session: session, keyEpoch: keyEpoch, mtu: mtu, parts: make(map[AssemblyKey]*assembly), global: budget}
}
func (c *Codec) key(h Header) cipher.AEAD {
	return aead(c.secret, fmt.Sprintf("%s/session-%d/key-%d/scope-%d/epoch-%d", c.label, c.session, c.keyEpoch, h.Scope, h.Epoch))
}
func (c *Codec) encode(source, target uint32, scope, epoch uint64, p []byte) ([][]byte, error) {
	return c.encodeKind(source, target, scope, epoch, p, 1)
}
func (c *Codec) encodeKind(source, target uint32, scope, epoch uint64, p []byte, kind uint8) ([][]byte, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	size := c.mtu - headerSize - tagSize
	if size < 1 || len(p) == 0 || len(p) > maxNative || scope == 0 || epoch == 0 {
		return nil, errWire
	}
	count := (len(p) + size - 1) / size
	if count > maxFragments || c.tx > math.MaxUint64-uint64(count) {
		return nil, errWire
	}
	dg := c.tx + 1
	frames := make([][]byte, 0, count)
	for i := 0; i < count; i++ {
		start := i * size
		end := min(start+size, len(p))
		c.tx++
		h := Header{c.session, scope, epoch, c.keyEpoch, source, target, c.tx, dg, uint32(len(p)), uint32(start), uint16(i), uint16(count), uint16(end - start), kind}
		b := h.bytes()
		frames = append(frames, append(b, c.key(h).Seal(nil, nonce(h), p[start:end], b)...))
	}
	return frames, nil
}
func (c *Codec) drop(k AssemblyKey) {
	a := c.parts[k]
	if a != nil {
		c.memory -= len(a.bytes)
		c.global.bytes.Add(-int64(len(a.bytes)))
		delete(c.parts, k)
	}
}
func (c *Codec) prune(now time.Time) {
	for k, a := range c.parts {
		if now.Sub(a.created) >= time.Second {
			c.drop(k)
		}
	}
}
func (c *Codec) clear() {
	c.mu.Lock()
	defer c.mu.Unlock()
	for k := range c.parts {
		c.drop(k)
	}
}

// authorize executes before replay or reassembly state changes. Header auth comes first.
func (c *Codec) decode(b []byte, now time.Time, authorize func(Header) bool) (Header, []byte, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.prune(now)
	h, e := parseHeader(b)
	if e != nil || len(b) > c.mtu || h.Session != c.session || h.KeyEpoch != c.keyEpoch || h.Scope == 0 || h.Epoch == 0 {
		return h, nil, errWire
	}
	p, e := c.key(h).Open(nil, nonce(h), b[headerSize:], b[:headerSize])
	if e != nil {
		return h, nil, errWire
	}
	size := c.mtu - headerSize - tagSize
	if !authorize(h) || h.Sequence == 0 || h.Datagram == 0 || h.Count == 0 || h.Count > maxFragments || h.Index >= h.Count || h.Total == 0 || h.Total > maxNative || int(h.Count) != (int(h.Total)+size-1)/size || int(h.Offset) != int(h.Index)*size || int(h.Length) != min(size, int(h.Total)-int(h.Offset)) || h.Sequence != h.Datagram+uint64(h.Index) || h.Sequence < h.Datagram {
		return h, nil, errWire
	}
	if !c.rx.accept(h.Sequence) {
		return h, nil, errWire
	}
	k := AssemblyKey{h.Source, h.Target, h.Scope, h.Epoch, h.Datagram}
	a := c.parts[k]
	if a == nil {
		if len(c.parts) >= 8 || c.memory+int(h.Total) > 512*1024 || !c.global.reserve(int64(h.Total)) {
			return h, nil, errWire
		}
		a = &assembly{header: h, bytes: make([]byte, h.Total), created: now}
		c.parts[k] = a
		c.memory += int(h.Total)
	}
	if a.header.Total != h.Total || a.header.Count != h.Count {
		c.drop(k)
		return h, nil, errWire
	}
	bit := uint64(1) << h.Index
	if a.seen&bit != 0 {
		c.drop(k)
		return h, nil, errWire
	}
	copy(a.bytes[int(h.Offset):], p)
	a.seen |= bit
	a.received++
	if a.received != int(h.Count) {
		return h, nil, nil
	}
	result := a.bytes
	c.drop(k)
	return h, result, nil
}
