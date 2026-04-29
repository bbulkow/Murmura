# TCP Tuning for Murmura

This document captures **why** the TCP-related lwIP knobs in [`sdkconfig.defaults`](sdkconfig.defaults) deviate from ESP-IDF defaults, what was considered and skipped, and what runtime-level socket options to reach for next if the symptoms come back.

If you're touching anything in the LWIP submenu of menuconfig, or you're chasing latency / jitter / stalled-socket bugs, read this first.

## Why we tuned TCP at all

The synchronized multi-MUR playback feature ([SYNC_DESIGN.md](SYNC_DESIGN.md)) makes TCP delivery latency *audible*. Every event from the trigger server is shipped over TCP to the gateway, then over TCP from the gateway to each MUR. The gateway adds `fanout_delay_ms` (default 2500 ms) of slack, but on a small (two- or three-device) deployment with modest packet loss we observed individual events arriving 1–2 s late and either being dropped (`late_policy=drop`) or fired late with a warning (`late_policy=play`).

The mechanism is the RTO calculation and policy:

1. **Coarse-grained RTO ticks.** lwIP stores RTO in slow-tick units. With ESP-IDF's default `TCP_TMR_INTERVAL=250 ms` the slow tick is 500 ms, with a one-tick floor — so even when Jacobson/Karels computes a ~100 ms RTO from the SRTT/RTTVAR samples, the value gets rounded up to 500 ms minimum. A single dropped segment then waits ~500 ms–1 s for the first retransmit to fire (one RTO tick plus the time to actually clear the air on the next opportunity), and if *that* gets missed too, the backoff doubles and the next retransmit lands ~2 s after the original send. The fix isn't fewer retries — it's letting the kernel use the RTO the estimator actually computed, instead of the rounded-up tick floor.
2. **Long retransmit purgatory.** ESP-IDF default `TCP_MAXRTX=12` combined with the standard exponential-backoff-then-cap-at-128× table means a *stuck* socket sits in retransmit hell for **~6 minutes worst case** before lwIP gives up and notifies the app. For a real-time control plane, you want to know the socket is dead in seconds, not minutes, so you can reconnect. This is a separate concern from the RTO-floor issue above; it bites if the gateway actually disappears (loses power, crashes) rather than just dropping a packet.

## Applied changes

Lines from [`sdkconfig.defaults`](sdkconfig.defaults):

| Symbol | Value | ESP-IDF default | Rationale |
|---|---|---|---|
| `CONFIG_LWIP_TCP_TMR_INTERVAL` | `100` | `250` | Slow tick becomes 200 ms (was 500 ms). Both the RTO floor and the rounding granularity drop 5×. Converged RTO on a typical WiFi-to-AP link should now sit around 200–400 ms instead of 500–1000 ms. Cost: marginally more CPU on the lwIP timer task (negligible for a 2-core ESP32). |
| `CONFIG_LWIP_TCP_RTO_TIME` | `750` | `1500` | Bootstrap RTO used until enough samples accumulate to compute SRTT/RTTVAR. Mostly cosmetic — the estimator takes over within a few RTTs and the initial value stops mattering. Reduced anyway because we're explicitly biasing toward fast retransmit, not robustness against spurious retransmits. |
| `CONFIG_LWIP_TCP_MAXRTX` | `7` | `12` | Caps total backoff on a stuck connection. With the converged tick this gives 200ms + 400ms + 800ms + 1600ms + 3200ms + 6400ms + 12800ms ≈ 25 s before lwIP errors out and the app reconnects. Originally proposed as `4` or `5`; we landed at `7` as a compromise between fast failure detection and tolerance for long real WiFi outages. |
| `CONFIG_LWIP_IPV6` | `n` | `y` | Unrelated to RTO/jitter. Unused on this product, and disabling shrinks the stack and removes a chunk of unused code paths. Drop in here because we were already in the LWIP submenu. |

Pasted at the end of `sdkconfig.defaults`. To regenerate from menuconfig, use the `D` (set as default) shortcut — see the file's leading comment.

## Considered but not yet applied

These came up in the original discussion and are reasonable to revisit when the next class of symptom shows up. None of them are wrong; they just weren't load-bearing for the current production issue.

