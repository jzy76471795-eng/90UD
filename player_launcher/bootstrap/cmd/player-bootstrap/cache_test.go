//go:build windows

package main

import (
	"bytes"
	"crypto/sha256"
	"dnf90manager/internal/launcherbundle"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

// This fixture preserves the shipped host's preparation algorithm as a baseline.
func legacyPrepare(f *os.File, end int64, root string) (string, error) {
	config, start, err := launcherbundle.Read(f, end, launcherbundle.MaxConfig, launcherbundle.ConfigMagic)
	if err != nil {
		return "", err
	}
	managed, _, err := launcherbundle.Read(f, start, 512<<20, launcherbundle.HostMagic)
	if err != nil {
		return "", err
	}
	hostHash := sha256.Sum256(managed)
	identity := sha256.Sum256(append(hostHash[:], config...))
	dir := filepath.Join(root, "host", hex.EncodeToString(identity[:]))
	if err = os.MkdirAll(dir, 0700); err != nil {
		return "", err
	}
	image := append(managed, config...)
	image = append(image, launcherbundle.Footer(int64(len(config)), sha256.Sum256(config), launcherbundle.ConfigMagic)...)
	path := filepath.Join(dir, "player.exe")
	existing, err := os.ReadFile(path)
	if err == nil && bytes.Equal(existing, image) {
		return path, nil
	}
	temp, err := os.CreateTemp(dir, "player-*.tmp")
	if err != nil {
		return "", err
	}
	defer os.Remove(temp.Name())
	_, err = temp.Write(image)
	if err == nil {
		err = temp.Sync()
	}
	closeErr := temp.Close()
	if err != nil {
		return "", err
	}
	if closeErr != nil {
		return "", closeErr
	}
	if _, err = os.Stat(path); err == nil {
		if err = os.Remove(path); err != nil {
			return "", err
		}
	}
	if err = os.Rename(temp.Name(), path); err != nil {
		return "", err
	}
	return path, nil
}

func fixture(t *testing.T) (*os.File, int64) {
	t.Helper()
	p := filepath.Join(t.TempDir(), "source.exe")
	f, err := os.Create(p)
	if err != nil {
		t.Fatal(err)
	}
	_, err = f.Write([]byte("native host"))
	if err != nil {
		t.Fatal(err)
	}
	if err = launcherbundle.Append(f, bytes.Repeat([]byte{23}, 1024), launcherbundle.HostMagic); err != nil {
		t.Fatal(err)
	}
	if err = launcherbundle.Append(f, []byte(`{"server_id":"test"}`), launcherbundle.ConfigMagic); err != nil {
		t.Fatal(err)
	}
	s, err := f.Stat()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { f.Close() })
	return f, s.Size()
}

func TestCacheCompatibilityAndRepair(t *testing.T) {
	f, end := fixture(t)
	root := t.TempDir()
	old, err := legacyPrepare(f, end, root)
	if err != nil {
		t.Fatal(err)
	}
	want, err := os.ReadFile(old)
	if err != nil {
		t.Fatal(err)
	}
	before, _ := os.Stat(old)
	got, err := prepareCachedImage(f, end, root)
	if err != nil {
		t.Fatal(err)
	}
	after, _ := os.Stat(got)
	if got != old || !before.ModTime().Equal(after.ModTime()) {
		t.Fatal("existing valid cache was not reused")
	}
	bad := append([]byte(nil), want...)
	bad[0] ^= 1
	if err = os.WriteFile(got, bad, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = prepareCachedImage(f, end, root); err != nil {
		t.Fatal(err)
	}
	actual, _ := os.ReadFile(got)
	if !bytes.Equal(actual, want) {
		t.Fatal("corrupted cache was not repaired byte-for-byte")
	}
	entries, _ := filepath.Glob(filepath.Join(filepath.Dir(got), "*.tmp"))
	if len(entries) != 0 {
		t.Fatal("temporary file left behind")
	}
}

func TestSourceDamageIsRejectedEvenWithValidCache(t *testing.T) {
	f, end := fixture(t)
	root := t.TempDir()
	if _, err := prepareCachedImage(f, end, root); err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteAt([]byte{99}, 20); err != nil {
		t.Fatal(err)
	}
	if _, err := prepareCachedImage(f, end, root); err == nil {
		t.Fatal("damaged embedded host was accepted")
	}
}

func TestInvalidFramingIsRejected(t *testing.T) {
	for _, kind := range []string{"truncated", "magic", "length", "config"} {
		t.Run(kind, func(t *testing.T) {
			f, end := fixture(t)
			switch kind {
			case "truncated":
				end = 20
			case "magic":
				f.WriteAt([]byte{0}, end-32)
			case "length":
				f.WriteAt(bytes.Repeat([]byte{255}, 8), end-launcherbundle.FooterSize)
			case "config":
				f.WriteAt([]byte{0}, end-launcherbundle.FooterSize-5)
			}
			if _, err := prepareCachedImage(f, end, t.TempDir()); err == nil {
				t.Fatal("invalid image was accepted")
			}
		})
	}
}

func TestActualFixtureMeasurements(t *testing.T) {
	path := os.Getenv("PY90_BENCH_FIXTURE")
	if path == "" {
		t.Skip("actual launcher not configured")
	}
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		t.Fatal(err)
	}
	type measurement struct {
		Implementation string  `json:"implementation"`
		Phase          string  `json:"phase"`
		Round          int     `json:"round"`
		Milliseconds   float64 `json:"ms"`
		Allocated      uint64  `json:"allocated_bytes"`
	}
	var results []measurement
	for round := 0; round < 3; round++ {
		for _, name := range []string{"legacy", "stream"} {
			prepare := legacyPrepare
			if name == "stream" {
				prepare = prepareCachedImage
			}
			root := t.TempDir()
			for _, phase := range []string{"cold", "warm"} {
				runtime.GC()
				var before, after runtime.MemStats
				runtime.ReadMemStats(&before)
				start := time.Now()
				result, err := prepare(f, info.Size(), root)
				elapsed := time.Since(start)
				runtime.ReadMemStats(&after)
				if err != nil {
					t.Fatal(err)
				}
				if result == "" {
					t.Fatal("empty output path")
				}
				allocated := after.TotalAlloc - before.TotalAlloc
				if name == "stream" && allocated > uint64(info.Size())+8<<20 {
					t.Fatalf("cached-image preparation allocated %d bytes", allocated)
				}
				results = append(results, measurement{name, phase, round, float64(elapsed.Microseconds()) / 1000, allocated})
			}
		}
	}
	encoded, _ := json.MarshalIndent(results, "", "  ")
	if err = os.WriteFile(os.Getenv("PY90_BENCH_OUTPUT"), encoded, 0600); err != nil {
		t.Fatal(err)
	}
}
