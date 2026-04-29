# mock-scene-server

Stand-in for the Haven Scene Service for end-to-end Murmura testing. Holds a list of scene names and which one is active; on every `set_active_scene` (via REST or the interactive CLI) it pushes a `SceneChange` trigger event to a configured trigger server so the full path `scene_server → trigger_server → mur_gateway → MURs` is exercisable.

## Configuration

All configuration lives in [`config.json`](config.json) alongside the script. There are no CLI flags for configuration (only `--port` to run on a non-default port). Edit the file and restart.

| Field | Default | Purpose |
|---|---|---|
| `scenes` | `["default", "night", "day"]` | Seeded scene names. Must be a non-empty list of non-empty strings. |
| `active_scene` | `"default"` | Scene that's active at startup. Must be `null` or one of `scenes`. |
| `trigger_server_url` | `"http://127.0.0.1:5002"` | Where to POST `SceneChange` events. `null` or `""` disables the push. |

## Run

```
python mock_scene_server.py
```

Defaults are sized for running mock-trigger-server on the same machine.

## Keep in sync with mock-trigger-server

**Important:** `mock-trigger-server`'s [`config.json`](../mock-trigger-server/config.json) carries its own list of trigger definitions. One of them is the `SceneChange` Discrete trigger, whose `range.values` is the list of scene names that mur-config-server's UI dropdown expects. **mock-trigger-server does not pull scenes from mock-scene-server** — by design, to keep the two mocks independent and runnable in any order.

When you edit `mock-scene-server/config.json`'s `scenes` field, you must also edit `mock-trigger-server/config.json` `SceneChange` entry's `range.values` to match. Both servers need to be restarted to pick up the change.

A representative pair:

```jsonc
// mock-scene-server/config.json
{
  "scenes": ["night", "day", "show"],
  "active_scene": "day",
  "trigger_server_url": "http://127.0.0.1:5002"
}

// mock-trigger-server/config.json
{
  "port": 5002,
  "triggers": [
    {
      "name": "SceneChange",
      "type": "Discrete",
      "range": { "values": ["night", "day", "show"] }
    },
    ...
  ]
}
```

The mismatch is harmless at trigger dispatch time (mock-trigger-server doesn't validate scene values against its own list — it just blasts `SceneChange={value}` to all registered TCP services). The cost of mismatch is in mur-config-server's UI, which reads `GET /api/triggers` from the gateway (which proxies it from the trigger server) to populate the scene dropdown — wrong list there means scene names don't show or show as stale options.
