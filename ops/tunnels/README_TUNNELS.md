# NeoAG public access: supervised Cloudflare Quick Tunnels

Operator entry point:

```
./tunnel-status.sh            # current public URLs + health (exit 2 = not all healthy)
./tunnel-status.sh --json     # raw state file
```

## Why this exists (2026-11-17 incident, root cause)

A Cloudflare Quick Tunnel (`cloudflared --url ...`) gets its random
`*.trycloudflare.com` hostname from a server-side registration that can be
reclaimed by Cloudflare while cloudflared keeps running. When that happens
the process logs `Register tunnel error from server side: Unauthorized:
Tunnel not found` forever: it never requests a new hostname and never
exits, so process-only watchdogs (and systemd's `Restart=`) cannot see
anything wrong. On 2026-11-17 the NeoAG Web hostname expired this way at
07:18 UTC; the process lived on, the URL stopped resolving, and public
access stayed broken until the 2026-11-18 rebuild.

Recovery requires a fresh cloudflared process, which always rotates to a
new random hostname. Quick Tunnels therefore provide NO permanent
hostname and NO uptime guarantee - this tooling's job is to notice fast,
recover automatically, verify the new URL for real, and tell you what it
is via `tunnel-status.sh`.

## Topology (final, 2026-11-18 evening - single tunnel)

```
browser --HTTPS--> [quick tunnel] cloudflared (neoag-tunnel-web.service)
                       |
                       +--> NeoAG Web  http://127.0.0.1:8712   (Basic auth ON)
                                |
                                +--> CodeBuddy Gateway http://127.0.0.1:7890
                                     (loopback only, backend/codebuddy_client.py)
```

ONE public entry point. The second (gateway-facing) quick tunnel that
existed on 2026-11-18 morning was RETIRED the same day after a
dependency audit: the demo backend reaches the gateway over loopback,
the frontend makes no external calls, no script/cron/config/systemd
unit on the host references a gateway public hostname, the gateway's
own settings carry no tunnel configuration, its public URL logged zero
incoming requests, and the operator uses CodeBuddy via SSH/tmux (local
PTY sessions that never touch :7890) plus NeoAG Web. With no client
left, that tunnel was disabled and uninstalled while the localhost
gateway process was left untouched (verified same pid before/after).
`units/neoag-tunnel-gw.service` remains here for reference with
re-adoption instructions; if ever re-adopted, note that a plain probe
of the gateway's API-guarded endpoints (e.g. `GET /health` without
`x-codebuddy-request`) answers 403 - use `GET /` (login page, 200) as
the app probe, and remember the `--http-host-header localhost:7890`
requirement.

Nothing else binds any new port; cloudflared is outbound-only (TCP/7844
to the Cloudflare edge, http2 because UDP is blocked host-wide).

## Components

| path | role |
|---|---|
| `units/neoag-tunnel-web.service` | cloudflared -> 8712, foreground, `Restart=always`, burst 5/600 s |
| `units/neoag-tunnel-gw.service`  | RETIRED 2026-11-18 (see Topology) - reference only, not installed, not monitored |
| `units/neoag-tunnel-health.{service,timer}` | one recovery cycle per minute |
| `tunnelctl.py`                   | all probe/recovery/status logic |
| `tunnel_health.sh`               | ExecStart wrapper for the timer |
| `tunnel-status.sh`               | operator report (reads only the state file) |
| `install_tunnels.sh`             | idempotent installer; `--uninstall` removes everything |

Runtime state lives OUTSIDE the repo (URLs are rotating, semi-secret):

* `/data1/neoag/agents/remote_access/tunnels/state.json` - 0600, atomic,
  the single authority for "what is the current URL"
* `web.log` - cloudflared output, 0600 (`gw.log` is a frozen artifact of
  the retired gateway tunnel)

## Layered health checks (recover only the failing layer)

1. **unit**: systemd `ActiveState=active` with a `MainPID`.
2. **app**: the local upstream answers HTTP at all. ANY status code
   proves the app process is serving (a 401 from the Basic-auth-protected
   demo is proof, not an outage); only a transport failure counts as
   app-down, because rotating a public URL cannot fix a broken app.
3. **network**: TCP/7844 to Cloudflare anycast edge IPs - this host's
   firewall opens exactly that transport, so it is the relevant probe.
   While the host has no outbound, NO tunnel is rotated: a local network
   blip must not burn a public URL.
4. **connection**: the unit's cloudflared actually holds an established
   TCP/7844 connection to the edge (`ss`, not `ps`).
