// egress-tun-engine is an unprivileged, per-run packet dataplane. It reads
// the selected SOCKS capability from an inherited pipe and a TUN fd passed
// with SCM_RIGHTS. Workload argv/env/cwd/stdio are never part of its inputs.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strconv"
	"syscall"

	"github.com/xjasonlyu/tun2socks/v2/engine"
	_ "github.com/xjasonlyu/tun2socks/v2/dns"
)

type config struct {
	Proxy string `json:"proxy"`
}

func main() {
	deviceFD := flag.Int("device-fd", -1, "inherited TUN file descriptor")
	configFD := flag.Int("config-fd", -1, "inherited private config pipe")
	readyFD := flag.Int("ready-fd", -1, "inherited readiness pipe")
	flag.Parse()
	if *deviceFD < 0 || *configFD < 0 || *readyFD < 0 {
		os.Exit(2)
	}
	var cfg config
	if err := json.NewDecoder(os.NewFile(uintptr(*configFD), "tun-config")).Decode(&cfg); err != nil {
		fmt.Fprintln(os.Stderr, "invalid private TUN config")
		os.Exit(2)
	}
	if cfg.Proxy == "" {
		os.Exit(2)
	}
	key := &engine.Key{Device: "fd://" + strconv.Itoa(*deviceFD), Proxy: cfg.Proxy,
		MTU: 1500, LogLevel: "silent"}
	engine.Insert(key)
	engine.Start()
	ready := os.NewFile(uintptr(*readyFD), "ready")
	_, _ = ready.Write([]byte("R"))
	_ = ready.Close()
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, syscall.SIGINT, syscall.SIGTERM)
	<-ch
	engine.Stop()
}
