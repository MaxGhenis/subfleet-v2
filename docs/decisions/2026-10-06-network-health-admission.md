# Network-health-aware admission (proposal, 2026-10-06, revision 1)

Proposes C-6.17 and amends C-4.5, C-9.5 and C-18.1. Nothing here is built yet.
The evidence comes from the 2026-10-03 and 2026-10-06 bad-network episodes on the
operator's Mac, a controlled fault-injection test of both CLIs, and a read of
both clients' retry code and docs. The full report is
`~/reviews/codex-vs-claude-network-2026-10-03/REPORT.md`.

## What was observed

On 2026-10-06 the Mac ran on a phone hotspot. It rebooted at 18:29:13Z, and
Subfleet relaunched about 30 attempts between 18:32 and 18:34Z. From about 18:48Z
the hotspot stopped completing most new connections, on IPv4 and IPv6 alike:
- per-minute TCP+TLS probes to api.anthropic.com, chatgpt.com and auth.openai.com
  mostly read 0 of 3;
- 129 to 153 sockets sat in SYN_SENT, 62 in FIN_WAIT_1;
- ping mostly answered, but slowly (up to 1.7 s).

The link came back in bursts (19:02Z, 19:10Z) and recovered by about 19:30Z.

The two providers' lanes came through very differently:

| | Claude lanes | Codex lanes |
|---|---|---|
| Attempts ended by the network | 18 `transient`, 18:46–19:34Z (ECONNREFUSED "Connection refused — a firewall or proxy may be blocking it", ECONNRESET, "Request timed out") | 0 |
| Retries of those jobs | the 60 s same-lane retries (C-4.5) failed again in the same outage, e.g. `pe9860-r2o/a2`, `la-gr-9916-review/a2` | n/a |
| What the client did | ended the turn with `API Error: …` after its retry budget | logged `Reconnecting... 1/5 … 5/5`, then "Falling back from WebSockets to HTTPS transport", then repeated "Reconnecting... waiting for network", and carried on when the link returned |

The desktop sessions match the Claude lanes: 24 `API Error` records in 23
sessions between 18:40 and 19:35Z.

The single Codex attempt recorded `transient` in that window
(`20261006-121957-pr148-review/a1`) was not a network failure. It recovered from
the outage, then ended on "This content was flagged for possible cybersecurity
risk". See "Classifier defects" below.

## Why the two clients differ (read in code and docs)

- **Codex 0.159.0** has `features.unbounded_connection_retries`, which is stable
  and on by default (`codex-rs/features/src/lib.rs:1292`). A sampling request
  that fails with `ConnectionFailed` (DNS, refused, TLS, connect timeout on the
  HTTP path) is retried after 5, 10, 20, 40 and 60 s, then every 60 s, with no
  limit. Those retries do not spend the five-retry stream budget
  (`core/src/responses_retry.rs:71`). The WebSocket path falls back to
  HTTPS for the rest of the session after five failed stream retries
  (`core/src/client.rs:2271`).
- **Claude Code 2.1.284** retries a failed request up to `CLAUDE_CODE_MAX_RETRIES`
  times: default 10, capped at 15. The backoff is 500 ms doubling, capped at
  32 s, plus 0–25% jitter: about three minutes in all. A request that gets no
  response headers is retried only once (docs: errors → Automatic retries, No
  response from API). After that the turn ends with `API Error: …`.
  `CLAUDE_CODE_RETRY_WATCHDOG=1` (2.1.199 and later) raises the default to 300
  retries, "roughly three hours of backoff". It also retries 429/529 capacity
  errors indefinitely and waits out a usage-limit window that carries a reset
  time (docs: env-vars, `CLAUDE_CODE_RETRY_WATCHDOG`).

So a Claude attempt survives an outage of up to about three minutes. A Codex
attempt survives any outage, as long as Subfleet's wall limit allows. Subfleet
then makes it worse:
- it retries a network-failed Claude attempt 60 s later on the same lane (C-4.5);
- every lane shares one network, so a second `transient` excludes the lane for
  no reason;
- `max_attempts` 3 is spent within minutes of an outage that lasts 25.

## Controlled test (2026-10-06)

