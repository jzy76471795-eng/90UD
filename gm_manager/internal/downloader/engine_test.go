package downloader

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func config(data []byte, url string) Config {
	return Config{Version: 1, Name: "测试", URL: url, FileName: "client.zip", SHA256: fmt.Sprintf("%x", sha256.Sum256(data)), Size: int64(len(data))}
}
func engine(clients ...*http.Client) Engine {
	e := Engine{Attempts: 3, RetryDelay: time.Millisecond}
	if len(clients) > 0 {
		e.Client = clients[0]
	}
	return e
}
func zipData(t *testing.T, names ...string) []byte {
	t.Helper()
	var b bytes.Buffer
	w := zip.NewWriter(&b)
	for _, name := range names {
		f, e := w.Create(name)
		if e != nil {
			t.Fatal(e)
		}
		f.Write([]byte("test file"))
	}
	if e := w.Close(); e != nil {
		t.Fatal(e)
	}
	return b.Bytes()
}
func TestDownloadRetryRangeAndHash(t *testing.T) {
	data := bytes.Repeat([]byte("abcdefghijklmnopqrstuvwxyz"), 1000)
	var count atomic.Int32
	var ranged atomic.Bool
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := count.Add(1)
		w.Header().Set("Content-Length", fmt.Sprint(len(data)))
		if n == 1 {
			w.Write(data[:1234])
			return
		}
		if r.Header.Get("Range") != "bytes=1234-" {
			t.Error("incorrect Range", r.Header.Get("Range"))
			return
		}
		ranged.Store(true)
		w.Header().Set("Content-Range", fmt.Sprintf("bytes 1234-%d/%d", len(data)-1, len(data)))
		w.Header().Set("Content-Length", fmt.Sprint(len(data)-1234))
		w.WriteHeader(206)
		w.Write(data[1234:])
	}))
	defer srv.Close()
	root := t.TempDir()
	r, e := engine(srv.Client()).Run(context.Background(), config(data, srv.URL), root, nil)
	if e != nil {
		t.Fatal(e)
	}
	b, _ := os.ReadFile(r.Archive)
	if !bytes.Equal(b, data) || !ranged.Load() {
		t.Fatal("resume result incorrect")
	}
	// Reuse the verified final file without a new request.
	if _, e = engine(srv.Client()).Run(context.Background(), config(data, srv.URL), root, nil); e != nil || count.Load() != 2 {
		t.Fatal(e, count.Load())
	}
}

func TestRedirectedDownloadRetriesFinalAddress(t *testing.T) {
	for _, expired := range []bool{false, true} {
		t.Run(fmt.Sprint("expired=", expired), func(t *testing.T) {
			data := []byte("client archive contents")
			var entries, files atomic.Int32
			srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/entry" {
					n := entries.Add(1)
					http.Redirect(w, r, fmt.Sprintf("/file?ticket=%d", n), http.StatusFound)
					return
				}
				n := files.Add(1)
				if n == 1 {
					w.Header().Set("Content-Length", fmt.Sprint(len(data)))
					w.Write(data[:5])
					return
				}
				if expired && r.URL.Query().Get("ticket") == "1" {
					w.WriteHeader(http.StatusForbidden)
					return
				}
				if r.Header.Get("Range") != "bytes=5-" {
					t.Error("lost downloaded bytes", r.Header.Get("Range"))
					w.WriteHeader(http.StatusBadRequest)
					return
				}
				w.Header().Set("Content-Range", fmt.Sprintf("bytes 5-%d/%d", len(data)-1, len(data)))
				w.Header().Set("Content-Length", fmt.Sprint(len(data)-5))
				w.WriteHeader(http.StatusPartialContent)
				w.Write(data[5:])
			}))
			defer srv.Close()
			result, err := engine(srv.Client()).Run(context.Background(), config(data, srv.URL+"/entry"), t.TempDir(), nil)
			if err != nil {
				t.Fatal(err)
			}
			raw, err := os.ReadFile(result.Archive)
			if err != nil || !bytes.Equal(raw, data) {
				t.Fatal("wrong completed archive", err)
			}
			wantEntries, wantFiles := int32(1), int32(2)
			if expired {
				wantEntries, wantFiles = 2, 3
			}
			if entries.Load() != wantEntries || files.Load() != wantFiles {
				t.Fatalf("unexpected redirect retries: entry=%d file=%d; want %d/%d", entries.Load(), files.Load(), wantEntries, wantFiles)
			}
		})
	}
}

