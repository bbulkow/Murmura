# mur-scene-server

Fleet-wide **active scene** authority for Murmura. Runs on port **5003**, serves a
web UI at `/`, and fires the `SceneChange` trigger whenever the active scene changes.

## What this service owns (and what it does not)

This is the part people get wrong, so it is the first thing in this file:

> **mur-scene-server decides _which_ scene is active fleet-wide.
> Each MUR decides _what that scene sounds like_.**

| | Lives here | Lives on each MUR |
|---|---|---|
| The set of scene **names** | yes | — |
| Which scene is **active** | yes | mirrors what it is told |
| Schedules ("go to `night` at 20:00") | yes | — |
| Per-track file, volume, mode, triggers | **no** | `/sdcard/scenes.json` |

Editing what a scene *plays* is [mur-config-server](../mur-config-server/)'s job.
This service never touches track content, and it never talks to a device directly.

A scene is just a name here. The name is the join key between this service and the
`scenes` dict on every MUR.

## Why it exists

The rest of the system was already written against a scene service that this repo
did not contain:

- `mur-gateway` primes its scene cache from `GET /api/scenes/active` at startup
  (30 x 2 s retry) and lazily refreshes it on TTL expiry. See
  `DEFAULT_SCENE_SERVICE_URL` and `_prime_scene_cache()` in
  [`mur_gateway.py`](../mur-gateway/mur_gateway.py). The default it ships with is
  `http://localhost:5003` — exactly where this service listens, so no gateway
  configuration is needed.
- [`SYNC_DESIGN.md`](../SYNC_DESIGN.md) makes it an operational rule: **never fire
  `SceneChange` from anywhere except the scene server.** `mur-conductor` and
  `mur-config-server` both enforce that in validation.

Until now the only implementations were a test mock and the Haven Scene Service in
a different repo, so a Murmura-only install had no way to set the fleet-wide scene.

## Run

```bash
pip install -r requirements.txt
python mur_scene_server.py
```

Then open <http://localhost:5003/>.

| Flag | Purpose |
|---|---|
| `-p`, `--port N` | HTTP port. Precedence: `--port` > `$MUR_SCENE_SERVER_PORT` > 5003 |
| `--ephemeral` | In-memory only: seed from `config.json`, persist nothing. Test mode. |
| `--cli` | Enable the interactive command prompt (auto-enabled by `--ephemeral`) |

The interactive prompt only attaches when stdin is a terminal, so running under
systemd or in the background is safe.

### Ephemeral mode

`--ephemeral` replaces the old `mock-scene-server`: state is in memory, seeded from
`seed_scenes` / `seed_active_scene`, and nothing is written to disk. Use it for
end-to-end tests where you want a known scene list every run.

```bash
python mur_scene_server.py --ephemeral --port 5013
```

## Configuration

Operational knobs live in [`config.json`](config.json) next to the script, merged
over built-in defaults (the `mur-gateway` pattern — edit on site, no code change).
CLI flags cover only deployment targets.

| Field | Default | Purpose |
|---|---|---|
| `trigger_server_url` | `http://127.0.0.1:5002` | Where `SceneChange` is POSTed. `null`/`""` disables the push. |
| `register_with_trigger_server` | `"auto"` | `"auto"` = probe once, stop on 404/405. `true` = keep retrying. `false` = never. |
| `scheduler_interval_s` | `30` | How often schedules are checked. |
| `seed_scenes` | `["night","day","show"]` | Ephemeral mode only. |
| `seed_active_scene` | `"day"` | Ephemeral mode only; must be in `seed_scenes`. |
| `config_server_url` | `http://127.0.0.1:8765` | Related Services link. |
| `gateway_status_url` | `http://127.0.0.1:4001/status` | Related Services link. |
| `conductor_status_url` | `http://127.0.0.1:4002/status` | Related Services link. |
| `trigger_server_ui_url` | `http://127.0.0.1:5002/api/triggers` | Related Services link. |

