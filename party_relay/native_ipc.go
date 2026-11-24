package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net"
)

const ipcHeader = 24

type gameIdentity struct {
	PID     uint32 `json:"game_pid"`
	Created uint64 `json:"game_created"`
}
type localRoute struct {
	Member uint32 `json:"member_id"`
	Actor  uint32 `json:"character_id"`
	Key    uint32 `json:"native_key"`
	IP     string `json:"virtual_ip"`
	Port   uint16 `json:"virtual_port"`
	Local  uint16 `json:"local_port"`
	Scope  uint64 `json:"scope"`
	Epoch  uint64 `json:"epoch"`
}
type localView struct {
	Game     gameIdentity
	Context  uint32
	Version  uint64
	Until    uint64
	Required bool
	Routes   []localRoute
}

func ipcFrame(kind uint16, sequence uint64, payload []byte) ([]byte, error) {
	if len(payload) > 4096 || sequence == 0 || (kind != 1 && kind != 2) {
		return nil, errors.New("invalid native IPC frame")
	}
	b := make([]byte, ipcHeader+len(payload))
	copy(b, "RIPC")
	binary.LittleEndian.PutUint16(b[4:], 1)
	binary.LittleEndian.PutUint16(b[6:], kind)
	binary.LittleEndian.PutUint32(b[8:], uint32(len(payload)))
	binary.LittleEndian.PutUint64(b[16:], sequence)
	copy(b[24:], payload)
	return b, nil
}
func ipcSize(header []byte, kind uint16, sequence uint64) (int, error) {
	if len(header) != ipcHeader || string(header[:4]) != "RIPC" || binary.LittleEndian.Uint16(header[4:]) != 1 || binary.LittleEndian.Uint16(header[6:]) != kind || binary.LittleEndian.Uint32(header[12:]) != 0 || binary.LittleEndian.Uint64(header[16:]) != sequence || sequence == 0 {
		return 0, errors.New("native IPC identity/sequence")
	}
	n := binary.LittleEndian.Uint32(header[8:])
	if n > 4096 {
		return 0, errors.New("native IPC length")
	}
	return int(n), nil
}
func parseNativeSnapshot(b []byte, game gameIdentity) (nativeReport, error) {
	if len(b) < 40 || binary.LittleEndian.Uint32(b) != game.PID || binary.LittleEndian.Uint64(b[8:]) != game.Created || binary.LittleEndian.Uint32(b[36:]) != 0 {
		return nativeReport{}, errors.New("native IPC process binding")
	}
	count := int(binary.LittleEndian.Uint16(b[34:]))
	if count > 10 || len(b) != 40+count*16 {
		return nativeReport{}, errors.New("native IPC peer bounds")
	}
	n := nativeReport{Actor: binary.LittleEndian.Uint32(b[4:]), Socket: binary.LittleEndian.Uint32(b[16:]), Context: binary.LittleEndian.Uint32(b[20:]), Installed: binary.LittleEndian.Uint64(b[24:]), Port: binary.LittleEndian.Uint16(b[32:]), Peers: []nativePeer{}}
	seen, slots := map[uint32]bool{}, map[uint32]bool{}
	for i := 0; i < count; i++ {
		p := b[40+i*16:]
		proof := binary.LittleEndian.Uint32(p[12:])
		v := nativePeer{Slot: binary.LittleEndian.Uint32(p), Key: binary.LittleEndian.Uint32(p[4:]), State: binary.LittleEndian.Uint32(p[8:]), Confirmed: proof == 1}
		if v.Slot >= 10 || v.Key == 0 || v.State > 3 || proof > 1 || (v.Confirmed && v.State != 0) || seen[v.Key] || slots[v.Slot] {
			return nativeReport{}, errors.New("native IPC peer identity")
		}
		seen[v.Key] = true
		slots[v.Slot] = true
		n.Peers = append(n.Peers, v)
	}
	if n.Actor >= 65535 {
		return nativeReport{}, errors.New("native IPC actor range")
	}
	return n, nil
}
func encodeLocalView(v localView) ([]byte, error) {
	if v.Game.PID == 0 || v.Game.Created == 0 || len(v.Routes) > 3 || (len(v.Routes) != 0 && (v.Context == 0 || v.Version == 0 || v.Until == 0)) {
		return nil, errors.New("native IPC route bounds")
	}
	b := make([]byte, 40+40*len(v.Routes))
	binary.LittleEndian.PutUint32(b, v.Game.PID)
	binary.LittleEndian.PutUint32(b[4:], v.Context)
	binary.LittleEndian.PutUint64(b[8:], v.Game.Created)
	binary.LittleEndian.PutUint64(b[16:], v.Version)
	binary.LittleEndian.PutUint64(b[24:], v.Until)
	if v.Required {
		binary.LittleEndian.PutUint32(b[32:], 1)
	}
	binary.LittleEndian.PutUint32(b[36:], uint32(len(v.Routes)))
	seen := map[uint32]bool{}
	actors, keys, endpoints := map[uint32]bool{}, map[uint32]bool{}, map[string]bool{}
	ports := map[uint16]bool{}
	for i, r := range v.Routes {
		ip := net.ParseIP(r.IP).To4()
		endpoint := net.JoinHostPort(r.IP, fmt.Sprint(r.Port))
		if r.Member == 0 || r.Actor == 0 || r.Actor >= 65535 || r.Key == 0 || ip == nil || ip[0] != 198 || (ip[1] != 18 && ip[1] != 19) || r.Port == 0 || r.Local == 0 || r.Scope == 0 || r.Epoch == 0 || seen[r.Member] || ports[r.Local] || actors[r.Actor] || keys[r.Key] || endpoints[endpoint] {
			return nil, errors.New("native IPC route identity")
		}
		seen[r.Member] = true
		ports[r.Local] = true
		actors[r.Actor], keys[r.Key], endpoints[endpoint] = true, true, true
		p := b[40+i*40:]
		binary.LittleEndian.PutUint32(p, r.Member)
		binary.LittleEndian.PutUint32(p[4:], r.Actor)
		binary.LittleEndian.PutUint32(p[8:], r.Key)
		copy(p[12:], ip)
		binary.LittleEndian.PutUint16(p[16:], r.Port)
		binary.LittleEndian.PutUint16(p[18:], r.Local)
		binary.LittleEndian.PutUint64(p[20:], r.Scope)
		binary.LittleEndian.PutUint64(p[28:], r.Epoch)
	}
	return b, nil
}
