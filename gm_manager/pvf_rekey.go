package main

// PVF 密钥轮换重打包（Go 移植版，逐字节对齐 pvf_protect.py 的 rekey）。
// 输入：标准密钥（或上一轮 96hex key_set）加密的 Script.pvf；
// 输出：随机（或指定）新密钥重打包的 Script.pvf，文件尺寸与区段布局保持不变。
// 仅供 PVF 加密页（pvf_tab.go）调用；纯标准库实现。

import (
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
)

const (
	lcgMain  = 2531011
	lcgStr   = 2531017
	pvfMagic = 0x69706B6E // "nkpi" little-endian
	xorA     = 0xAA74472E
	xorB     = 0x9A82F037
)

// 标准密钥 16B 宽 blob（源自 pvf_codec.py / dnf.exe key blobs 逆向）。
var stdWideBlobs = map[string]string{
	"head": "0054512a9c4c349a35f07e4fcd9b0000",
	"hash": "0054512a9c4c109a07f05e4ffff90000",
	"grpi": "0054512a934c039a03f02e4fbd2b0000",
	"body": "0054512a964c3e9a2af05d4f565a0000",
	"stra": "0054512aa74c059a0df0284fb3e50000",
	"strb": "0054512aa74c059a0df03e4fb3e50000",
}

// keyOrder 96hex 分段顺序（与三端 EInfo 契约、服务端 NormalizeKeySetHex 对齐）。
var keyOrder = []string{"head", "hash", "grpi", "body", "stra", "strb"}

// wideToPlain 将 16B 宽加密 blob 解为 8B 明文 key（逆向自 dnf.exe 的宽 blob 编解码）。
func wideToPlain(blob []byte) ([]byte, error) {
	if len(blob) < 4 {
		return nil, errors.New("wide blob too short")
	}
	v7 := uint32(blob[1] & 0xFE)
	encLen := binary.LittleEndian.Uint16(blob[2:4])
	nchars := uint16((encLen ^ uint16(v7|(v7<<7))) & 0xFFFF)
	dl := int(nchars) * 2
	if 4+dl > len(blob) {
		return nil, errors.New("wide blob len mismatch")
	}
	key := (v7 | 0x9A714CA0) & 0xFFFFFFFF
	out := make([]byte, dl)
	nd := dl / 4
	for i := 0; i < nd; i++ {
		c := binary.LittleEndian.Uint32(blob[4+i*4:])
		p := key ^ c
		binary.LittleEndian.PutUint32(out[i*4:], p)
		key = (p + 65599*key) & 0xFFFFFFFF
	}
	for j := 0; j < dl&3; j++ {
		c := uint32(blob[4+nd*4+j])
		p := (key & 0xFF) ^ c
		out[nd*4+j] = byte(p)
		key = (p + 263*key) & 0xFFFFFFFF
	}
	return out, nil
}

// standardOldKeys 返回 6 组标准密钥（8B/组，供 lcgCrypt 种子）。
// 注：宽 blob 解密明文可能带 wide 串尾（>8B），但种子只取前 8B，截断一致。
func standardOldKeys() (map[string][]byte, error) {
	m := map[string][]byte{}
	for k, h := range stdWideBlobs {
		raw, err := hex.DecodeString(h)
		if err != nil {
			return nil, fmt.Errorf("decode std wide blob %s: %w", k, err)
		}
		plain, err := wideToPlain(raw)
		if err != nil {
			return nil, fmt.Errorf("decode std key %s: %w", k, err)
		}
		if len(plain) < 8 {
			return nil, fmt.Errorf("std key %s too short %d", k, len(plain))
		}
		m[k] = plain[:8]
	}
	return m, nil
}

// parseKeySet 将 96 hex key_set 按 head/hash/grpi/body/stra/strb 拆成 6 组 8B。
func parseKeySet(hexSet string) (map[string][]byte, error) {
	if len(hexSet) != 96 {
		return nil, errors.New("key_set must be 96 hex chars")
	}
	raw, err := hex.DecodeString(hexSet)
	if err != nil || len(raw) != 48 {
		return nil, errors.New("key_set must be 96 hex chars")
	}
	m := map[string][]byte{}
	for i, name := range keyOrder {
		m[name] = raw[i*8 : i*8+8]
	}
	return m, nil
}

func seedFromKey(key []byte) uint32 {
	if len(key) < 8 {
		return 0
	}
	w0 := uint64(binary.LittleEndian.Uint16(key[0:]))
	w1 := uint64(binary.LittleEndian.Uint16(key[2:]))
	w2 := uint64(binary.LittleEndian.Uint16(key[4:]))
	w3 := uint64(binary.LittleEndian.Uint16(key[6:]))
	return uint32((866031377*w0 + 915*(w3+915*(w2+915*w1))) & 0xFFFFFFFF)
}

// lcgCrypt LCG XOR 流加解密（对称），与 pvf_protect.py 逐字节一致。
func lcgCrypt(data, key []byte, inc uint32) []byte {
	seed := seedFromKey(key)
	out := make([]byte, len(data))
	copy(out, data)
	n := len(data)
	i := 0
	for ; i+4 <= n; i += 4 {
		v6 := uint32((214013*uint64(seed) + uint64(inc)) & 0xFFFFFFFF)
		seed = uint32((214013*uint64(v6) + uint64(inc)) & 0xFFFFFFFF)
		xork := ((uint64(seed) >> 16) + uint64(v6&0xFFFF0000)) & 0xFFFFFFFF
		d := binary.LittleEndian.Uint32(out[i:]) ^ uint32(xork)
		binary.LittleEndian.PutUint32(out[i:], d)
	}
	rem := n - i
	if rem > 0 {
		v6 := uint32((214013*uint64(seed) + uint64(inc)) & 0xFFFFFFFF)
		v7 := uint32((uint64(v6&0xFFFF0000) + ((214013*uint64(v6)+uint64(inc))&0xFFFFFFFF)>>16) & 0xFFFFFFFF)
		var kb [4]byte
		binary.LittleEndian.PutUint32(kb[:], v7)
		for j := 0; j < rem; j++ {
			out[i+j] ^= kb[j]
		}
	}
	return out
}