The four link fields are cosmetic — never polled. The UI rewrites a `localhost`
host to whatever host you are browsing from, so the links work from a laptop.

## Scene names

Names are validated **server-side** against the limits the firmware actually has,
so any name this service accepts is storable on a MUR:

- **1–31 characters** (`MAX_SCENE_NAME_LEN` is 32 in
  [`main/scene_manager.h`](../main/scene_manager.h); the 32nd byte is the NUL)
- **letters, digits, hyphen, underscore** only (per [`HTTP_API.md`](../HTTP_API.md))

A device holds at most **16 scenes** (`MAX_SCENES`). Going over that is a
**warning, not an error** — the limit is per-device while this list is fleet-wide,
so a hard reject would block legitimate operations. `POST /api/scenes` still
returns 201 but adds a `warning` field, and `GET /api/scenes` reports
`over_device_limit: true` so the UI can flag it.

## HTTP API

### Scenes

#### `GET /api/scenes/active` — **frozen contract**

```json
{"active_scene": "night"}
```

`mur-gateway` depends on this exact shape. Do not add required fields or rename
the key.

#### `POST /api/scenes/active`

```json
{"name": "night"}
```

Requires a JSON object with a `name` key. `{"name": null}` clears the active
scene. A missing key, non-object body, or unparseable JSON is a **400** — see
"Differences from the Haven original" below.

On success the service POSTs `{"name": "SceneChange", "value": "<scene>"}` to
`{trigger_server_url}/api/trigger-event` in a background thread. The push is
best-effort: if the trigger server is down the request still returns 200, the
state is still persisted, and `mur-gateway` picks the change up on its next
HTTP refresh instead.

#### `GET /api/scenes`

```json
{
  "scenes": ["day", "night", "show"],
  "active_scene": "night",
  "count": 3,
  "max_device_scenes": 16,
  "over_device_limit": false
}
```

#### `POST /api/scenes` — create

```json
{"name": "show"}
```
`201` with `{"message", "scene"}`, plus `"warning"` past 16 scenes. `400` on a
duplicate or an invalid name.

#### `DELETE /api/scenes/<name>`

`200` on success, `404` if it does not exist, `400` if it is the active scene.
Any schedules referencing the scene are removed with it.

### Schedules

Schedules activate a scene at a wall-clock time.

| Route | Notes |
|---|---|
| `GET /api/schedules` | `{"schedules": [...]}` |
| `POST /api/schedules` | `{"scene", "time": "HH:MM", "repeat": "daily"\|"once"}` → 201 |
| `PUT /api/schedules/<id>` | All three fields required; resets `last_fired` → 200 |
| `DELETE /api/schedules/<id>` | 200 / 404 |

`once` schedules delete themselves after firing. A scheduled activation fires
`SceneChange` exactly like a manual one. Times are normalized (`8:5` → `08:05`).

Status codes come from a reason code, not from sniffing the error message: an
unknown id is `404`, a bad scene/time/repeat is `400`.

### `GET /health`

```json
{
  "status": "healthy",
  "service": "mur-scene-server",
  "scenes_count": 3,
  "active_scene": "night",
  "schedules_count": 1,
  "over_device_limit": false,
  "max_device_scenes": 16,
  "ephemeral": false,
  "trigger_server_url": "http://127.0.0.1:5002",
  "trigger_server_registered": false,
  "registration_supported": false,
  "last_push_ok": true,
  "scenes_file": "/home/brian/Murmura/mur-scene-server/mur_scene_server/scenes.json"
}
```

## Persistence

State lives in `mur_scene_server/scenes.json` (gitignored), written atomically
via a temp file plus `os.replace`, so a crash mid-write cannot corrupt it.

```json
{
  "scenes": ["day", "night", "show"],
  "active_scene": "night",
  "schedules": [
    {"id": "b3d2…", "scene": "day", "time": "08:00", "repeat": "daily",
     "created": "2026-08-12T09:00:00", "last_fired": null}
  ],
  "last_updated": "2026-08-12T09:00:00.123456"
}
```