5. **public**: the published URL resolves (DNS) and returns an expected
   code through Cloudflare (`401` for the protected web route is a PASS;
   edge codes 5xx/530 are a fail).

Decision rules:

* `Tunnel not found` seen in a fresh log tail -> conclusive, act now.
* otherwise public failures need `TUNNEL_HEALTH_FAIL_THRESHOLD`
  consecutive cycles (default 3 x 1 min) before replacement; missing edge
  connections need 2 - temporary interruptions expire on their own.
* MainPID changed without the supervisor asking (systemd `Restart=`, manual
  restart) -> the old hostname died with the process: one failed probe
  retires the stale URL immediately, no streak needed.
* upstream down or host network down -> status is recorded
  (`upstream-down` / `network-down`) and NO URL is rotated: rotating is
  pointless while the origin or the network itself is down.

Restart backoff: exponential per consecutive recovery (2 min, 4, ...
capped 1 h), cleared once the tunnel is healthy. systemd itself stops
after 5 starts in 600 s (`StartLimitBurst`); the supervisor then owns the
recovery pace via `reset-failed` + restart, which matters because
Cloudflare rate-limits quick-tunnel creation (HTTP 429) from one IP.

## The URL handoff contract

* A URL is marked `active` ONLY after a real end-to-end probe
  (`tunnelctl.py public_reach`) succeeds. New quick hostnames can take
  ~1 min to resolve through this host's resolvers; unverified candidates
  sit in `url_state=pending` and are retried by later cycles.
* When a URL is replaced, the old one moves to `history` with
  `expired_at`. `tunnel-status.sh` never presents an expired URL as
  current.

## Operations

* Current URL / health: `./tunnel-status.sh`
* Unit status: `systemctl --user status neoag-tunnel-web.service neoag-tunnel-health.timer`
* Supervisor reasoning: `journalctl --user -u neoag-tunnel-health.service`
* Manual cycle now (instead of waiting for the timer):
  `systemctl --user start neoag-tunnel-health.service`
* Deliberate restart (URL rotates):
  `systemctl --user restart neoag-tunnel-web.service && systemctl --user start neoag-tunnel-health.service`
* Boot persistence: user-level systemd + `loginctl enable-linger caojingqi`
  (done 2026-11-18; without linger, user units stop when the last login
  session closes and do not come back after a reboot).
* Log growth: truncate `web.log` any time; the supervisor
  reads it lazily and tolerates truncation.

## Failure-mode quick table

| symptom | meaning | action |
|---|---|---|
| status `healthy` | all five layers pass | none |
| `url-pending` | tunnel restarting, new URL not verified yet | wait a cycle |
| `upstream-down` | local app/gateway not answering | fix the APP, not the tunnel; the demo has its own `watchdog.sh`, restarting the CodeBuddy gateway is an operator decision |
| `network-down` | host lost outbound to Cloudflare edge | network, not tunnel |
| `recovering` | restart issued, inside cooldown | wait |
| status stuck / all URLs dead for long | Cloudflare quick-tunnel outage or quota | `journalctl --user -u neoag-tunnel-health.service` shows each attempt; consider a Named Tunnel |

## Rollback

`./install_tunnels.sh --uninstall` stops, disables and removes the units
(state/logs stay). To return to the pre-2026-11-18 manual scheme:
`MODE=quick CF_PROTOCOL=http2 TUNNEL_TARGET=http://127.0.0.1:8712
TUNNEL_SUFFIX=-demo /data1/neoag/agents/remote_access/start_tunnel.sh`
(and the same without `TUNNEL_SUFFIX` and with
`HOST_HEADER=localhost:7890` for the gateway). The legacy scripts are
kept but are no longer maintained - they contain none of the detection or
state logic that fixed this incident. The supervisor touches only the two
cloudflared units: NeoAG Web, the watchdog, the gateway and all research
state are outside its scope by design.

## Remaining limitations (Quick Tunnel, not our tooling)

* Hostnames rotate on every fresh registration - there is no permanent
  URL. For a stable hostname you need a Named Tunnel with a Cloudflare
  account (`start_tunnel.sh MODE=named` was written for that future).
* No uptime guarantee from Cloudflare; quick-tunnel creation is
  rate-limited per source IP (HTTP 429), so a storm of failures lengthens
  recovery. The backoff above keeps us on the right side of it.
* Hostname reclamation by the edge is silent (the process only learns via
  register errors); worst-case detection here is one health cycle.
* The gateway chat UI still relies on knowing its current URL; anything
  bookmarked must be refreshed from `tunnel-status.sh` after a recovery.
