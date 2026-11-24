package downloader

import (
	"net/http"
	"net/url"
	"testing"
)

func TestWindowsProxyServerAndBypass(t *testing.T) {
	for _, tc := range []struct{ server, bypass, address, want string }{
		{"127.0.0.1:18081", "", "https://example.test/client.zip", "http://127.0.0.1:18081"},
		{"http=127.0.0.1:18080;https=127.0.0.1:18081", "", "https://example.test/client.zip", "http://127.0.0.1:18081"},
		{"socks=127.0.0.1:1080", "", "https://example.test/client.zip", "socks5://127.0.0.1:1080"},
		{"127.0.0.1:18081", "<local>;*.internal.test", "https://files.internal.test/client.zip", ""},
		{"127.0.0.1:18081", "<local>", "https://files/client.zip", ""},
		{"127.0.0.1:18081", "", "https://127.0.0.1/client.zip", ""},
	} {
		req, err := http.NewRequest(http.MethodGet, tc.address, nil)
		if err != nil {
			t.Fatal(err)
		}
		proxy, err := proxyFromSettings(req, tc.server, tc.bypass)
		if err != nil {
			t.Fatal(err)
		}
		got := ""
		if proxy != nil {
			got = proxy.String()
		}
		if got != tc.want {
			t.Fatalf("%s: got %q, want %q", tc.address, got, tc.want)
		}
	}
}

func TestEnvironmentProxyBypass(t *testing.T) {
	for _, address := range []string{"https://example.test/file", "https://a.example.test/file", "https://192.0.2.7/file"} {
		u, _ := url.Parse(address)
		if !matchesNoProxy(u, "example.test,192.0.2.0/24") {
			t.Fatal("environment bypass ignored", address)
		}
	}
	u, _ := url.Parse("https://elsewhere.test/file")
	if matchesNoProxy(u, "example.test,192.0.2.0/24") {
		t.Fatal("bypassed unrelated public host")
	}
}
