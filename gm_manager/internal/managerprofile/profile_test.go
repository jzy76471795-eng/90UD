package managerprofile

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func certificate(t *testing.T, root string) string {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "fixture"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "external-certificate.pem")
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestMoveCompleteProfileToDifferentDirectory(t *testing.T) {
	root := t.TempDir()
	first := filepath.Join(root, "computer-a", "管理配置")
	second := filepath.Join(root, "computer-b", "管理配置")
	token := strings.Repeat("fixture-", 8)
	if err := Save(first, Profile{Address: "https://example.invalid:17890", Token: token, Certificate: certificate(t, root)}); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(second, 0700); err != nil {
		t.Fatal(err)
	}
	files, err := os.ReadDir(first)
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range files {
		raw, e := os.ReadFile(filepath.Join(first, f.Name()))
		if e != nil {
			t.Fatal(e)
		}
		if e = os.WriteFile(filepath.Join(second, f.Name()), raw, 0600); e != nil {
			t.Fatal(e)
		}
	}
	if err := os.Rename(filepath.Dir(first), filepath.Join(root, "old-location-retained")); err != nil {
		t.Fatal(err)
	}
	p, err := Load(second)
	if err != nil {
		t.Fatal(err)
	}
	if p.Token != token || !strings.HasPrefix(p.Certificate, second+string(filepath.Separator)) {
		t.Fatal("profile still depends on old account/location")
	}
	if _, err = os.Stat(p.Certificate); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(second, FileName))
	if err != nil {
		t.Fatal(err)
	}
	var stored map[string]any
	if json.Unmarshal(raw, &stored) != nil {
		t.Fatal("invalid JSON")
	}
	if _, exists := stored["protected_token"]; exists {
		t.Fatal("Windows binding remains")
	}
	if filepath.IsAbs(stored["certificate"].(string)) {
		t.Fatal("certificate path is not portable")
	}
}

func TestSuccessfulProfileCanBeReplaced(t *testing.T) {
	dir := t.TempDir()
	p := Profile{Address: "https://example.invalid:17890", Token: strings.Repeat("a", 40)}
	if err := Save(dir, p); err != nil {
		t.Fatal(err)
	}
	p.Token = strings.Repeat("b", 40)
	if err := Save(dir, p); err != nil {
		t.Fatal(err)
	}
	loaded, err := Load(dir)
	if err != nil || loaded.Token != p.Token {
		t.Fatal("updated portable profile was not committed")
	}
}

func TestInvalidCertificateDoesNotReplaceWorkingProfile(t *testing.T) {
	dir := t.TempDir()
	p := Profile{Address: "https://example.invalid:17890", Token: strings.Repeat("a", 40)}
	if err := Save(dir, p); err != nil {
		t.Fatal(err)
	}
	bad := filepath.Join(dir, "bad.pem")
	if err := os.WriteFile(bad, []byte("invalid certificate"), 0600); err != nil {
		t.Fatal(err)
	}
	p.Token = strings.Repeat("b", 40)
	p.Certificate = bad
	if Save(dir, p) == nil {
		t.Fatal("invalid certificate accepted")
	}
	loaded, err := Load(dir)
	if err != nil || loaded.Token != strings.Repeat("a", 40) {
		t.Fatal("working configuration changed")
	}
}

func TestLegacyAccountEncryptedProfileHasActionableError(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, FileName), []byte(`{"address":"https://example.invalid:17890","protected_token":"old-account-cipher"}`), 0600); err != nil {
		t.Fatal(err)
	}
	_, err := Load(dir)
	if !errors.Is(err, ErrLegacyProfile) {
		t.Fatal("old encrypted profile was silently ignored")
	}
}

func TestExecutableRelativeDataDirectory(t *testing.T) {
	exe := filepath.Join(t.TempDir(), "moved-manager", "manager.exe")
	if got := DataDirectory(exe, ""); got != filepath.Join(filepath.Dir(exe), "管理配置") {
		t.Fatal(got)
	}
	if got := DataDirectory(exe, "custom-data"); got != filepath.Join(filepath.Dir(exe), "custom-data") {
		t.Fatal(got)
	}
}

func TestRejectRelativeCertificateEscapingConfigurationDirectory(t *testing.T) {
	dir := t.TempDir()
	raw, _ := json.Marshal(Profile{Address: "https://example.invalid:17890", Token: strings.Repeat("x", 40), Certificate: filepath.Join("..", "outside.pem")})
	if err := os.WriteFile(filepath.Join(dir, FileName), raw, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(dir); err == nil {
		t.Fatal("out-of-directory certificate accepted")
	}
}
