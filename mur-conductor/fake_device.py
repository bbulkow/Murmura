#!/usr/bin/env python3
"""
Fake MUR device - a development stand-in for verifying ensemble sync without
hardware.

Speaks the device side of the Mur Protocol: connects outbound to the gateway,
announces an id with a TSF reading, subscribes to trigger names, answers
tsf_query, and prints every trigger event it receives together with the
target_tsf_us deadline and how far in the future that deadline is.

THE THING TO LOOK FOR: run two or more instances and confirm that for each
downbeat they print the SAME target_tsf_us. That identical deadline is what
makes real devices start together; if the values differ, the fan-out is not
sharing a deadline and no amount of firmware tuning will align them.

The reported TSF is derived from the host wall clock, so several instances on
one machine agree closely - good enough to exercise the gateway's TSF map and
its cross-device jitter check.

IMPORTANT when running more than one instance on one host: the gateway evicts
any existing connection coming from the same peer IP, treating it as a device
that reconnected (see _handle_device_connection in mur_gateway.py). Two fake
devices both sourced from 127.0.0.1 therefore kick each other off in a loop and
never both stay subscribed. Give each instance its own source address with
--source-ip; the whole 127.0.0.0/8 range is loopback, so 127.0.0.2, 127.0.0.3
and so on work. Real MURs each have their own LAN address and never hit this.

Usage:
  python fake_device.py --id MUR-001 --triggers EnsembleMain
  python fake_device.py --id MUR-002 --triggers EnsembleMain --source-ip 127.0.0.2
"""

import argparse
import asyncio
import json
import sys
import time

RECONNECT_S = 5.0


def tsf_us() -> int:
    """A plausible stand-in for esp_wifi_get_tsf_time()."""
    return int(time.time() * 1_000_000) & ((1 << 64) - 1)


class FakeDevice:
    def __init__(self, device_id: str, host: str, port: int, triggers: list,
                 source_ip: str = ""):
        self.device_id = device_id
        self.host = host
        self.port = port
        self.triggers = triggers
        self.source_ip = source_ip
        self.events = 0

    def log(self, msg: str) -> None:
        print(f"  [{time.strftime('%H:%M:%S')}] {self.device_id}: {msg}", flush=True)

    async def run(self) -> None:
        while True:
            try:
                await self._session()
            except (ConnectionError, OSError) as e:
                self.log(f"connection failed: {e}")
            except asyncio.CancelledError:
                raise
            self.log(f"reconnecting in {RECONNECT_S:.0f}s")
            await asyncio.sleep(RECONNECT_S)

    async def _session(self) -> None:
        local_addr = (self.source_ip, 0) if self.source_ip else None
        reader, writer = await asyncio.open_connection(
            self.host, self.port, local_addr=local_addr)
        self.log(f"connected to gateway {self.host}:{self.port}"
                 f"{f' from {self.source_ip}' if self.source_ip else ''}")

        await self._send(writer, {"type": "announce", "id": self.device_id,
                                  "tsf_us": tsf_us()})
        await self._send(writer, {"type": "subscribe", "triggers": self.triggers})
        self.log(f"subscribed to {self.triggers}")

        buf = ""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    self.log("gateway closed the connection")
                    return
                buf += data.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        await self._handle(writer, line)
        finally:
            writer.close()

    async def _send(self, writer: asyncio.StreamWriter, obj: dict) -> None:
        writer.write((json.dumps(obj) + "\n").encode("utf-8"))
        await writer.drain()

    async def _handle(self, writer: asyncio.StreamWriter, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            self.log(f"bad JSON: {line[:100]}")
            return

        msg_type = msg.get("type")
        if msg_type == "welcome":
            self.log(f"welcome from {msg.get('gateway')} v{msg.get('version')}")
            return
        if msg_type == "tsf_query":
            await self._send(writer, {"type": "tsf_reply", "tsf_us": tsf_us()})
            return
        if msg_type == "scene":
            self.log(f"scene push: {msg.get('value')!r}")
            return
        if msg_type is not None:
            self.log(f"unhandled message type {msg_type!r}")
            return

        # No "type" field means a trigger event.
        self.events += 1
        name = msg.get("name")
        target = msg.get("target_tsf_us")
        if target is None:
            self.log(f"EVENT #{self.events} '{name}' - NO target_tsf_us "
                     f"(fires immediately; devices will NOT be aligned)")
        else:
            lead_ms = (int(target) - tsf_us()) / 1000.0
            self.log(f"EVENT #{self.events} '{name}' target_tsf_us={target} "
                     f"(lead {lead_ms:+.1f} ms)")


async def amain(args: argparse.Namespace) -> None:
    device = FakeDevice(args.id, args.gateway, args.port, args.triggers,
                        args.source_ip)
    await device.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake MUR device for sync verification")
    parser.add_argument("--id", required=True, help="Device id to announce")
    parser.add_argument("--gateway", default="127.0.0.1", help="Gateway host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=4000, help="Gateway device port (default: 4000)")
    parser.add_argument("--triggers", nargs="+", default=[],
                        help="Trigger names to subscribe to")
    parser.add_argument("--source-ip", default="",
                        help="Bind this source address (e.g. 127.0.0.2). Required when "
                             "running several instances on one host - the gateway evicts "
                             "connections that share a peer IP.")
    args = parser.parse_args()

    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
