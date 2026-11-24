package main

import (
	"bytes"
	"compress/zlib"
	"encoding/binary"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const fixtureKeyset = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f"

func fixtureCrypt(data []byte, seed, inc uint32) []byte {
	out := append([]byte(nil), data...)
	for offset := 0; offset < len(out); offset += 4 {
		a := uint32(uint64(seed)*214013 + uint64(inc))
		seed = uint32(uint64(a)*214013 + uint64(inc))
		key := (a & 0xffff0000) | (seed >> 16)
		for j := 0; j < 4 && offset+j < len(out); j++ {
			out[offset+j] ^= byte(key >> uint(j*8))
		}
	}
	return out
}
func fixtureZip(data []byte) []byte {
	var b bytes.Buffer
	w := zlib.NewWriter(&b)
	_, _ = w.Write(data)
	_ = w.Close()
	return b.Bytes()
}
func fixtureWords(words ...uint32) []byte {
	b := make([]byte, 4*len(words))
	for i, w := range words {
		binary.LittleEndian.PutUint32(b[i*4:], w)
	}
	return b
}
func fixtureNarrow(s string) uint32 {
	a := []byte(s)
	return uint32(uint64(a[0])*1988257537 + 449*(uint64(a[3])+449*(uint64(a[2])+449*uint64(a[1]))))
}
func fixtureSeeds(profile string) map[string]uint32 {
	if profile == "utility-guarded" {
		return map[string]uint32{"head": 0x76c5d612, "hash": fixtureNarrow("HASH"), "grpi": fixtureNarrow("GRPI"), "body": fixtureNarrow("BodY"), "stra": fixtureNarrow("sTrA"), "strb": fixtureNarrow("sTrW")}
	}
	keys, _ := standardOldKeys()
	if profile == "keyset96" {
		keys, _ = parseKeySet(fixtureKeyset)
	}
	seeds := map[string]uint32{}
	for name, key := range keys {
		seeds[name] = seedFromKey(key)
	}
	return seeds
}
func fixturePVF(profile string) []byte {
	seeds := fixtureSeeds(profile)
	a, b := []byte("file.bin\x00dir\x00"), []byte{0, 0}
	az, bz := fixtureZip(a), fixtureZip(b)
	first, second := fixtureZip(bytes.Repeat([]byte("body-payload-1"), 10)), fixtureZip([]byte("another group not referenced by tree"))
	tree := fixtureWords(0, 18, 0, 0, 140, 1)
	hash := fixtureWords(1, 0, 18, 2, 0, 18)
	names := make([]byte, 8)
	names = append(names, fixtureWords(uint32(len(az))^xorA, uint32(len(az))^uint32(len(a)))...)
	names = append(names, fixtureCrypt(az, seeds["stra"], lcgStr)...)
	names = append(names, fixtureWords(uint32(len(bz))^xorB, uint32(len(bz))^uint32(len(b)))...)
	names = append(names, fixtureCrypt(bz, seeds["strb"], lcgStr)...)
	pairs := fixtureWords(uint32(len(first)), 140, uint32(len(first)+len(second)), 36)
	fields := fixtureWords(pvfMagic, 0, 0, 0, 0, 0, 1, 0x31415926, uint32(len(first)+len(second)), 2, uint32(len(hash)), uint32(len(names)))
	out := fixtureCrypt(fields, seeds["head"], lcgMain)
	if profile == "utility-guarded" {
		for i := 24; i < 28; i++ {
			out[i] ^= 0x55
		}
	}
	out = append(out, tree...)
	out = append(out, fixtureCrypt(hash, seeds["hash"], lcgMain)...)
	out = append(out, names...)
	out = append(out, fixtureCrypt(pairs, seeds["grpi"], lcgMain)...)
	out = append(out, fixtureCrypt(first, seeds["body"], lcgMain)...)
	out = append(out, fixtureCrypt(second, seeds["body"], lcgMain)...)
	return out
}

func TestAutoDetectCurrentUtilityPVF(t *testing.T) {
	dir := t.TempDir()
	src, dst := filepath.Join(dir, "source.pvf"), filepath.Join(dir, "result.pvf")
	if err := os.WriteFile(src, fixturePVF("utility-guarded"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := protectPVFFile(src, dst, "", fixtureKeyset); err != nil {
		t.Fatalf("current utility PVF was not recognized: %v", err)
	}
}
func TestCorruptCompressedGroupMustNotProduceOutput(t *testing.T) {
	dir := t.TempDir()
	src, dst := filepath.Join(dir, "source.pvf"), filepath.Join(dir, "result.pvf")
	data := fixturePVF("standard-us-zimo")
	data[len(data)-1] ^= 0x80
	_ = os.WriteFile(src, data, 0600)
	if _, _, err := protectPVFFile(src, dst, "", fixtureKeyset); err == nil {
		t.Fatal("a damaged zlib checksum was accepted")
	}
	if _, err := os.Stat(dst); !os.IsNotExist(err) {
		t.Fatal("damaged source produced an output")
	}
}
func TestRefusesExistingOutputAndSourcePath(t *testing.T) {
	dir := t.TempDir()
	src, dst := filepath.Join(dir, "source.pvf"), filepath.Join(dir, "result.pvf")
	data := fixturePVF("standard-us-zimo")
	_ = os.WriteFile(src, data, 0600)
	_ = os.WriteFile(dst, []byte("must survive"), 0600)
	if _, _, err := protectPVFFile(src, dst, "", fixtureKeyset); err == nil {
		t.Fatal("existing output was overwritten")
	}
	actual, _ := os.ReadFile(dst)
	if string(actual) != "must survive" {
		t.Fatal("existing output changed")
	}
	if _, _, err := protectPVFFile(src, src, "", fixtureKeyset); err == nil {
		t.Fatal("source path was accepted as output")
	}
	actual, _ = os.ReadFile(src)
	if !bytes.Equal(actual, data) {
		t.Fatal("source bytes changed")
	}
}
func TestKeysetStrictValidation(t *testing.T) {
	for _, key := range []string{"", strings.Repeat("g", 96), strings.Repeat("a", 95), " " + fixtureKeyset} {
		if _, err := parseKeySet(key); err == nil {
			t.Fatal("invalid keyset accepted")
		}
	}
}