func TestDownloadWithoutSHA256RetriesExtractsAndReusesArchive(t *testing.T) {
	data := zipData(t, "游戏/登录器.exe")
	var requests atomic.Int32
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := requests.Add(1)
		if n == 1 {
			w.Header().Set("Content-Length", fmt.Sprint(len(data)))
			w.Write(data[:len(data)/2])
			return
		}
		offset := len(data) / 2
		if r.Header.Get("Range") != fmt.Sprintf("bytes=%d-", offset) {
			t.Errorf("unexpected resume range: %s", r.Header.Get("Range"))
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", offset, len(data)-1, len(data)))
		w.Header().Set("Content-Length", fmt.Sprint(len(data)-offset))
		w.WriteHeader(http.StatusPartialContent)
		w.Write(data[offset:])
	}))
	defer srv.Close()
	c := Config{Version: 1, Name: "客户端下载", URL: srv.URL, FileName: "client.zip", Extract: true, Launcher: "游戏/登录器.exe", AutoStart: true}
	root := t.TempDir()
	report := func(p Progress) {
		if strings.Contains(p.Stage, "SHA256") {
			t.Errorf("unexpected client checksum stage: %s", p.Stage)
		}
	}
	for i := 0; i < 2; i++ {
		result, err := engine(srv.Client()).Run(context.Background(), c, root, report)
		if err != nil {
			t.Fatal(err)
		}
		archive, err := os.ReadFile(result.Archive)
		if err != nil || !bytes.Equal(archive, data) {
			t.Fatal("archive was not downloaded completely", err)
		}
		launcher, err := os.ReadFile(result.Launcher)
		if err != nil || string(launcher) != "test file" {
			t.Fatal("launcher was not extracted", err)
		}
	}
	if requests.Load() != 2 {
		t.Fatalf("expected one interrupted request and one resume, got %d", requests.Load())
	}
}
func TestDownloadIgnoresRangeSafely(t *testing.T) {
	data := []byte("complete data")
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write(data) }))
	defer srv.Close()
	c := config(data, srv.URL)
	root := t.TempDir()
	os.WriteFile(filepath.Join(root, ".下载-"+c.SHA256+".part"), []byte("old"), 0600)
	r, e := engine(srv.Client()).Run(context.Background(), c, root, nil)
	if e != nil {
		t.Fatal(e)
	}
	b, _ := os.ReadFile(r.Archive)
	if !bytes.Equal(b, data) {
		t.Fatal("wrong data")
	}
}
func TestHashMismatchNeverPublishes(t *testing.T) {
	data := []byte("good")
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("evil")) }))
	defer srv.Close()
	c := config(data, srv.URL)
	root := t.TempDir()
	_, e := engine(srv.Client()).Run(context.Background(), c, root, nil)
	if e == nil {
		t.Fatal("accepted corrupted file")
	}
	if _, e = os.Stat(filepath.Join(root, c.FileName)); !os.IsNotExist(e) {
		t.Fatal("published corrupted file")
	}
}
func TestExtractAndPathRejection(t *testing.T) {
	for _, tc := range []struct {
		name string
		bad  bool
	}{{"游戏/登录器.exe", false}, {"../escape.exe", true}, {"/absolute.exe", true}, {"C:/escape.exe", true}, {"COM1.exe", true}, {"COM¹.exe", true}, {"file.exe:stream", true}, {"dir./x.exe", true}} {
		t.Run(tc.name, func(t *testing.T) {
			data := zipData(t, tc.name)
			srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write(data) }))
			defer srv.Close()
			c := config(data, srv.URL)
			c.Extract = true
			if !tc.bad {
				c.Launcher = tc.name
			}
			r, e := engine(srv.Client()).Run(context.Background(), c, t.TempDir(), nil)
			if tc.bad {
				if e == nil {
					t.Fatal("unsafe ZIP accepted")
				}
			} else {
				if e != nil {
					t.Fatal(e)
				}
				if _, e = os.Stat(r.Launcher); e != nil {
					t.Fatal(e)
				}
			}
		})
	}
}
func TestCancellationAndNoOverwrite(t *testing.T) {
	root := t.TempDir()
	data := []byte("hello")
	c := config(data, "https://example.test/client.zip")
	os.WriteFile(filepath.Join(root, c.FileName), []byte("original"), 0600)
	if _, e := engine().Run(context.Background(), c, root, nil); e == nil {
		t.Fatal("overwrote existing file")
	}
	b, _ := os.ReadFile(filepath.Join(root, c.FileName))
	if string(b) != "original" {
		t.Fatal("old content changed")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, e := engine().Run(ctx, c, t.TempDir(), nil)
	if !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
}
func TestInvalidContentRange(t *testing.T) {
	data := []byte("hello")
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Range", "bytes 3-7/8")
		w.WriteHeader(206)
		w.Write(data)
	}))
	defer srv.Close()
	_, e := engine(srv.Client()).Run(context.Background(), config(data, srv.URL), t.TempDir(), nil)
	if e == nil || !strings.Contains(e.Error(), "范围") {
		t.Fatal(e)
	}
}
func TestDirectoryLock(t *testing.T) {
	root := t.TempDir()
	unlock, e := lockDirectory(root)
	if e != nil {
		t.Fatal(e)
	}
	defer unlock()
	ch := make(chan error, 1)
	go func() {
		release, e := lockDirectory(root)
		if e == nil {
			release()
		}
		ch <- e
	}()
	if e = <-ch; e == nil {
		t.Fatal("concurrent download accepted")
	}
}
func TestDuplicateZipAndMissingLauncher(t *testing.T) {
	for _, names := range [][]string{{"A.exe", "a.exe"}, {"x.exe"}} {
		data := zipData(t, names...)
		srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write(data) }))
		c := config(data, srv.URL)
		c.Extract = true
		c.Launcher = "missing.exe"
		root := t.TempDir()
		_, e := engine(srv.Client()).Run(context.Background(), c, root, nil)
		srv.Close()
		if e == nil {
			t.Fatal("invalid zip accepted")
		}
		entries, _ := os.ReadDir(root)
		for _, f := range entries {
			if f.IsDir() {
				t.Fatal("left extraction directory")
			}
		}
	}
}
