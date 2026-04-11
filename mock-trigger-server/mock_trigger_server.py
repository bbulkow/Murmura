#!/usr/bin/env python3
"""
Mock Haven Trigger Server for end-to-end testing.

Replaces the real Haven Trigger Server so you can test the full chain:
    mock-trigger-server -> real mur-gateway -> real device

Flow:
  1. Starts an HTTP API on port 5002 (matches real trigger server)
  2. Mur Gateway registers via POST /api/register
  3. This server connects back to the gateway on its upstream port (TCP_SOCKET)
  4. You type commands to send trigger events through the full chain

Usage:
    python mock_trigger_server.py
    python mock_trigger_server.py --port 5002

Then point your mur-gateway at this server:
    python mur_gateway.py --trigger-host <this-pc-ip> --trigger-port 5002
"""

import argparse
import json
import os
import socket
import threading
import time
import sys
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- Configuration ---

DEFAULT_TRIGGERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'triggers.json')

triggers = []

# --- Service connections ---

# Connected services: name -> {host, port, socket, protocol}
services = {}
services_lock = threading.Lock()

# Event ID counter
event_id_counter = 0
event_id_lock = threading.Lock()


def next_event_id():
    global event_id_counter
    with event_id_lock:
        event_id_counter += 1
        return event_id_counter


# --- HTTP API (matches Haven Trigger Server) ---

@app.route('/api/triggers', methods=['GET'])
def get_triggers():
    return jsonify({"triggers": triggers, "last_modified": datetime.now().isoformat()})


@app.route('/api/register', methods=['POST'])
def register_service():
    data = request.json
    name = data.get('name', '')
    host = data.get('host', 'localhost')
    port = data.get('port')
    protocol = data.get('protocol', 'TCP_SOCKET')

    if not name or not port:
        return jsonify({"error": "name and port required"}), 400

    print(f"\n  [REGISTER] {name} at {host}:{port} ({protocol})")

    if protocol == 'TCP_SOCKET':
        # Connect to the service
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((host, port))
            sock.settimeout(None)
            print(f"  [CONNECTED] TCP socket to {name} at {host}:{port}")

            with services_lock:
                # Close old connection if any
                old = services.get(name)
                if old and old.get('socket'):
                    try:
                        old['socket'].close()
                    except Exception:
                        pass
                services[name] = {
                    'host': host, 'port': port, 'socket': sock,
                    'protocol': protocol, 'registered_at': datetime.now().isoformat()
                }

            return jsonify({
                "message": "Service registered successfully",
                "registration": {
                    "name": name, "port": port, "host": host,
                    "protocol": protocol, "socket_status": "connected"
                }
            })
        except Exception as e:
            print(f"  [ERROR] Failed to connect to {name}: {e}")
            return jsonify({"error": f"Failed to connect: {e}"}), 500
    else:
        return jsonify({"error": f"Unsupported protocol: {protocol}"}), 400


@app.route('/api/registrations', methods=['GET'])
def get_registrations():
    with services_lock:
        result = []
        for name, svc in services.items():
            result.append({
                "name": name,
                "host": svc['host'],
                "port": svc['port'],
                "protocol": svc['protocol'],
                "socket_connected": svc.get('socket') is not None,
            })
    return jsonify({"services": result})


# --- Event dispatch ---

def send_event(trigger_name, value):
    """Send a trigger event to all connected services."""
    eid = next_event_id()
    event = {
        "name": trigger_name,
        "value": value,
        "id": eid,
        "timestamp": datetime.now().isoformat(),
    }
    line = json.dumps(event) + "\n"

    sent = 0
    with services_lock:
        for name, svc in list(services.items()):
            sock = svc.get('socket')
            if not sock:
                continue
            try:
                sock.sendall(line.encode('utf-8'))
                sent += 1
            except Exception as e:
                print(f"  [ERROR] Send to {name} failed: {e}")
                try:
                    sock.close()
                except Exception:
                    pass
                svc['socket'] = None

    return sent, eid


# --- Interactive CLI ---

def print_help():
    print("""
  Commands:
    send <trigger_name> <value>   Send a trigger event (works for any trigger type)
    on <trigger_name>             Shortcut: send value "On"  (On/Off triggers)
    off <trigger_name>            Shortcut: send value "Off" (On/Off triggers)
    discrete <value>              Shortcut: send value on the first Discrete trigger
    continuous <value>            Shortcut: send value on the first Continuous trigger
    triggers                      List configured triggers
    services                      List connected services
    help                          Show this help
    quit                          Exit
""")


