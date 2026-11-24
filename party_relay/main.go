// Independent relay service. No client edges, test accounts, Frida or fixture files.
package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"time"
)

type serviceConfig struct {
	Listen         string           `json:"listen"`
	Certificate    string           `json:"tls_certificate"`
	PrivateKey     string           `json:"tls_private_key"`
	ControlKeyFile string           `json:"control_key_file"`
	MaxSessions    int              `json:"max_sessions"`
	UDPListen      string           `json:"udp_listen"`
	UDPPublic      string           `json:"udp_public"`
	UDPIngress     udpIngressLimits `json:"udp_ingress"`
}

// A finite diagnostic vocabulary: never print a credential, URL, native frame,
// or arbitrary error text into the launcher's persistent event journal.
func edgeExitCode(err error) string {
	if errors.Is(err, context.Canceled) {
		return "cancelled"
	}
	if errors.Is(err, io.EOF) {
		return "private_channel_eof"
	}
	var neterr net.Error
	if errors.As(err, &neterr) && neterr.Timeout() {
		return "network_timeout"
	}
	if err == nil {
		return "completed"
	}
	known := map[string]string{
		"edge transport closed": "transport_closed", "edge write deadline failed": "write_deadline",
		"probe state bound": "probe_state_bound", "probe queue bound": "probe_queue_bound",
		"probe queue closed":      "probe_queue_closed",
		"native retirement bound": "retirement_bound", "edge upstream congestion": "upstream_congestion",
		"native delivery failed": "native_delivery", "bounded edge upstream failed": "upstream_failed",
		"relay view binding mismatch": "view_binding", "relay view rolled back": "view_rollback",
		"parent IPC exchange replay": "parent_sequence", "parent game exited": "game_exited",
		"parent game process binding": "game_binding", "parent IPC frame size": "parent_frame_size",
		"bounded private startup required": "private_line_bound", "invalid private startup": "private_line_invalid",
		"single-use edge connection failed": "single_use_connection", "edge control unavailable": "control_unavailable",
	}
	if code, ok := known[err.Error()]; ok {
		return code
	}
	return "unclassified"
}

func main() {
	mode := flag.String("mode", "relay", "relay or edge")
	config := flag.String("config", "", "server configuration path")
	ready := flag.String("ready", "", "nonsecret ready report")
	report := flag.String("report", "", "nonsecret exit report")
	stop := flag.String("stop-file", "", "optional supervisor stop marker")
	duration := flag.Duration("duration", 0, "optional bounded runtime")
	flag.Parse()
	if *mode == "edge" {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		signals := make(chan os.Signal, 1)
		signal.Notify(signals, os.Interrupt)
		defer signal.Stop(signals)
		go func() {
			ticker := time.NewTicker(100 * time.Millisecond)
			defer ticker.Stop()
			started := time.Now()
			for {
				select {
				case <-ctx.Done():
					return
				case <-signals:
					cancel()
					return
				case <-ticker.C:
					if *duration > 0 && time.Since(started) >= *duration {
						cancel()
						return
					}
					if *stop != "" {
						if _, err := os.Stat(*stop); err == nil {
							cancel()
							return
						}
					}
				}
			}
		}()
		ended := make(chan error, 1)
		go func() { ended <- runEdgeFromStdin(ctx, os.Stdin, *ready) }()
		select {
		case <-ctx.Done():
		case err := <-ended:
			fmt.Fprintln(os.Stderr, "edge stopped code="+edgeExitCode(err))
		}
		return
	}
	if *mode != "relay" {
		fmt.Fprintln(os.Stderr, "invalid process role")
		os.Exit(2)
	}
	raw, e := os.ReadFile(*config)
	if e != nil {
		panic("configuration unavailable")
	}
	var cfg serviceConfig
	if strictJSON(raw, &cfg) != nil || cfg.Listen == "" || cfg.Certificate == "" || cfg.PrivateKey == "" || cfg.ControlKeyFile == "" {
		panic("invalid service configuration")
	}
	if cfg.MaxSessions == 0 {
		cfg.MaxSessions = 1024
	}
	if cfg.MaxSessions < 2 || cfg.MaxSessions > 4096 {
		panic("session capacity outside supported bounds")
	}
	key, e := os.ReadFile(cfg.ControlKeyFile)
	if e != nil || len(key) != 32 {
		panic("private control key unavailable")
	}
	certificate, e := tls.LoadX509KeyPair(cfg.Certificate, cfg.PrivateKey)
	if e != nil {
		panic("server TLS certificate unavailable")
	}
	listener, e := net.Listen("tcp", cfg.Listen)
	if e != nil {
		panic("service bind failed")
	}
	service := newService(key, cfg.MaxSessions)
	if (cfg.UDPListen == "") != (cfg.UDPPublic == "") {
		panic("paired UDP listen and public endpoint required")
	}
	if cfg.UDPListen != "" {
		addr, err := net.ResolveUDPAddr("udp4", cfg.UDPListen)
		if err != nil {
			panic("invalid UDP bind")
		}
		conn, err := net.ListenUDP("udp4", addr)
		if err != nil {
			panic("UDP service bind failed")
		}
		public := cfg.UDPPublic
		if host, port, err := net.SplitHostPort(public); err == nil && port == "0" {
			public = net.JoinHostPort(host, strconv.Itoa(conn.LocalAddr().(*net.UDPAddr).Port))
		}
		if service.attachUDP(conn, public, cfg.UDPIngress) != nil {
			conn.Close()
			panic("UDP public endpoint unavailable")
		}
	}
	server := &http.Server{Handler: service, ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second, MaxHeaderBytes: 8192, TLSConfig: &tls.Config{Certificates: []tls.Certificate{certificate}, MinVersion: tls.VersionTLS12, NextProtos: []string{"http/1.1"}}}
	errors := make(chan error, 1)
	go func() { errors <- server.Serve(tls.NewListener(listener, server.TLSConfig)) }()
	if *ready != "" {
		b, _ := json.Marshal(map[string]any{"pid": os.Getpid(), "role": "relay", "listen": listener.Addr().String(), "boot": service.registry.boot, "protocol": "RLV2", "separate_edge_required": true})
		if os.WriteFile(*ready, b, 0600) != nil {
			panic("ready report unavailable")
		}
	}
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt)
	defer signal.Stop(signals)
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	started := time.Now()
	running := true
	for running {
		select {
		case <-signals:
			running = false
		case <-errors:
			running = false
		case <-ticker.C:
			if *duration > 0 && time.Since(started) >= *duration {
				running = false
			}
			if *stop != "" {
				if _, e := os.Stat(*stop); e == nil {
					running = false
				}
			}
		}
	}
	service.close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	server.Shutdown(ctx)
	cancel()
	if *report != "" {
		b, _ := json.MarshalIndent(service.status(), "", "  ")
		if os.WriteFile(*report, b, 0600) != nil {
			fmt.Fprintln(os.Stderr, "exit report unavailable")
		}
	}
}