Both CLIs ran through a user-space CONNECT proxy, with nothing changed on the
host. Each arm ran the same task: read a file with a tool, then write about 150
words. Codex: gpt-6-luna on a ChatGPT-subscription lane. Claude: Haiku 4.5 on a
lane token, CLI 2.1.284. Results are in the report.
- **Short faults: both clients recover.** These were 67% of new connects refused,
  50% of tunnels reset mid-stream, 50% black-holed for 15 s, 30% silently
  stalled, and +1.5 s latency. Counting a no-fault proxy baseline, 23 of 24
  Codex runs and 24 of 24 Claude runs succeeded (4 runs per arm). The Codex failure came under +1.5 s latency: app-server workspace
  routing discovery has a fixed 15 s deadline, which it missed four times
  running.
- **Silent stalls cost Claude minutes.** A stalled stream cost Claude 6–7 minutes
  (byte watchdog 180 s, then the retry). It cost Codex under 2 minutes.
- **Sustained outages separate them.** These arms put the model host in a total
  outage starting 4 s into the run.

  | Outage | Claude Code (default) | Claude + `CLAUDE_CODE_RETRY_WATCHDOG=1` | Codex |
  |---|---|---|---|
  | 5 min, connects refused | 0/2, both died at 184 s after 10/10 retries | 2/2 (339 s, 362 s) | 2/2 (361 s, 361 s) |
  | 5 min, silent (connects hang, live tunnels go quiet) | 2/2 | not run | 2/2 |
  | 15 min, silent | 1/2: one died at 785 s, "No response from API" (180 s + one ~600 s retry); the other survived only because its later connects sat in the proxy until the outage ended | 2/2 (912 s, 914 s) | 2/2 (912 s, 915 s) |

## Proposal

### C-6.17 Network-health admission (new, off as shipped)

1. **A sampler like C-6.15's.**
   - Policy section `net_health`, defaults
     `{"enabled": false, "act": false, "sample_s": 30, "tries": 3, "timeout_s": 5}`
     plus per-provider endpoints:
     `{"claude": ["api.anthropic.com:443"], "codex": ["chatgpt.com:443", "auth.openai.com:443"]}`.
   - While enabled, the control loop takes one sample per `sample_s`. For each
     endpoint and for each address family that has both a local address and a DNS
     record, the sample makes `tries` TCP connects, each followed by a TLS
     handshake, each with a `timeout_s` deadline.
   - It runs on its own worker key (`net-health`), never in a store transaction
     and never on admission's worker.
   - It sends no HTTP request and no credential, so it spends nothing on any
     account.
   - The reading per provider is the success ratio and median handshake time per
     family, plus the best family's ratio.
   - A reading older than four sample intervals is none. As in C-6.15, the
     monotonic clock stops while the Mac sleeps, so the first sample after a wake
     is taken before the next admission pass uses one.
2. **Passive evidence.** It joins the active probe because it reflects the
   clients' real traffic and costs nothing. The stream readers already tail every
   live attempt. A provider is also marked as failing when, within the last
   `2 × sample_s`, at least two of its live attempts emitted a connection-class
   retry. For Claude that is a `system/api_retry` event whose error is a
   connection error. For Codex it is a `Reconnecting...` line whose cause is
   connect/DNS/TLS/timeout, or "waiting for network".
3. **State per provider, with hysteresis.**
   - States are `healthy`, `degraded` and `down`.
   - `down`: the best family's ratio is below 0.2 in two consecutive samples, or
     the passive rule fires.
   - `degraded`: below 0.67 in two consecutive samples.
   - Back to `healthy`: at least 0.9 in three consecutive samples, with the
     passive rule quiet.
   - At most one transition per sample.
   - An `unknown` state (no reading, or a stale one) holds nothing (fail-open).
4. **Admission** (only when `act` is true).
   - While a provider is `down`, `scheduler.evaluate` rejects each of its lanes
     with `network-down`, so the chain walk promotes as it does for a closure:
     upward only, never to a model outside the job's chain. A job whose whole
     chain is down waits with hold `network` and `next_check_at` no later than
     the next sample.
   - While a provider is `degraded`, at most `net_health.degraded_starts` (default
     2) new attempts start on it per sample interval. This is a slow start, so
     that a reboot or wake into a bad network does not launch thirty attempts at
     once, as happened at 18:32Z.
   - Live attempts are never killed for network health. Codex waits by itself,
     and Claude's own budget either recovers or ends the attempt.
   - The timers' probes (C-18.1) are held while their provider is `down`.
5. **Retry accounting** (amends C-4.5 and C-9.5).
   - A `transient` attempt whose terminal evidence is connection-class, and that
     ended while its provider was `degraded` or `down`, is `transient/network`.
   - It does not count toward the second-transient lane exclusion: every lane
     shares the network.
   - Its retry waits on hold `network` until the provider is `healthy`, instead
     of after 60 s, and keeps the C-4.5 same-lane pin, so the native session
     resumes.
   - Such attempts draw on a separate budget, `net_health.max_network_retries`
     (default 6), and never on `max_attempts`. The job's `max_wall_s` still
     bounds everything.