def find_trigger_by_type(trigger_type):
    """Find the first trigger name of the given type."""
    for t in triggers:
        if t.get('type') == trigger_type:
            return t['name']
    return None


def interactive_loop():
    """Run the interactive command loop in a separate thread."""
    # Wait for Flask to start
    time.sleep(1)
    print("\n  Mock Haven Trigger Server ready.")
    print(f"  Waiting for mur-gateway to register...")
    print(f"  Type 'help' for commands.\n")

    while True:
        try:
            line = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Shutting down...")
            break

        if not line:
            continue

        parts = line.split(None, 2)
        cmd = parts[0].lower()

        if cmd == 'help':
            print_help()

        elif cmd == 'quit' or cmd == 'exit':
            print("  Shutting down...")
            import os
            os._exit(0)

        elif cmd == 'triggers':
            for t in triggers:
                range_info = ''
                if 'range' in t:
                    r = t['range']
                    if 'values' in r:
                        range_info = f"  values={r['values']}"
                    elif 'min' in r:
                        range_info = f"  min={r['min']} max={r['max']}"
                print(f"    {t['name']:30s}  type={t['type']}{range_info}")

        elif cmd == 'services':
            with services_lock:
                if not services:
                    print("    (no services registered)")
                for name, svc in services.items():
                    status = "connected" if svc.get('socket') else "disconnected"
                    print(f"    {name}: {svc['host']}:{svc['port']} ({status})")

        elif cmd == 'send' and len(parts) >= 3:
            trigger_name = parts[1]
            value = parts[2]
            sent, eid = send_event(trigger_name, value)
            print(f"    Sent event #{eid}: {trigger_name} = {value} -> {sent} service(s)")

        elif cmd == 'discrete' and len(parts) >= 2:
            value = parts[1]
            name = find_trigger_by_type('Discrete')
            if not name:
                print("    No Discrete trigger configured")
            else:
                sent, eid = send_event(name, value)
                print(f"    Sent event #{eid}: {name} = {value} -> {sent} service(s)")

        elif cmd == 'continuous' and len(parts) >= 2:
            value = parts[1]
            name = find_trigger_by_type('Continuous')
            if not name:
                print("    No Continuous trigger configured")
            else:
                sent, eid = send_event(name, value)
                print(f"    Sent event #{eid}: {name} = {value} -> {sent} service(s)")

        elif cmd == 'on' and len(parts) >= 2:
            trigger_name = parts[1]
            sent, eid = send_event(trigger_name, "On")
            print(f"    Sent event #{eid}: {trigger_name} = On -> {sent} service(s)")

        elif cmd == 'off' and len(parts) >= 2:
            trigger_name = parts[1]
            sent, eid = send_event(trigger_name, "Off")
            print(f"    Sent event #{eid}: {trigger_name} = Off -> {sent} service(s)")

        else:
            print(f"    Unknown command: {line}")
            print_help()


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Mock Haven Trigger Server for end-to-end Murmura testing"
    )
    parser.add_argument("--port", type=int, default=5002,
                        help="HTTP API port (default: 5002, matches real trigger server)")
    parser.add_argument("--triggers-file", type=str, default=DEFAULT_TRIGGERS_FILE,
                        help=f"JSON file with trigger definitions (default: {DEFAULT_TRIGGERS_FILE})")
    args = parser.parse_args()

    global triggers
    try:
        with open(args.triggers_file) as f:
            data = json.load(f)
            triggers = data.get('triggers', data) if isinstance(data, dict) else data
            print(f"  Loaded {len(triggers)} trigger(s) from {args.triggers_file}")
    except Exception as e:
        print(f"  Error loading triggers file '{args.triggers_file}': {e}")
        sys.exit(1)

    print(f"\n  Mock Haven Trigger Server")
    print(f"  HTTP API on port {args.port}")
    print(f"  Triggers: {len(triggers)} configured")
    for t in triggers:
        print(f"    - {t['name']} ({t['type']})")

    # Start interactive CLI in background thread
    cli_thread = threading.Thread(target=interactive_loop, daemon=True)
    cli_thread.start()

    # Run Flask (suppress banner noise)
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.WARNING)

    app.run(host='0.0.0.0', port=args.port, debug=False)


if __name__ == '__main__':
    main()
