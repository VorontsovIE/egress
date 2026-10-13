// Userspace WireGuard server for the e2e PoC.
//
// Runs a wireguard-go device on a UDP port with a gVisor IP stack
// (sing-wireguard StackDevice) that forwards accepted TCP connections to
// 127.0.0.1:<target-port>. A Mihomo client configured with a `wireguard`
// proxy pointing at this server therefore tunnels its traffic here, and
// the forwarded connection proves the full WG path works end-to-end
// without a kernel module.
package main

import (
	"context"
	"crypto/ecdh"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"log"
	"net"
	"net/netip"
	"os"
	"os/signal"
	"syscall"

	"github.com/metacubex/sing/common/buf"
	M "github.com/metacubex/sing/common/metadata"
	N "github.com/metacubex/sing/common/network"
	singwg "github.com/metacubex/sing-wireguard"
	"github.com/metacubex/wireguard-go/conn"
	"github.com/metacubex/wireguard-go/device"
)

type forwarder struct {
	target netip.AddrPort
}

func (f *forwarder) NewConnection(ctx context.Context, c net.Conn, md M.Metadata) error {
	// Forward the tunneled TCP connection to the local echo server. The echo
	// server's response (JSON with token_ok) travels back through the WG
	// tunnel, proving the full data plane: mihomo client -> WG tunnel ->
	// wgserver -> echo -> WG tunnel -> client.
	//
	// NOTE: in the gvisor TCP forwarder, md.Source is the tunnel client
	// (10.66.0.2:port) and md.Destination is the stack's local TCP endpoint
	// (10.66.0.1:ephemeral) — the original destination (203.0.113.1:port) is
	// NOT preserved. So we always relay to the fixed echo target.
	log.Printf("FWD NewConnection src=%s dst=%s target=%s", md.Source, md.Destination, f.target)
	up, err := net.Dial("tcp", f.target.String())
	if err != nil {
		log.Printf("FWD dial %s failed: %v", f.target, err)
		c.Close()
		return err
	}
	log.Printf("FWD dialed %s, relaying", f.target)
	bufio := make([]byte, 32*1024)
	go func() {
		for {
			n, err := c.Read(bufio)
			if n > 0 {
				log.Printf("FWD client->echo %d bytes", n)
				if _, werr := up.Write(bufio[:n]); werr != nil {
					log.Printf("FWD write echo failed: %v", werr)
					return
				}
			}
			if err != nil {
				log.Printf("FWD client read end: %v", err)
				up.Close()
				return
			}
		}
	}()
	go func() {
		for {
			n, err := up.Read(bufio)
			if n > 0 {
				log.Printf("FWD echo->client %d bytes", n)
				if _, werr := c.Write(bufio[:n]); werr != nil {
					log.Printf("FWD write client failed: %v", werr)
					return
				}
			}
			if err != nil {
				log.Printf("FWD echo read end: %v", err)
				c.Close()
				return
			}
		}
	}()
	return nil
}

func (f *forwarder) NewPacket(ctx context.Context, key netip.AddrPort, buffer *buf.Buffer, metadata M.Metadata, init func(natConn N.PacketConn) N.PacketWriter) {
	// UDP forwarding not needed for this PoC; drop.
}

func keypair() (priv, pub string) {
	k, err := ecdh.X25519().GenerateKey(rand.Reader)
	if err != nil {
		log.Fatal(err)
	}
	return base64.StdEncoding.EncodeToString(k.Bytes()),
		base64.StdEncoding.EncodeToString(k.PublicKey().Bytes())
}

func base64ToHex(b64 string) string {
	raw, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		log.Fatalf("bad base64 key: %v", err)
	}
	return hex.EncodeToString(raw)
}

func main() {
	if len(os.Args) < 3 {
		fmt.Fprintln(os.Stderr, "usage: wgserver <udp-port> <client-public-key> [forward-port]")
		os.Exit(2)
	}
	port, err := netip.ParseAddrPort("127.0.0.1:" + os.Args[1])
	if err != nil {
		log.Fatal(err)
	}
	clientPub := os.Args[2]
	fwdPort := 80
	if len(os.Args) >= 4 {
		fmt.Sscanf(os.Args[3], "%d", &fwdPort)
	}
	target := netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(fwdPort))

	srvPriv, srvPub := keypair()

	// wireguard-go IpcSet expects hex keys; the mihomo config uses base64.
	privHex := base64ToHex(srvPriv)
	clientPubHex := base64ToHex(clientPub)

	stack, err := singwg.NewStackDevice([]netip.Prefix{netip.MustParsePrefix("10.66.0.1/24")}, 1420)
	if err != nil {
		log.Fatalf("stack: %v", err)
	}
	fwd := &forwarder{target: target}
	if err := stack.RegisterForward(singwg.ForwardOptions{Handler: fwd}); err != nil {
		log.Fatalf("register forward: %v", err)
	}

	bind := conn.NewStdNetBind()
	dev := device.NewDevice(stack, bind, device.NewLogger(2, "wgserver: "), 2)
	cfg := fmt.Sprintf("listen_port=%d\nprivate_key=%s\npublic_key=%s\nallowed_ip=10.66.0.0/24\n",
		port.Port(), privHex, clientPubHex)
	if err := dev.IpcSet(cfg); err != nil {
		log.Fatalf("ipcset: %v", err)
	}
	if err := stack.Start(); err != nil {
		log.Fatalf("start: %v", err)
	}
	fmt.Printf("WG_SERVER_OK public_key=%s port=%d\n", srvPub, port.Port())

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	<-ctx.Done()
	dev.Close()
}
