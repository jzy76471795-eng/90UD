package manager

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLauncherResourceUpdateRequiresActiveDigestAndTLS(t *testing.T) {
	l, _, _ := launcherFixture(t)
	l.manager.config.ServerRoot = t.TempDir()
	pin := strings.Repeat("a", 64)
	for _, sql := range []string{
		"DELETE FROM gmkit_pvf_current",
		"INSERT INTO gmkit_pvf_releases (pvf_version,file_sha256,key_set,download_url,client_download_url,enabled,created_at,updated_at) VALUES ('resource-test','" + pin + "','','','',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
		"INSERT INTO gmkit_pvf_current(slot,release_id) SELECT 1,id FROM gmkit_pvf_releases WHERE pvf_version='resource-test'",
	} {
		if _, err := l.manager.store.db.Exec(sql); err != nil {
			t.Fatal(err)
		}
	}
	folder := filepath.Join(l.manager.config.ServerRoot, "server_proto", "resources", "launcher")
	if err := os.MkdirAll(folder, 0700); err != nil {
		t.Fatal(err)
	}
	raw := "PK\x03\x04fixture"
	if err := os.WriteFile(filepath.Join(folder, pin+".zip"), []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	request := func(scheme, method, name, byteRange string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, scheme+"://127.0.0.1"+launcherResourcePrefix+name, nil)
		r.Header.Set("Range", byteRange)
		w := httptest.NewRecorder()
		l.ServeHTTP(w, r)
		return w
	}
	if w := request("https", "GET", pin+".zip", ""); w.Code != 200 || w.Body.String() != raw {
		t.Fatalf("download status %d", w.Code)
	}
	if w := request("https", "GET", "current.zip", ""); w.Code != 200 || w.Body.String() != raw {
		t.Fatalf("current download status %d", w.Code)
	}
	if w := request("https", "GET", pin+".zip", "bytes=0-3"); w.Code != 206 || w.Body.String() != raw[:4] {
		t.Fatalf("range status %d", w.Code)
	}
	if w := request("https", "HEAD", pin+".zip", ""); w.Code != 200 || w.Body.Len() != 0 {
		t.Fatalf("head status %d", w.Code)
	}
	if w := request("http", "GET", pin+".zip", ""); w.Code != http.StatusUpgradeRequired {
		t.Fatalf("insecure request status %d", w.Code)
	}
	if w := request("https", "GET", strings.Repeat("b", 64)+".zip", ""); w.Code != 404 {
		t.Fatalf("unpublished request status %d", w.Code)
	}
	for i := 0; i < cap(l.downloads); i++ {
		l.downloads <- struct{}{}
	}
	if w := request("https", "GET", pin+".zip", ""); w.Code != 503 {
		t.Fatalf("overloaded request status %d", w.Code)
	}
}