6. **Observability.**
   - `subfleet status` prints one `network:` line per provider: state, ratio and
     p50 per family, and the age of the reading.
   - `why` reports `network-down` and `network`. These are expected queueing
     under C-6.11's log line.
   - Events `net.sample` and `net.state`; one `daemon.log` line per state change.

Invariants, each a property test, in the style of C-6.15:
- **Off has no effect.** With `enabled` false, `act` false, or no section, every
  decision equals the decision without C-6.17, whatever the readings.
- **Placement only moves up the chain.** Under any state a job is placed where it
  would have been, on a later model of its own chain, or nowhere. It is never
  placed on a model outside its chain.
- **Monotonic.** A worse state for a provider never places what a better state
  withheld, and `down` ⊇ `degraded` ⊇ `healthy` in what each withholds.
- **Missing evidence holds nothing.** A missing or stale reading, or a provider
  with no configured endpoint, holds nothing.
- **Bounded probing.** A sample makes at most
  endpoints × families × `tries` connections, and samples begin at most once per
  `sample_s`.
- **Bounded network retries.** They never exceed `max_network_retries` and
  never outlive `max_wall_s`.

### Classifier defects (fix first, separately)

Network health and the history report both need the terminal cause, and today's
classifier mislabels it.

1. **Codex: the cybersecurity flag is not `content-filter`.** `CONTENT_RE`
   (`adapters/codex.py:51`) does not match "This content was flagged for possible
   cybersecurity risk". When such a turn also logged a recovered
   `Reconnecting...` line, `TRANSIENT_RE` labels it `transient`, and C-4.5 then
   retries a request the filter will flag again. Seen on
   `20261006-111456-hub-rev-receipt-86/a1` and
   `20261006-121957-pr148-review/a1`.
2. **Codex: non-terminal lines count as failure evidence.** Recovered
   `Reconnecting... k/5` lines, `codex_models_manager … failed to refresh
   available models: request timed out` (a fixed 5 s side request,
   `codex-api/src/endpoint/models.rs:33`) and `rmcp::transport::worker … Transport
   channel closed` all match `TRANSIENT_RE`. Today they turn any otherwise
   unexplained failure, and every admission probe that lacks a deliverable, into
   `transient`. The 2026-10-03 probe `transient`s quoted these lines.
   Fix: classify on the `turn.failed` event when there is one, and never on these
   side-channel lines.
3. **Claude: connection errors read as `server_error`.** On a connection failure
   Claude Code itself stamps the closed `error` enum: `server_error` on the
   final assistant frame and `unknown` on `system/api_retry` frames. Observed on
   2026-10-06 for ECONNREFUSED, ECONNRESET and timeouts; the enum is listed in
   `adapters/claude_stream.py:46`. Subfleet records these as "provider error kind
   server_error", so an outage looks like a 5xx.
   Fix: sub-classify on the `API Error:` text Claude Code documents for
   connection failures (docs: errors → Unable to connect to API, No response
   from API). Those texts are "Connection refused", "Connection dropped",
   "Can't reach the API server", "No internet route", "Request timed out", "No
   response from API" and "Connection lost while your computer was asleep".
   Record them as `transient` with `evidence.network = true`.

### Claude lane settings (proposal, needs the outage-arm result)

Codex already waits out outages. Claude lanes would match it with
`CLAUDE_CODE_RETRY_WATCHDOG=1` plus an explicit bound such as
`CLAUDE_CODE_MAX_RETRIES=60` (about 35 minutes at the 32 s cap). But under the
watchdog a lane that reaches its usage limit "waits out the remaining window"
instead of exiting, and Subfleet's `limited` failover depends on that exit. So
the adapter must end the attempt itself: on the first `system/api_retry` with
`error_status` 429 whose rate-limit event says `rejected`, kill it and record
`limited` with the reset time. Until that exists, the watchdog stays off on
lanes.

## Rollout

1. Classifier fixes (separate PR, with tests drawn from the cited attempts).
2. C-6.17 with `enabled: true, act: false`: sampler, status, events only. Run it
   through the next bad-network episode and compare its states with attempt
   outcomes before turning `act` on.
3. `act: true`.
4. Retry accounting (5).
5. Claude lane watchdog with the adapter's own limit exit.
