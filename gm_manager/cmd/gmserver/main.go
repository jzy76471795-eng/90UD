package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"time"

	"newadmin/internal/manager"
)

func main() {
	exe, _ := os.Executable()
	defaultPath := filepath.Join(filepath.Dir(exe), "go-manager.json")
	path := flag.String("config", defaultPath, "管理服务配置文件")
	initialize := flag.Bool("init", false, "只生成本机配置并退出")
	flag.Parse()
	if *initialize {
		if err := manager.WriteInitialConfig(*path); err != nil {
			log.Fatal(err)
		}
		fmt.Println("本机配置已生成；令牌仅保存在指定配置文件内。")
		return
	}
	raw, err := os.ReadFile(*path)
	if err != nil {
		log.Fatal("请先使用 -init 生成本机配置")
	}
	var config manager.Config
	if err = json.Unmarshal(raw, &config); err != nil {
		log.Fatal("配置格式错误")
	}
	if err = os.MkdirAll(filepath.Join(config.DataDirectory, "logs"), 0700); err != nil {
		log.Fatal("管理服务日志目录不可用")
	}
	logFile, err := os.OpenFile(filepath.Join(config.DataDirectory, "logs", "manager.log"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		log.Fatal("管理服务日志不可用")
	}
	defer logFile.Close()
	log.SetOutput(logFile)
	if config.Listen == "" {
		config.Listen = "127.0.0.1:17890"
	}
	service, err := manager.New(config)
	if err != nil {
		log.Fatal(err)
	}
	defer service.Close()
	server := &http.Server{Addr: config.Listen, Handler: service.Handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16384}
	servers := []*http.Server{server}
	var launcher *http.Server
	if config.LauncherListen != "" {
		handler, e := service.LauncherHandler()
		if e != nil {
			log.Fatal(e)
		}
		launcher = &http.Server{Addr: config.LauncherListen, Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16384, TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12}}
		servers = append(servers, launcher)
	}
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt)
	defer signal.Stop(stop)
	errors := make(chan error, len(servers))
	if launcher != nil {
		go func() { errors <- launcher.ListenAndServeTLS(config.LauncherCertificate, config.LauncherKey) }()
	}
	go func() {
		if config.TLSCertificate != "" {
			errors <- server.ListenAndServeTLS(config.TLSCertificate, config.TLSKey)
		} else {
			errors <- server.ListenAndServe()
		}
	}()
	log.Printf("Go GM 管理服务启动：%s；游戏连接是否可用由状态接口单独报告", config.Listen)
	select {
	case <-stop:
	case <-service.ShutdownRequested():
	case err = <-errors:
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for _, listener := range servers {
		_ = listener.Shutdown(ctx)
	}
	if err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