The format matches the Haven Scene Service, so a `scenes.json` from a Haven box
can be dropped in directly. Scene names are written sorted so the file diffs
cleanly.

If the file fails to parse at startup the service logs the error, starts empty in
memory, and **leaves the file alone** so you can recover it by hand.

## Trigger-server integration

Two outbound behaviours, both best-effort:

1. **`SceneChange` push** — `POST {trigger_server_url}/api/trigger-event` on every
   active-scene change. Works with `mock-trigger-server` and `mur-conductor`.
2. **Device registration** — `POST {trigger_server_url}/api/register-device`,
   advertising `SceneChange` as a Discrete trigger whose value list is the live
   scene list.

**No Murmura trigger server currently implements `/api/register-device`** —
`mock-trigger-server` and `mur-conductor` both expose `/api/register` instead. A
404/405 is therefore the expected answer, not a failure: the service logs it once
at INFO, marks the capability absent, and never asks again. `mur-config-server`
reads the scene list straight from `GET /api/scenes` instead, so nothing depends
on registration succeeding.

Registration is owned by a single long-lived thread. Scene create/delete just wakes
it for one attempt.

## Deployment

See [SYSTEMD_INSTALL.md](SYSTEMD_INSTALL.md). The unit has **no ordering dependency**
on the trigger server or gateway — every side already retries, so an ordering
constraint would buy nothing and slow boot.

## Differences from the Haven original

Ported from `haven/Triggers/scene_service.py` + `scene_service.html`. Behaviour
changes, all deliberate:

| Change | Why |
|---|---|
| `POST /api/scenes/active` rejects a malformed body | The original treated *any* unparseable body as "clear the active scene" and fired `SceneChange` with an empty value — a typo'd curl dropped the whole fleet out of its scene. |
| Scene names validated server-side | The original enforced length only via `maxlength` in the UI, so the API would accept names no MUR could store. |
| `404` vs `400` from reason codes | The original chose its status with `400 if 'active' in message else 404`, and `404 if 'not found' in result else 400` for schedules. |
| Idle scheduler no longer rewrites state | The original saved every tick for as long as *any* scene was active — a disk write every 30 s, forever. |
| Schedule validation moved inside the lock | The original checked scene existence outside the lock and mutated inside it. |
| One registrar thread | The original spawned a fresh 30-attempt thread on every scene create and delete. |
| `/api/register-device` 404 handled as "unsupported" | No Murmura trigger server implements it; the original would retry 30 times and log errors on every install. |
| `templates/` + `static/` instead of `static_folder='.'` | The original served its entire working directory over HTTP, `scenes.json` included. |
| Scene names persisted sorted | A `set` serializes in arbitrary order, so the file churned on every write. |
| CLI survives EOF on stdin | The mock called `os._exit(0)` on EOF, so running it detached killed the service. |
| `/api/scene-status` removed | It aggregated Haven's flame service and OSC proxy, neither of which exists in Murmura. The Related Services card replaces it. |

## Replacing mock-scene-server

`mock-scene-server/` is gone; `--ephemeral` covers what it did, with the same
seeded-scene config and interactive CLI. Its README also claimed you had to
hand-sync `mock-trigger-server`'s `SceneChange` `range.values` with the scene list
or mur-config-server's dropdown would break. That was inaccurate: the batch scene
dropdown is built from the union of scenes reported by online devices. The only
consumer of `range.values` is the scene-trigger mismatch warning on the device
detail page, which now prefers this service's live list.

## See also

- [`../SYNC_DESIGN.md`](../SYNC_DESIGN.md) — why only this service may fire `SceneChange`
- [`../MUR_PROTOCOL.md`](../MUR_PROTOCOL.md) — the `scene` message devices receive
- [`../HTTP_API.md`](../HTTP_API.md) — the device-side scene API
- [`../mur-config-server/README.md`](../mur-config-server/README.md) — editing scene content
