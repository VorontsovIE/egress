#!/usr/bin/env python3
"""Fake SOCKS5 node for the PoC.

Runs N named SOCKS5 servers (one per fake "provider node"), each on its own
port, forwarding to the real destination. Every accepted SOCKS request is
logged to a JSONL file with the node name at request-parse time (before the
upstream dial), so tests can assert which node was selected and used even
when the upstream destination is unreachable.

Usage: fake_node.py --base-port 18001 --nodes a,b,c --log nodes.jsonl
"""
import argparse
import asyncio
import json
import os
import time


class FakeNode:
    def __init__(self, name: str, port: int, log_path: str):
        self.name = name
        self.port = port
        self.log_path = log_path
        self.server = None
        self.conns = 0

    def log(self, **kw):
        kw.setdefault("node", self.name)
        kw.setdefault("ts", time.time())
        line = json.dumps(kw) + "\n"
        # append + flush; single-line writes are atomic enough for the PoC
        with open(self.log_path, "a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        try:
            data = await asyncio.wait_for(reader.readexactly(2), 10)
            if data[0] != 0x05:
                writer.close()
                return
            nmethods = data[1]
            await asyncio.wait_for(reader.readexactly(nmethods), 10)
            writer.write(b"\x05\x00")
            await writer.drain()
            req = await asyncio.wait_for(reader.readexactly(4), 10)
            atyp = req[3]
            if atyp == 0x01:
                dst = await asyncio.wait_for(reader.readexactly(4), 10)
                host = ".".join(str(b) for b in dst)
            elif atyp == 0x03:
                ln = (await asyncio.wait_for(reader.readexactly(1), 10))[0]
                host = (await asyncio.wait_for(reader.readexactly(ln), 10)).decode()
            elif atyp == 0x04:
                dst = await asyncio.wait_for(reader.readexactly(16), 10)
                host = ":".join(f"{b:02x}" for b in dst)
            else:
                writer.close()
                return
            portdata = await asyncio.wait_for(reader.readexactly(2), 10)
            dport = portdata[0] << 8 | portdata[1]

            # Log at parse time: proves this node was selected for the request.
            self.log(dst=f"{host}:{dport}", client=str(peer), phase="selected")
            self.conns += 1

            # Respond directly with a canned HTTP 200 identifying this node.
            # No upstream relay: the response body is the routing proof, and
            # the test can target any non-LAN address (so the hard-direct
            # rule cannot intercept the connection).
            writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await writer.drain()
            body = (f'NODE:{self.name}\n'
                    f'dst:{host}:{dport}\n'
                    f'client:{peer[0] if peer else "?"}\n').encode()
            resp = (b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Connection: close\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode()
                    + b"\r\n" + body)
            writer.write(resp)
            await writer.drain()
            self.log(dst=f"{host}:{dport}", phase="responded")
            await asyncio.sleep(0.05)
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.port)

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-port", type=int, default=18001)
    ap.add_argument("--nodes", required=True)
    ap.add_argument("--log", default="nodes.jsonl")
    args = ap.parse_args()

    names = [n.strip() for n in args.nodes.split(",") if n.strip()]
    nodes = [FakeNode(n, args.base_port + i, args.log) for i, n in enumerate(names)]
    for n in nodes:
        await n.start()
    print(json.dumps({"started": [n.name for n in nodes],
                      "ports": {n.name: n.port for n in nodes}}), flush=True)
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for n in nodes:
            await n.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
