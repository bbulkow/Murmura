#!/usr/bin/env python3
"""
Trigger longevity test tool.

Replaces the Mur Gateway for automated trigger testing. Accepts device
connections on a TCP port, handles the Mur Protocol (announce/subscribe),
and runs configurable test scripts that generate trigger events.

Usage:
    python trigger_test.py --list
    python trigger_test.py --script basic-on-off
    python trigger_test.py --script basic-on-off --trigger "RedButton.Button_1" --on-time 3 --off-time 3
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Ensure scripts package is importable
sys.path.insert(0, str(__file__ and __import__("pathlib").Path(__file__).parent))

import scripts as script_registry

logger = logging.getLogger("trigger-test")

DEFAULT_PORT = 4000
VERSION = "1.0"

# ANSI colours
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"


# ---------------------------------------------------------------------------
#  Data structures
# ---------------------------------------------------------------------------

@dataclass
class DeviceConnection:
    """A connected Mur device."""
    device_id: str
    writer: asyncio.StreamWriter
    triggers: set = field(default_factory=set)
    connected_at: float = field(default_factory=time.time)
    peer: str = ""

    async def send_line(self, data: str) -> bool:
        try:
            self.writer.write((data + "\n").encode("utf-8"))
            await self.writer.drain()
            return True
        except (ConnectionError, OSError) as e:
            logger.warning("Send to %s failed: %s", self.device_id, e)
            return False


# ---------------------------------------------------------------------------
#  Script context — passed to test scripts
# ---------------------------------------------------------------------------

class ScriptContext:
    """Context object provided to test scripts with helpers for sending events."""

    def __init__(self, server: "TestServer", args: argparse.Namespace):
        self._server = server
        self.args = args
        self.trigger = args.trigger
        self.on_time = args.on_time
        self.off_time = args.off_time
        self.cycle = 0
        self._start_time = time.time()
        self._event_id = 0

    @property
    def elapsed(self) -> float:
        return time.time() - self._start_time

    @property
    def devices(self) -> list[str]:
        return [d.device_id for d in self._server.devices.values()
                if d.device_id != "(unannounced)"]

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  {DIM}{ts}{RESET}  {msg}")

    async def send(self, trigger_name: str, value: str):
        """Send a trigger event to all subscribed devices."""
        self._event_id += 1
        event = json.dumps({
            "name": trigger_name,
            "value": value,
            "id": self._event_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        sent = 0
        for conn_id, device in list(self._server.devices.items()):
            if trigger_name in device.triggers:
                ok = await device.send_line(event)
                if ok:
                    sent += 1
                else:
                    self._server.remove_device(conn_id)

        arrow = f"{GREEN}On{RESET}" if value in ("On", "1") else f"{YELLOW}Off{RESET}"
        self.log(f"{arrow}  {trigger_name} = {value}  →  {sent} device(s)")

    async def wait(self, seconds: float):
        await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
#  Mini Mur Gateway server
# ---------------------------------------------------------------------------

class TestServer:
    """Minimal Mur Protocol server for accepting device connections."""

    def __init__(self, port: int):
        self.port = port
        self.devices: dict[int, DeviceConnection] = {}
        self.subscriptions: dict[str, set[int]] = {}
        self._server: asyncio.Server | None = None

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_connection, "0.0.0.0", self.port
        )
        logger.info("Listening for devices on port %d", self.port)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for device in self.devices.values():
            device.writer.close()
        self.devices.clear()
        self.subscriptions.clear()

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        conn_id = id(writer)
        logger.info("Device connection from %s", peer)

        device = DeviceConnection(
            device_id="(unannounced)",
            writer=writer,
            peer=str(peer),
        )
        self.devices[conn_id] = device

        buf = ""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    logger.info("Device %s disconnected", device.device_id)
                    break
                buf += data.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._process_message(conn_id, line)
        except (ConnectionError, OSError) as e:
            logger.info("Device %s connection error: %s", device.device_id, e)
        finally:
            self.remove_device(conn_id)
            writer.close()

    def _process_message(self, conn_id: int, line: str):
        device = self.devices.get(conn_id)
        if not device:
            return

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Bad JSON from %s: %.100s", device.device_id, line)
            return

        msg_type = msg.get("type")

        if msg_type == "announce":
            device.device_id = msg.get("id", "(unknown)")
            logger.info("Device announced: %s from %s", device.device_id, device.peer)
            welcome = json.dumps({"type": "welcome", "gateway": "trigger-test", "version": VERSION})
            asyncio.ensure_future(device.send_line(welcome))

        elif msg_type == "subscribe":
            triggers = msg.get("triggers", [])
            if isinstance(triggers, list):
                for t in triggers:
                    device.triggers.add(t)
                    self.subscriptions.setdefault(t, set()).add(conn_id)
                logger.info("Device %s subscribed to: %s", device.device_id, triggers)

        elif msg_type == "unsubscribe":
            triggers = msg.get("triggers", [])
            if isinstance(triggers, list):
                for t in triggers:
                    device.triggers.discard(t)
                    if t in self.subscriptions:
                        self.subscriptions[t].discard(conn_id)
                        if not self.subscriptions[t]:
                            del self.subscriptions[t]
                logger.info("Device %s unsubscribed from: %s", device.device_id, triggers)

        else:
            logger.warning("Unknown message type '%s' from %s", msg_type, device.device_id)

    def remove_device(self, conn_id: int):
        device = self.devices.pop(conn_id, None)
        if not device:
            return
        for t in device.triggers:
            if t in self.subscriptions:
                self.subscriptions[t].discard(conn_id)
                if not self.subscriptions[t]:
                    del self.subscriptions[t]
        logger.info("Removed device %s", device.device_id)


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace):
    server = TestServer(args.port)
    await server.start()

    print(f"\n{BOLD}Trigger Test Tool{RESET}")
    print(f"  Port:    {server.port}")
    print(f"  Script:  {args.script}")
    print(f"  Trigger: {args.trigger}")
    print(f"  Timing:  on={args.on_time}s  off={args.off_time}s")
    print(f"\n  Waiting for device connections...  (Ctrl+C to stop)\n")

    # Wait for at least one announced device
    while True:
        announced = [d for d in server.devices.values() if d.device_id != "(unannounced)"]
        if announced:
            names = ", ".join(d.device_id for d in announced)
            print(f"  {GREEN}Device(s) connected: {names}{RESET}\n")
            break
        await asyncio.sleep(0.5)

    # Run the script
    entry = script_registry.get_script(args.script)
    if not entry:
        print(f"Script '{args.script}' not found")
        await server.stop()
        return

    script_fn, description = entry
    print(f"  Running: {CYAN}{args.script}{RESET} — {description}\n")

    ctx = ScriptContext(server, args)
    try:
        await script_fn(ctx)
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()


def main():
    # Discover all test scripts
    script_registry.discover()

    parser = argparse.ArgumentParser(
        description="Trigger longevity test tool — replaces Mur Gateway for automated testing"
    )
    parser.add_argument("--list", action="store_true",
                        help="List available test scripts and exit")
    parser.add_argument("--script", type=str,
                        help="Test script to run")
    parser.add_argument("--trigger", type=str, default="Test.Trigger",
                        help="Trigger name to use (default: Test.Trigger)")
    parser.add_argument("--on-time", type=float, default=5.0,
                        help="Seconds to hold On (default: 5)")
    parser.add_argument("--off-time", type=float, default=5.0,
                        help="Seconds between Off and next On (default: 5)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Device listen port (default: {DEFAULT_PORT})")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable verbose logging")

    args = parser.parse_args()

    # Logging setup
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(name)-14s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.list:
        available = script_registry.list_scripts()
        if not available:
            print("No test scripts found.")
        else:
            print(f"\n{BOLD}Available test scripts:{RESET}\n")
            for name, desc in available:
                print(f"  {CYAN}{name:<20}{RESET} {desc}")
            print()
        return

    if not args.script:
        parser.error("--script is required (use --list to see available scripts)")

    if not script_registry.get_script(args.script):
        available = script_registry.list_scripts()
        names = ", ".join(n for n, _ in available)
        parser.error(f"Unknown script '{args.script}'. Available: {names}")

    # Run with Ctrl+C handling
    loop = asyncio.new_event_loop()
    task = loop.create_task(run(args))

    def shutdown():
        task.cancel()

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT, shutdown)
        loop.add_signal_handler(signal.SIGTERM, shutdown)

    try:
        loop.run_until_complete(task)
    except KeyboardInterrupt:
        task.cancel()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
    finally:
        loop.close()
        print(f"\n{BOLD}Test stopped.{RESET}")


if __name__ == "__main__":
    main()
