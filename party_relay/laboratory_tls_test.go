package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func laboratoryTLS() (tls.Certificate, *x509.CertPool, error) {
	key, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if e != nil {
		return tls.Certificate{}, nil, e
	}
	serial, e := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if e != nil {
		return tls.Certificate{}, nil, e
	}
	template := &x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: "90US loopback laboratory only"}, NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(24 * time.Hour), KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, IPAddresses: []net.IP{net.IPv4(127, 0, 0, 1)}, DNSNames: []string{"localhost"}}
	der, e := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if e != nil {
		return tls.Certificate{}, nil, e
	}
	pk, e := x509.MarshalPKCS8PrivateKey(key)
	if e != nil {
		return tls.Certificate{}, nil, e
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	cert, e := tls.X509KeyPair(certPEM, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: pk}))
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(certPEM)
	return cert, pool, e
}

func TestExportExplicitLoopbackFixtureTLS(t *testing.T) {
	directory := os.Getenv("PARTY_RELAY_TEST_TLS_DIR")
	if directory == "" {
		t.Skip("explicit multiprocess fixture only")
	}
	absolute, err := filepath.Abs(directory)
	if err != nil || !strings.HasPrefix(strings.ToLower(absolute), `e:\codex\`) {
		t.Fatal("fixture artifacts must stay under E codex")
	}
	cert, _, err := laboratoryTLS()
	if err != nil {
		t.Fatal(err)
	}
	if address := os.Getenv("PARTY_RELAY_TEST_TLS_IP"); address != "" {
		ip := net.ParseIP(address)
		if ip == nil || !ip.IsPrivate() || ip.To4() == nil {
			t.Fatal("explicit private IPv4 fixture only")
		}
		private, e := rsa.GenerateKey(rand.Reader, 2048)
		if e != nil {
			t.Fatal(e)
		}
		template := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: "90US private interface fixture only"}, NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(24 * time.Hour), KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, IPAddresses: []net.IP{net.IPv4(127, 0, 0, 1), net.IPv4(127, 0, 0, 2), ip}}
		der, e := x509.CreateCertificate(rand.Reader, template, template, &private.PublicKey, private)
		if e != nil {
			t.Fatal(e)
		}
		cert = tls.Certificate{Certificate: [][]byte{der}, PrivateKey: private}
	}
	key, err := x509.MarshalPKCS8PrivateKey(cert.PrivateKey)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.MkdirAll(absolute, 0700); err != nil {
		t.Fatal(err)
	}
	for name, block := range map[string]*pem.Block{"lab-cert.pem": {Type: "CERTIFICATE", Bytes: cert.Certificate[0]}, "lab-key.pem": {Type: "PRIVATE KEY", Bytes: key}} {
		if err = os.WriteFile(filepath.Join(absolute, name), pem.EncodeToMemory(block), 0600); err != nil {
			t.Fatal(err)
		}
	}
}