- **`LWIP_MAX_ACTIVE_TCP` (default 16).** Matters for the *gateway-side* (Python on Linux — irrelevant) and only matters on the MUR if a MUR ever holds many concurrent TCP connections. A MUR has one outbound TCP to the gateway plus the HTTP server's accepted connections. 16 has been fine. **Bump to 32 or 48 if** you ever see "too many open files" or `ENOBUFS` in the HTTP path.
- **SACK (`LWIP_TCP_SACK_OUT` / `LWIP_TCP_MAX_SACK_NUM`).** Selective ACKs save a roundtrip on the rare multi-segment retransmit. Cost is ~32 bytes per PCB. **Worth flipping on** the next time we touch TCP defaults; no downside and a small wins on lossy WiFi. Skipped this round only because it didn't address the observed symptom directly.
- **`LWIP_TCP_MSL` (default 60000 → 120 s TIME_WAIT).** Only matters on a fast-churn-of-connections scenario. Murmura's control links are long-lived, so it's not biting. **Drop to `15000`** if you ever see PCB exhaustion after frequent reconnects.
- **LWIP IRAM optimization (`LWIP_IRAM_OPTIMIZATION` and `LWIP_TCPIP_RECVMBOX_IRAM_OPTIMIZATION` family).** Moves hot lwIP code into IRAM for measurable RX/TX throughput and latency wins. Cost: ~10 KB IRAM for the general one, ~17 KB for the TCP-specific one. **Worth measuring** with a packet capture before/after. Skipped this round because IRAM is also where audio code lives and we didn't want to risk a tradeoff without measuring.
- **`LWIP_TCPIP_CORE_LOCKING`.** Lets app tasks call lwIP APIs directly without round-tripping through the `tcpip_thread` mailbox. A latency win for a control plane that issues lots of small writes. **Worth flipping on** if you see contention or want to drop a few hundred microseconds off socket-write latency. Skipped this round because it's a behavioral change that's harder to undo than the RTO knobs.
- **`LWIP_TCPIP_TASK_PRIO` (default 18).** Fine where it is. Watch for any task running at priority ≥18 that does heavy work — that's how `tcp_tmr` gets starved and the RTO clock skews late. The audio control task and listener task are below 18; this hasn't bitten us.

## Runtime socket options (not in menuconfig)

These are per-socket calls in firmware. Some are **not yet applied** — call them out the next time someone touches [`mur_listener.c`](main/mur_listener.c).

Currently set on the gateway socket ([mur_listener.c:151](main/mur_listener.c#L151), [:162](main/mur_listener.c#L162)):
- `SO_SNDTIMEO = 5 s` — caps a stuck `send()` so the listener task doesn't hang.
- `SO_RCVTIMEO = 200 ms` — gives the recv loop a tick frequency for resubscribe checks.

**Not applied but recommended for a control plane carrying small messages:**

```c
// Kill Nagle — small protocol writes shouldn't sit in the send queue waiting
// for more data. Set immediately after connect().
int yes = 1;
setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof(yes));

// Liveness — independent of whether app traffic happens to be flowing.
// With our converged RTO behavior these values give 5–11 s dead-peer
// detection. Replace the existing socket-timeout-only hack with this.
int keepalive = 1;
int idle = 5;     // seconds of idle before first probe
int intvl = 2;    // seconds between probes
int cnt = 3;      // probes before declaring dead
setsockopt(sock, SOL_SOCKET, SO_KEEPALIVE, &keepalive, sizeof(keepalive));
setsockopt(sock, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
setsockopt(sock, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
setsockopt(sock, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof(cnt));
```

**Why not yet applied:** the `MAXRTX=7` change above gives a ~25 s socket-error trigger on a stuck send — *if there's outgoing traffic*. If the gateway dies during a quiet period (no triggers, no scene polls), the MUR won't notice until the next send. KEEPALIVE closes that gap. Worth adding the next time we touch reconnect logic.

## How to verify a change worked

The TCP knobs above are not directly observable from MUR logs — they're internal to lwIP. Use one of:

1. **Packet capture on the gateway side.** Wireshark on the gateway's NIC, filter for the MUR's IP. Look at retransmit timing in a window where you provoke a stall (e.g. briefly block the MUR's MAC at the AP). Compare against the previous-config capture.
2. **Synthetic stall test.** From the MUR side, ARP-poison the AP entry briefly (or pull the AP power for 1–3 s) and watch how long the next `send()` takes to fail or recover. With the previous defaults you'd expect 6+ minutes worst-case before `send()` returns an error; with `MAXRTX=7` and the tighter tick you should be in the 25-second range.
3. **End-to-end sync verification.** Run the SYNC_DESIGN.md "validate on a new install" steps before and after the tuning. Cross-device jitter should hold steady; the change you should see is fewer `late event ...` warnings on the MURs in the gateway logs during sustained AP load.

## Things this doc isn't claiming

- **No claim that this fixes a fundamentally bad RF environment.** If the underlying RTT is genuinely jittery (link near the noise floor, real interference) the SRTT/RTTVAR estimator will sit higher and the computed RTO with it; the tighter tick helps the rounding, not the underlying samples. If you're seeing RTO above ~400 ms on a converged link in a clean environment, the issue is upstream of TCP and no menuconfig knob is going to save you.
- **The change to `CONFIG_LWIP_TCP_RTO_TIME=750` is not load-bearing.** It's a starting RTO; the estimator takes over fast. We could leave it at 1500 and you'd never see a difference in steady-state behavior. Lowered for the very-first-packet bias only.

## See also

- [SYNC_DESIGN.md](SYNC_DESIGN.md) — *why* TCP latency matters in this product (sync feature deadlines)
- [main/mur_listener.c](main/mur_listener.c) — current per-socket options
- ESP-IDF docs: Component config → LWIP → TCP submenu has Help text on every symbol above
- lwIP: `lwip/src/core/tcp_in.c`, `tcp_out.c` for the actual RTO computation; `LWIP_TCP_RTO_TIME` is referenced in `tcp_alloc()`
