# Running mur-scene-server under systemd

Installs `mur-scene-server` as a service that starts on boot. Written for the
Raspberry Pi show host; adjust `User`, `Group` and paths if yours differ.

## 1. Prerequisites

```bash
cd ~/Murmura/mur-scene-server
pip3 install -r requirements.txt
python3 mur_scene_server.py        # confirm it starts, then Ctrl-C
```

Check <http://localhost:5003/> loads before going further.

## 2. Check the unit file

[`mur-scene-server.service`](mur-scene-server.service) assumes:

| Setting | Value |
|---|---|
| User / Group | `brian` |
| Working directory | `/home/brian/Murmura/mur-scene-server` |
| Port | `5003` via `MUR_SCENE_SERVER_PORT` |
| Python | `/usr/bin/python3` |

`WorkingDirectory` matters less here than for mur-config-server — this service
resolves `config.json` and its state directory relative to the **script file**,
not the CWD — but keep it correct anyway so relative paths in logs make sense.

To run on a different port, change the `Environment=` line rather than adding a
`--port` flag, so the env var stays the single source of truth.

## 3. Install and enable

```bash
sudo cp ~/Murmura/mur-scene-server/mur-scene-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable mur-scene-server
sudo systemctl start mur-scene-server
sudo systemctl status mur-scene-server
```

## 4. Verify

```bash
curl -s http://localhost:5003/health | python3 -m json.tool
curl -s http://localhost:5003/api/scenes/active
```

Then confirm the gateway is actually reading from it:

```bash
curl -s http://localhost:4001/status | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d['scene_service_url'], d['cached_scene'])"
```

`scene_service_url` should be `http://localhost:5003` (mur-gateway's default — it
needs no configuration) and `cached_scene` should match this service's active scene.

## 5. Logs

```bash
sudo journalctl -u mur-scene-server -f
sudo journalctl -u mur-scene-server --since "1 hour ago"
```

Expected at startup, once:

```
Trigger server at http://127.0.0.1:5002 does not implement /api/register-device
(HTTP 404). Scene list will not be advertised upstream; ...
```

That line is **normal** — no Murmura trigger server implements that endpoint. It
appears once and never repeats. If you see it repeatedly, something is restarting
the service.

## 6. Start ordering

The unit intentionally declares **no** `After=` or `Requires=` on the trigger
server, gateway, or conductor. Every side already tolerates any order:

- `mur-gateway` primes its scene cache with a 30 x 2 s retry.
- This service retries its registration, and its `SceneChange` push is
  best-effort with the gateway's HTTP refresh as the backstop.

Adding an ordering dependency would slow boot and make failures harder to reason
about without preventing anything.

## 7. Common operations

```bash
sudo systemctl restart mur-scene-server
sudo systemctl stop mur-scene-server
sudo systemctl disable mur-scene-server
```

State survives restarts in `mur_scene_server/scenes.json`. To reset the service to
an empty scene list, stop it, delete that file, and start it again.

## Troubleshooting

**`[FATAL] Cannot bind 0.0.0.0:5003`** — something already holds the port, often a
manually started copy. `ss -lntp 'sport = :5003'` to find it. The service refuses
to start rather than silently running a second instance.

**Gateway shows a stale `cached_scene`** — the gateway keeps its last known value
when the scene service is unreachable, by design. Check this service is up and that
`scene_service_url` in `/status` points at it.

**Scene changes do not reach devices** — check `last_push_ok` in `/health`. If it is
`false`, the trigger server is unreachable and only the gateway's slower HTTP
refresh is carrying changes. Confirm the trigger server is running on the port in
`config.json`.
