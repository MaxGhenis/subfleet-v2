# Network-health-aware admission (proposal, 2026-10-06, revision 3)

Proposes C-6.17. Amends C-4.1, C-4.5, C-6.9, C-6.11, C-6.12 and C-9.5.
C-18.1 continues unchanged. No implementation or lane setting changes are
part of this revision. All independent-review findings are valid. Their
dispositions for both review rounds appear at the end.
[R1-review-pr150.md][review]; [review-r2.md][review2]

Evidence files live under
`~/reviews/codex-vs-claude-network-2026-10-03/`. Links identify the source
file for measured counts, times and client defaults. Proposed numerical
choices cite the evidence or review requirement motivating them and give
their rationale below. Those sources do not establish the new constants
as measurements or existing defaults. Release compatibility quotes use
`origin/release/217`, pinned at `581b07b01f1e`, rather than this older
checkout's code. Clause, issue, revision and source-line numbers are identifiers.

[review]: /Users/maxghenis/reviews/codex-vs-claude-network-2026-10-03/out/R1-review-pr150.md
[history]: /Users/maxghenis/reviews/codex-vs-claude-network-2026-10-03/out/A-subfleet-history.md
[desktop]: /Users/maxghenis/reviews/codex-vs-claude-network-2026-10-03/out/B-desktop-sessions.md
[mechanisms]: /Users/maxghenis/reviews/codex-vs-claude-network-2026-10-03/out/C-mechanisms.md
[report]: /Users/maxghenis/reviews/codex-vs-claude-network-2026-10-03/REPORT.md
[notes]: /Users/maxghenis/reviews/codex-vs-claude-network-2026-10-03/notes-live-20261006.md
[monitor]: /Users/maxghenis/reviews/codex-vs-claude-network-2026-10-03/data/monitor-hotspot-20261006.jsonl
[replay]: /Users/maxghenis/reviews/codex-vs-claude-network-2026-10-03/tools/replay_states.py
[review2]: /Users/maxghenis/reviews/subfleet-2110/network-health/review-r2.md
[release-daemon]: https://github.com/MaxGhenis/subfleet-v2/blob/581b07b01f1e590a640da35e4eb79b1de14e3c41/subfleet/daemon.py
[release-policy]: https://github.com/MaxGhenis/subfleet-v2/blob/581b07b01f1e590a640da35e4eb79b1de14e3c41/subfleet/default_policy.json
[release-contract]: https://github.com/MaxGhenis/subfleet-v2/blob/581b07b01f1e590a640da35e4eb79b1de14e3c41/docs/acceptance-contract.md

## What was observed

The Mac rebooted at 18:29:13Z on the hotspot. The launch cohort at
18:32–18:35Z contained 53 lane attempts: 22 Claude and 31 Codex. Of those,
18 Claude attempts died in the 18:46–19:34Z outage. Codex lost none of its
31 attempts to the network. These are the author's corrected cohort counts.
They are not a controlled provider comparison.
[REPORT.md, live comparison][report]; [R1-review-pr150.md, finding 2][review]

Nine additional Claude attempts died with DNS errors around 18:06Z while
the Mac slept, approximately 17:47–18:09Z. Sleep is a separate exposure.
The earlier history snapshot ends at 18:16:18Z. It cannot independently
establish the later outage totals.
[REPORT.md][report]; [A-subfleet-history.md, scope and B22][history]

Retract the desktop-session claim. Lane transcripts also live under
`~/.claude/projects`. Matching native session ids leaves two desktop errors
in one session during 18:40–19:35Z. The overlapping transcript scan was not
independent corroboration. The desktop audit excludes lane sessions and
separates parent continuation from recovery of a failed child.
[R1-review-pr150.md, finding 1][review]; [B-desktop-sessions.md][desktop]

The monitor was configured for one sample per minute. During failures,
completed samples were often two to three minutes apart. The 19:30:46Z
burst was not complete recovery: failed samples followed at 19:31:46Z,
19:34:46Z and 19:41:09Z. Real client runs succeeded from 19:36Z onward.
[monitor-hotspot-20261006.jsonl][monitor]; [R1-review-pr150.md, findings 2 and 7][review]

The reported socket census and reboot time are live notes, without separately
retained raw census or reboot output. The Codex content-filter attempt cited
in the old draft ended at 19:46:13Z, outside its desktop comparison window.
The Claude retry examples lasted tens of minutes. Rapid exhaustion of the
ordinary retry budget remains a possible fast-failure scenario, not an
observed timing conclusion from those examples.
[notes-live-20261006.md][notes]; [R1-review-pr150.md, finding 2][review]

## What the clients establish

Codex 0.159.0 enables unbounded connection retries by default. They apply to
qualifying external sampling requests with `ConnectionFailed`, with the
feature enabled and outside Bedrock. The separate waits are 5, 10, 20, 40
and 60 seconds, then 60 seconds repeatedly. Workspace discovery, remote
compaction, fatal errors and repeated stream failures retain finite paths.
WebSocket fallback and HTTP connection retries are different layers.
There is no universal outage-survival guarantee.
[C-mechanisms.md, Codex retry layers][mechanisms]

Claude Code 2.1.284 defaults to ten ordinary retries, capped at fifteen
without the watchdog. Its approximately three minutes of ordinary backoff
exclude request duration. The usual no-response path permits one retry,
with approximately three minutes then ten minutes of header waiting.
Certificate validation can fail immediately. Partial-output handling can
also end a turn before ordinary exhaustion.
[C-mechanisms.md, Claude retries and timeouts][mechanisms]

The controlled CONNECT-proxy tests used the same small tool task on both
clients. Across the short-fault arms and proxy baseline, Codex succeeded in
23 of 24 runs and Claude in 24 of 24. The Codex latency failure emitted
nine reconnection notifications across transports before a workspace-routing
timeout. Notifications do not count total requests. The routing deadline is
15 seconds. The separate models deadline is five seconds, defined at
`codex-rs/model-provider/src/models_endpoint.rs:43`.
[REPORT.md, controlled fault test][report]; [C-mechanisms.md, endpoint map][mechanisms]

| Controlled outage | Claude default | Claude watchdog | Codex | Source |
|---|---|---|---|---|
| Five minutes, refused connects | 0/2; both ended around 184 s | 2/2 | 2/2 | [REPORT.md][report] |
| Five minutes, silent | 2/2 | Not run | 2/2 | [REPORT.md][report] |
| Fifteen minutes, silent | 1/2; one ended around 785 s | 2/2 | 2/2 | [REPORT.md][report] |

Fault timing starts at proxy startup, four seconds before injection,
not at client launch. These results establish eligible recovery paths.
They do not establish a maximum survivable outage for either client.
[R1-review-pr150.md, findings 3 and 17][review]

Classifier defects (a)–(c) are outside this proposal and are fixed under #83: the audit found 11 of 67 Codex `transient` attempts were terminal content filters, plus 18 content-filter attempts recorded `unknown`. [A-subfleet-history.md, classifier audit][history]

## Hysteresis replay

Choose rule C from `tools/replay_states.py`. It pools the best-family
inference-endpoint counts over up to three samples. Its thresholds are
`down < 0.15`, `degraded < 0.5`, `down → degraded >= 0.3`, and
`degraded → healthy >= 0.67` for two consecutive pooled readings.
[replay_states.py, RULES][replay]

I reran the supplied script successfully with
`python3 ~/reviews/codex-vs-claude-network-2026-10-03/tools/replay_states.py`.
It reads the retained monitor only. The following are its rounded interval
totals. The post-recovery column starts at 19:36Z and runs through the
last reading at 23:17:43Z, not just the paired-client test interval.
[replay_states.py][replay]; [monitor-hotspot-20261006.jsonl][monitor]

| Rule | Provider | Down during failure window, min | Down after recovery, min | Degraded after recovery, min | Source |
|---|---|---:|---:|---:|---|
| A, literal script baseline | Claude | 48 | 221 | 0 | [replay_states.py][replay] |
| A, literal script baseline | Codex | 48 | 221 | 0 | [replay_states.py][replay] |
| B | Claude | 32 | 26 | 71 | [replay_states.py][replay] |
| B | Codex | 36 | 25 | 64 | [replay_states.py][replay] |
| C, selected | Claude | 26 | 24 | 70 | [replay_states.py][replay] |
| C, selected | Codex | 24 | 15 | 62 | [replay_states.py][replay] |

The supplied old-rule summary differs from this rerun. Literal rule A sets
`up_to_degraded=1.01` and has no direct `down → healthy` branch, so it
cannot leave `down`. Its printed baseline is not an exact implementation
of the prose in the first draft. Rule C's reproduced recovery totals are
the calibration used here. [replay_states.py][replay]

Rule C trades less outage coverage for shorter recovery holds. These totals
are proxy scores, not measured unnecessary job waits. The script holds each
reading until the next one, including long gaps. It omits freshness expiry,
sleep invalidation, passive evidence, proxy parity and queue decisions.
It uses the inference endpoint alone, not pooled authentication probes.
The proposed sampler has a different cadence. Shadow validation must compare
against timestamped real traffic and terminal causes.
[replay_states.py][replay]; [R1-review-pr150.md, findings 7 and 18][review]

## Release compatibility

These are verbatim excerpts from `origin/release/217` at the pinned commit.
The quotes establish the behavior the new clause must preserve. Review
sources: [review-r2.md, findings 1–4][review2];
[R1-review-pr150.md, findings 4, 9, 10 and 11][review].

### Lane-fault credit

[subfleet/daemon.py:6495–6505][release-daemon]:

```python
            moved = self._uncharged(tx, job["job_id"], before=a["seq"])
            fault = None if lost or cancel or moved else self._lane_fault(job, a, outcome, checkpoint,
                                                                           salvage_artifacts, salvage_evidence)
            # C-23.44: a job that already moved on from one auth-dead lane and meets
            # another is itself the common factor. It ends, and this lane stays
            # enabled for the next attempt there to judge.
            again = not lost and outcome.cls == OutcomeClass.AUTH_DEAD and moved > 0
            retry = (not cancel and (fault is not None or self._attempts_left(tx, job, a) and
                     ((lost and job["sandbox"] == "read-only") or
                      (outcome.cls == OutcomeClass.LIMITED and not job["pinned_lane"]) or
                      (outcome.cls == OutcomeClass.TRANSIENT and not (job["pinned_lane"] and previous_transient)))))
```

[subfleet/daemon.py:6515–6516][release-daemon]:

```python
            if fault is not None:
                evidence["lane_fault"] = fault
```

[subfleet/daemon.py:6599–6613][release-daemon]:

```python
        if outcome.cls != OutcomeClass.AUTH_DEAD or job["pinned_lane"] or job["kind"] == "turn":
            return None
        answered = (outcome.evidence or {}).get("model_answered")
        fault = {"class": outcome.cls.value, "lane_id": a["lane_id"], "seq": a["seq"], "model_answered": answered}
        if job["sandbox"] == "read-only":
            return {**fault, "workspace": "read-only"}
        if answered is not False:
            # A writable attempt whose model answered may have pushed, commented or
            # written outside its worktree, which no tree shows; where the adapter
            # cannot say, it is taken to have.
            return None
        baseline = json.loads(a["evidence_json"] or "{}").get("baseline_commit") or job["workdir_head"]
        if salvage_artifacts or salvage_evidence or not checkpoint or checkpoint != baseline:
            return None
        return {**fault, "workspace": "unchanged", "head": checkpoint}
```

[subfleet/daemon.py:6615–6627][release-daemon]:

```python
    @staticmethod
    def _uncharged(conn, job_id: str, *, before: int) -> int:
        """C-4.5: the job's attempts before `before` that were lane faults (at most
        one: a job moves on once), which `max_attempts` does not count."""
        return sum(1 for (data,) in conn.execute(
            "SELECT evidence_json FROM attempts WHERE job_id=? AND seq<? AND outcome_class='auth-dead'",
            (job_id, before)) if _lane_fault_of(data))

    def _attempts_left(self, conn, job: dict, a: dict) -> bool:
        """C-4.5: may the job have an attempt after `a`, which is no lane fault?
        `max_attempts` counts every attempt but the one lane fault a job may have
        had before it: that attempt ran nothing (a writable job's), or only read."""
        return a["seq"] - self._uncharged(conn, job["job_id"], before=a["seq"]) < job["max_attempts"]
```

[subfleet/daemon.py:224–231][release-daemon]:

```python
def _lane_fault_of(evidence_json: str | None) -> dict | None:
    """C-4.5: the lane fault an attempt's evidence records (`Daemon._lane_fault`), or None."""
    try:
        evidence = json.loads(evidence_json or "{}")
    except (TypeError, ValueError):
        return None
    fault = evidence.get("lane_fault") if isinstance(evidence, dict) else None
    return fault if isinstance(fault, dict) else None
```

The ledger must preserve those eligibility, evidence and credit predicates,
including the bypass of the ordinary budget check for a qualifying fault.
Migration must read the recorded credit rather than infer it anew.
[docs/acceptance-contract.md:179, C-4.5][release-contract]

### Fleet and wall defaults

[subfleet/default_policy.json:104][release-policy]:

```json
    "max_active_attempts": null,
```

[subfleet/default_policy.json:109][release-policy]:

```json
    "max_wall_s": 21600,
```

Release C-6.4 states: "Each of these six is null by default, which is no cap,
and the shipped policy says so".
[docs/acceptance-contract.md:203][release-contract]
The wall-default conversion to six hours also matches
[R1-review-pr150.md, finding 10][review].

### First-answer reader

[subfleet/daemon.py:1234–1242][release-daemon]:

```python
    def _read_answer(self, a: dict, adir: Path) -> None:
        """C-6.14: read what a live detached attempt's stream added since the last look,
        at most every `ANSWER_READ_INTERVAL_S` and `ANSWER_READ_CHUNK` bytes at a time,
        and stop at the first event its adapter says shows the model answering
        (`Adapter.model_answered`). Only the stream file, opened only as a regular
        file; nothing here raises."""
        aid = a["attempt_id"]
        if aid in self._attempt_answers:
            return
```

[subfleet/daemon.py:206–208][release-daemon]:

```python
ANSWER_READ_INTERVAL_S = 1.0
ANSWER_READ_CHUNK = 256 * 1024
ANSWER_LINE_MAX = 4 * 1024 * 1024
```

[subfleet/daemon.py:1248–1253][release-daemon]:

```python
                state = {"path": self._saved_launch(a).stdout_path, "offset": 0, "tail": b"", "skip": False,
                         "next": 0.0, "adapter": get_adapter(lane.provider) if lane else None}
                self._answer_reads[aid] = state
            if state["adapter"] is None or now < state["next"]:
                return
            state["next"] = now + ANSWER_READ_INTERVAL_S
```

[subfleet/daemon.py:1268–1292][release-daemon]:

```python
            with open_regular(state["path"]) as stream:
                size = os.fstat(stream.fileno()).st_size
                if size <= state["offset"]:
                    return
                stream.seek(state["offset"])
                data = stream.read(min(size - state["offset"], ANSWER_READ_CHUNK))
            state["offset"] += len(data)
            if state["skip"]:
                # The rest of a line longer than `ANSWER_LINE_MAX`, which is no event read whole.
                cut = data.find(b"\n")
                if cut < 0:
                    return
                data, state["skip"] = data[cut + 1:], False
            lines = (state["tail"] + data).split(b"\n")
            state["tail"] = lines.pop()
            if len(state["tail"]) > ANSWER_LINE_MAX:
                state["tail"], state["skip"] = b"", True
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if state["adapter"].model_answered(event):
                    self._record_answer(a["lane_id"], "stream", attempt=a)
                    return
```

[subfleet/daemon.py:5712–5713][release-daemon]:

```python
        if job["kind"] != "turn" and a["state"] in ("starting", "running"):
            self._read_answer(a, adir)          # C-6.14: files only, paced; never raises
```

The continuous observer can share compatible file-reading mechanics, but
must continue after a first answer and persist its own evidence cursor.
The first-answer reader's limits above are existing constants. The observer
limits below are new proposed choices responding to
[R1-review-pr150.md, findings 4 and 18][review].

### Explicit native continuation

[subfleet/daemon.py:5547–5552][release-daemon]:

```python
            elif job["kind"] == "resume":
                manifest = self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json") or {}
                resume = manifest.get("resume")
                if not resume or not self._resume_lane(resume["lane_id"], lane) or resume["model_id"] != model["id"]:
                    raise AdapterError("resume source identity is missing or does not match this attempt",
                                       fix="resubmit the resume from the original job")
```

[subfleet/daemon.py:5555][release-daemon]:

```python
            elif resume or (job["kind"] == "revive" and job["caller_session"]):
```

[subfleet/daemon.py:5562–5571][release-daemon]:

```python
                launch = adapter.resume_launch(spec, a["attempt_id"], adir, lane,
                                               credential_env, resume["native_session_id"] if resume else job["caller_session"],
                                               prompt_path, guard_override, model["id"])
                if launch is None:
                    raise AdapterError(
                        f"{lane.provider} cannot resume a session in place", code=7,
                        fix="use `subfleet sessions handoff` to continue this session")
            else:
                launch = adapter.build_launch(spec, a["attempt_id"], adir, lane, credential_env,
                                              model["id"], model.get("effort"), prompt_path, guard_override)
```

Network retry classification does not change these launch predicates,
identity checks or failures. [review-r2.md, finding 4][review2]

## Numerical choices and provenance

Every value in this table is a proposed initial policy or execution bound.
The cited review/evidence files supply the requirement or calibration, not
measurements establishing these exact new limits. The rationale explains
why the proposal selects them. Shadow validation must measure the resulting
cost and hold time before activation. Existing release constants are quoted
separately above; replay thresholds remain sourced to
[replay_states.py, RULES C][replay].

| Proposed choice | Rationale | Source requirement or calibration |
|---|---|---|
| `sample_s: 30` | Shorten detection relative to the retained minute-cadence monitor without continuous socket traffic | [monitor-hotspot-20261006.jsonl][monitor]; [R1-review-pr150.md, findings 2 and 18][review] |
| `tries: 3` per eligible endpoint/family | Retain the trial count used by the monitor and selected replay calibration | [monitor-hotspot-20261006.jsonl][monitor]; [replay_states.py][replay] |
| `connect_tls_timeout_s: 5`, `dns_timeout_s: 2`, `sample_timeout_s: 20` | Bound individual stalls and the whole round below the proposed sample interval; unfinished work becomes missing evidence | [R1-review-pr150.md, finding 18][review] |
| `fresh_intervals: 4` | Tolerate short missed rounds, then release stale restrictions; generation invalidation remains immediate | [R1-review-pr150.md, findings 8, 12 and 18][review] |
| `degraded_starts: 2` per provider/interval | Permit limited progress on partial recovery; count reservations so fast completions cannot multiply starts | [R1-review-pr150.md, findings 7 and 13][review] |
| `max_network_retries: 6` additional reservations per job | Bound the extra launch allowance across restart and recovery while leaving ordinary and lane-fault allowances intact | [R1-review-pr150.md, finding 11][review]; [review-r2.md, finding 1][review2] |
| 32 route groups | Bound retained route metadata and the roster visited by a sampling round; rotate when the deadline prevents full coverage | [R1-review-pr150.md, findings 5 and 18][review] |
| Four endpoints per route | Bound endpoint fan-out while separating inference and conditional startup needs; reject unsupported larger manifests | [R1-review-pr150.md, findings 6 and 18][review] |
| Eight concurrent sockets | Bound sampler socket pressure while allowing parallel trials within the round deadline | [R1-review-pr150.md, finding 18][review] |
| One proxy hop per trial | Bound traversal and helper complexity; routes requiring more remain unmeasurable until a later parity design supports them | [R1-review-pr150.md, findings 5 and 18][review] |
| One observer poll per second | Reuse the first-answer reader's pacing as an initial cadence, with a separate fleet-wide disk budget | [R1-review-pr150.md, findings 4 and 18][review]; quoted `subfleet/daemon.py:206` above |
| 64 KiB per attempt and 256 KiB per fleet per poll | Bound disk/parsing work and rotate streams so one busy attempt cannot take the whole budget | [R1-review-pr150.md, findings 4 and 18][review] |
| 16 KiB per observer line | Admit compact retry metadata while discarding oversized events as unavailable evidence; full response text is unnecessary | [R1-review-pr150.md, findings 4 and 18][review] |
| Two distinct top-level attempts within two sample intervals | Require independent, recent connection evidence; repeated events from one attempt cannot create a quorum | [R1-review-pr150.md, findings 4 and 18][review] |
| One final transition per endpoint/round | Deduplicate intermediate states before publication | [R1-review-pr150.md, finding 18][review] |
| Down within three scheduled intervals after measurements become available; zero holds from unavailable/parity-failed evidence or after invalidation/disable | Allow the selected window to fill while making fail-open release an exact correctness gate | [replay_states.py, RULES C][replay]; [R1-review-pr150.md, findings 8, 12 and 18][review] |

## Acceptance-contract clause

The following clause and companion amendments are ready to paste into the
acceptance contract. Limits introduced here are proposed choices with the
source requirements and rationale given above. The transition thresholds
come from the selected replay rule.
[R1-review-pr150.md, findings 4, 11, 13 and 18][review]; [replay_states.py][replay]

- **C-6.17** Network health may withhold new job attempts. It is off as
  shipped. A policy without `net_health` is off. `enabled: true, act: false`
  observes only. Both flags must be true for admission or new network retry
  exemptions. The sampler and observer never run on admission's worker or
  inside a store transaction. Admission, `why` and dry-run evaluate the same
  immutable view. Network health writes no account or model closure.

  **Policy and execution bounds.** Proposed defaults are `enabled: false`,
  `act: false`, `sample_s: 30`, `tries: 3`, `connect_tls_timeout_s: 5`,
  `dns_timeout_s: 2`, `sample_timeout_s: 20`, `fresh_intervals: 4`,
  `degraded_starts: 2` and `max_network_retries: 6`. Intervals and timeouts
  must be positive. A whole-sample deadline must be shorter than the sample
  interval. Connection and retry counts are bounded nonnegative integers;
  `tries` must be positive. Additional proposed caps are 32 route groups,
  four endpoints per route, eight concurrent sockets and one proxy hop per
  trial. These bound route metadata, endpoint fan-out, socket pressure and
  proxy traversal; they are proposed limits, not observed fleet sizes.
  [R1-review-pr150.md, findings 5, 6 and 18][review]
  Validate those caps before enabling a route. Rotate route groups
  across rounds when the deadline cannot cover the roster. Unvisited groups
  retain only unexpired evidence. Samples start at most once per interval, including
  failed samples. One `net-health` worker owns each sampling round. It never
  overlaps itself or queues catch-up rounds. DNS runs once per endpoint and
  required family per round, under its own deadline. A round attempts at most
  `route endpoints × eligible families × tries` destination connections.
  Proxy hops have an additional recorded socket bound from the route manifest.
  Every operation fits the whole-sample deadline. Cancellation closes sockets.
  A stuck resolver cannot accumulate threads or child processes; an unsupported
  bounded resolver makes that route unmeasurable. Partial rounds publish
  missing measurements, not invented connection failures.
  Worker and scheduling bounds are proposed serialization guards.
  [R1-review-pr150.md, finding 18][review]

  **Route and trust parity.** A reading belongs to a lane route, not just a
  provider name. The adapter publishes the effective inference origin,
  conditional startup endpoints, proxy selection and bypass rules, DNS
  location, address-family policy, TLS backend, root-store fingerprints,
  custom CA and client-certificate configuration. A route key binds those
  settings, the lane configuration epoch and the network generation. Lanes
  share readings only when those route keys are equivalent. Codex workspace
  routing and base-URL overrides must be resolved before a route is actionable.
  Unresolved routing is `unknown`.

  A probe uses a bounded transport helper matching that lane's runtime and
  trust configuration. Direct probes cannot substitute for a proxy route.
  Preserve environment precedence, `NO_PROXY`, system/PAC selection where
  enabled, proxy-side DNS, SNI and hostname verification. Proxy CONNECT and
  proxy authentication are permitted. Provider API requests and provider API
  credentials are not sent. Proxy secrets and TLS private material stay out
  of logs. If runtime roots, proxy authentication, routing or family selection
  cannot be represented and validated, publish `parity: false` and `unknown`
  for that route. Withhold actionable conclusions until parity is established.
  Passive pressure cannot bypass this guard. Probes cost DNS, socket setup
  and TLS traffic, with no billable provider inference request.
  A TLS certificate error has its own stage and configuration cause. It does
  not establish a captive portal or qualify for a shared-network retry exemption.

  **Endpoint and family aggregation.** Inference is required. The default
  origins are `api.anthropic.com:443` for Claude and `chatgpt.com:443` for
  subscription Codex, subject to the effective route.
  [C-mechanisms.md, Transport and endpoints][mechanisms]
  Authentication is
  ancillary while a valid token needs no refresh. It becomes required only
  for a launch that actually requires authentication or refresh. Discovery
  is required only when launch needs it. Analytics and model-list refresh
  remain ancillary. Success at an ancillary endpoint never offsets failure
  at a required endpoint. An ancillary failure never blocks an otherwise
  valid inference launch.

  For each endpoint, record successful and attempted logical trials per
  family, median successful handshake time and failure stages. Pick the
  best family's exact fraction for that sample, then pool its counts over
  the rolling window. Do not average across endpoints. Endpoint states use
  the transition table below. A lane route is `down` if any required endpoint
  is actionably down, else `degraded` if any is degraded, else `healthy` if
  all are healthy, else `unknown`. A proven required-endpoint failure can
  block despite a different required endpoint being unmeasured. An
  unmeasured endpoint alone cannot block. Provider status summarizes route
  states and lists mixed routes; it is never substituted for a lane reading.

  DNS failure is measured failure, not the disappearance of an eligible
  family. NXDOMAIN, resolver error or a completed resolution timeout for a
  required name yields a failed round with ratio zero and stage `dns`.
  The logical trial denominator is recorded separately from actual DNS calls.
  No usable local address or a measured missing route is stage `route`, also
  failed. Absence of AAAA with usable A records is ordinary family absence.
  Failure of the sampler itself is unavailable evidence and `unknown`.
  The family roster comes from the validated client route, not only from
  successful DNS replies. Best-family success establishes reachability.
  It does not prove which family the client uses, HTTP availability,
  WebSocket upgrade success or survival of a long stream. An actionable
  all-family failure requires coverage of every family the client could use.

  **Signal sources.** `Daemon._read_answer` already reads live stream files
  incrementally and stops at the first model answer. Its release code is
  quoted above. The guardian continues writing files. A new `net-passive`
  observer continues throughout each live attempt, including after that
  first answer. Reuse the first-answer reader's regular-file opening,
  offset advancement, partial-line tail and oversized-line skip handling
  where compatible. Give the observer its own continuous cursor lifecycle,
  fleet budget, persisted file identity and restart/freshness rules.
  Neither consumer may consume or reset the other's cursor. The observer reads
  each attempt's `stream.jsonl`, or its guardian-designated stdout file when
  that is the stream source. It does not scan desktop transcripts. It parses
  outside transactions and publishes a bounded batch of evidence and cursors.
  Proposed limits are one poll per second, 64 KiB per attempt per poll,
  256 KiB across the fleet per poll, and 16 KiB per line. These are disk-read
  and parsing costs, with no model requests. Rotation among attempts prevents
  one busy stream from using every poll's budget.
  The smaller line cap is for retry metadata, not full model responses;
  oversized retry events become unavailable evidence.
  [R1-review-pr150.md, findings 4 and 18][review]

  Persist attempt id, file identity, byte offset, partial-line start and
  source-event ids. Parse only complete lines. Buffer partial lines within
  the line cap, and enforce that cap on complete lines before parsing too.
  Skip an oversized line through its next newline and report
  the gap. File replacement, truncation, parse failure and excessive backlog
  invalidate that source until caught up. On daemon restart, persisted
  cursors deduplicate evidence, but unread preexisting bytes cannot become
  fresh evidence through a new ingestion timestamp. With no trustworthy
  producer timestamp, restart at the current file end for actionable signals.
  Newly discovered preexisting streams use the same rule. A poll gap longer
  than the passive freshness window also makes untimestamped backlog
  nonactionable. Preserve skipped ranges for diagnosis. Observer failure removes passive
  evidence and never blocks admission on its own.

  Codex's structured `Reconnecting... waiting for network` signal proves the
  qualifying connection-retry path. Other reconnect events qualify only
  with an explicit DNS, connect, proxy or route cause. Claude's
  `system/api_retry` with `error: unknown` and null status proves no network
  cause. Its terminal API-error text can supply connection evidence through
  the corrected classifier after exit. That is a terminal signal, not an
  assumed live retry signal. `server_error` alone and generic timeouts do not
  qualify. Completed model responses or accepted terminal results are kept
  as positive evidence for validation; tool output alone is not API recovery.

  Evidence binds the attempt, route, source offset or event id, native time
  when present, observed time and network generation. Do not count stderr
  copies of stream events, child events as independent lane attempts, or
  repeated events from the same attempt as a quorum. Within two sample
  intervals, proven connection failures from two distinct top-level lane
  attempts on the same route establish passive pressure. This may lower
  `healthy` or `unknown` to `degraded`; it never creates `down` by itself.
  An already-down endpoint stays down until its recovery rule permits release.
  Passive pressure prevents promotion to healthy while fresh. Expiry or
  observer failure removes that modifier at the next evaluation.
  The quorum and freshness window are proposed guards against one attempt's
  repeated retries becoming a fleet signal.
  [R1-review-pr150.md, findings 4 and 18][review]

  **Freshness and wake.** Age uses a suspend-aware continuous clock within
  the same boot, not a clock that stops during sleep. A reading older than
  four sample intervals is absent. Every pooled component must also be
  fresh. A missing complete reading clears its streak and state to `unknown`.
  Boot, sleep/wake, interface, default-route, resolver, proxy or trust changes
  advance a generation and invalidate active windows, passive evidence and
  health streaks. Consume macOS power and network-change notifications.
  Check the continuous-clock versus awake-clock gap as a fallback for missed
  notifications. A discontinuity invalidates rather than extends freshness.
  The proposed expiry permits short missed rounds without carrying a reading
  across a prolonged gap. [R1-review-pr150.md, findings 8 and 18][review]

  Admission checks generation and evidence age before each evaluation and
  again in the reserving transaction. A stale-generation verdict cannot
  reserve. Invalidation precedes the next admission evaluation and requests
  a fresh sample asynchronously. Scheduling that sample does not await it.
  Startup and wake are fail-open while evidence is unknown. This clause does
  not promise to prevent an initial reboot or wake burst.

  **State transitions.** Pool the best-family counts over up to three fresh,
  complete endpoint samples in the current generation. Use exact fractions
  for thresholds and round only for display. Reset the healthy streak on any
  nonqualifying sample or passive pressure. The table gives the active result.
  Passive pressure prevents a result better than degraded; it cannot improve
  down to degraded. Publish at most one final transition per endpoint per
  round. Unknown evidence releases a network restriction unless fresh,
  proven passive pressure supplies degraded evidence independently.

  | Current state | Fresh pooled fraction `q` | Next state |
  |---|---|---|
  | Any | No usable evidence | `unknown` |
  | `unknown` or `healthy` | `q < 0.15` | `down` |
  | `unknown` or `healthy` | `0.15 <= q < 0.5` | `degraded` |
  | `unknown` or `healthy` | `q >= 0.5` | `healthy` |
  | `degraded` | `q < 0.15` | `down` |
  | `degraded` | `q >= 0.67` on two consecutive readings, passive quiet | `healthy` |
  | `degraded` | Otherwise | `degraded` |
  | `down` | `q >= 0.3` | `degraded`, healthy streak cleared |
  | `down` | Otherwise | `down` |

  Thresholds, window and healthy streak: [replay_states.py, RULES C][replay].
  The single published transition per round is a proposed deduplication rule.
  [R1-review-pr150.md, finding 18][review]

  **Admission and reservation quotas.** With act on, an actionably down
  required route rejects that lane as `network-down`, naming the endpoint
  and route. Chain walking remains upward and within the submitted chain.
  Explicit model and lane pins are preserved. Degraded routes permit slow
  starts. `degraded_starts` limits the provider's aggregate reservations on
  its degraded routes in a sample interval, not active attempt counts.
  Exhaustion rejects those lanes as `network-slow-start`.

  Intervals are UTC buckets of `sample_s`, with a policy-version key. Persist
  reservation counts atomically with attempt reservations. Count unlaunched
  attempts and do not refund fast completions or failed launches within a
  bucket. Restart, wake and route-state changes do not reset the bucket.
  Configuration changes carry prior charges into any overlapping new bucket.
  A clock discontinuity carries charges forward until a full new interval
  has elapsed. The view exposes remaining quota; pure evaluation consumes none.
  `why`, dry-run and repeated evaluation consume none. Admission's preparatory
  probe does not charge a start; its eventual job reservation does, once.
  Transport samples and C-18.1 usage probes consume no start quota.

  **Queue waits and fairness.** Add persisted `wait_reason: network` to
  C-4.1. Use it only when network refusals alone prevent every otherwise
  eligible placement. Its hold records refused routes, evidence age, first
  hold time and next check. Check by the next scheduled sample and on state,
  generation or configuration change, whichever comes first. A mixed
  refusal keeps its existing wait reason and guards, with network evidence
  recorded beside them. A network wait owns no attempt slot or provider
  lease. Existing workspace ownership remains subject to its own rules.

  Amend C-6.9 to exclude an actionable network-only waiter from the competing
  demand set and from spare-slot reservation. Compute this in the current
  view, rather than trusting yesterday's persisted wait label. A mixed
  capacity waiter competes only for model/lane pairs not currently refused
  by network health. It keeps the spare slot only if such demand remains.
  At recovery or expiry, restore normal demand and oldest-first order on
  the same pass before younger jobs reserve. This allows disjoint healthy
  work even when the fleet cap is one.
  That is a configured-cap test case from
  [R1-review-pr150.md, finding 10][review].

  Live attempts retain capacity and ownership leases. Network health never
  kills or evicts them. No provider capacity reservation is introduced here.
  If a configured fleet cap is filled by live network waiters, they can still
  starve another provider. The shipped fleet cap is `null`, meaning uncapped.
  The bound is the existing job wall deadline, plus enforcement delay while
  the daemon or host cannot run. Status reports this occupancy explicitly.

  **Retry pins and route-clock precedence.** Network retry classification
  adds no native continuation. Ordinary jobs keep `build_launch`; explicit
  resume jobs and revive jobs with `caller_session` keep `resume_launch`
  under the release predicate quoted above. Preserve their source-session
  identity checks and unsupported-resume failure. C-13.3 reconciliation still
  runs before another writable attempt. Amend C-6.12 to retain the daemon's
  previous lane/model pair when its only refusals are `network-down`,
  `network-slow-start` and ordinary slot shortage. The pair must still be
  enabled, owned, identity-valid and resolvable to that provider. A closure,
  desktop login exclusion, disabled or latched credential, identity mismatch,
  job exclusion, reserve/floor refusal or invalid pair releases the daemon
  pin and routes as submitted. Explicit user pins are never released by this
  exception. A route error has C-6.12 precedence. Network changes do not
  bypass its route clock, terminal refusal or workspace/reconciliation waits.

  Retry admission resumes at degraded or healthy, subject to quota and every
  other guard. Unknown, stale, removed, unsupported or disabled network
  evidence releases the network hold and this pin exception. Re-evaluate
  through ordinary C-6.12. Retain a pin only if its ordinary rule permits it.

  **Durable retry accounting.** Keep the closed outcome enum. A network
  failure is `transient` with terminal `evidence.network: true`, its failure
  stage, route, source ids and a fresh degraded/down snapshot at actual exit.
  A recovered reconnect, null retry status, side-channel model-list timeout
  or scheduler termination cannot supply that terminal cause. Timeout or
  no-response text without proven connection-establishment failure remains
  an ordinary transient. The controlled `ERR_PROXY_TUNNEL` terminal fixture
  and documented `Unable to connect to API` form must be recognized by the
  upstream cause contract; generic text requires corroborated cause and route.
  The terminal Codex `turn.failed` cause controls over earlier retry messages.
  Daemon cancellation and wall-limit termination keep precedence.

  Persist a per-attempt accounting record and per-job
  `ordinary_attempts_used`, `lane_faults_used`, `network_failures` and
  `network_retries_used`. Accounting reasons are mutually exclusive:
  `ordinary`, `lane-fault` or `network`. Preserve C-4.5's qualifying
  `auth-dead` lane-fault exemption and next-candidate failover, including
  its unpinned, non-turn and unchanged-workspace requirements. A qualifying
  lane fault releases its provisional ordinary charge, records the existing
  `lane_fault` evidence and uses the job's sole lane-fault allowance. It
  charges neither ordinary attempts nor network retries. Issue and consume
  its failover grant without a remaining ordinary or network budget
  precondition, matching the release predicate; a job with `max_attempts: 1`
  still gets that failover. A subsequent `auth-dead` ends the job under
  C-4.5 and cannot acquire another lane-fault or network exemption.
  [review-r2.md, finding 1][review2]; [R1-review-pr150.md, finding 11][review]

  A finalized qualifying network failure increments `network_failures` and
  releases its provisional ordinary charge. It is exempt from the ordinary
  count and repeated-transient lane exclusion. All remaining finalized
  attempts consume the ordinary count. An outstanding reservation is a
  provisional ordinary charge until classified. Only one attempt of a job
  may remain outstanding. No new reservation occurs before its accounting
  and finalization commit. Preserve existing class-specific retry eligibility;
  an accounting credit alone cannot make another terminal class retryable.
  [R1-review-pr150.md, finding 11][review]

  `max_network_retries: 6` means at most six additional reservations after
  qualifying network failures, in aggregate across the whole job. Consume
  `network_retries_used` when reserving such a retry, not when deciding to
  wait. Never refund it if launch fails. A network retry requires remaining
  ordinary budget after releasing the triggering failure's provisional charge,
  as well as the additional network allowance. Ordinary retries require
  ordinary budget. A qualifying lane-fault failover uses C-4.5's own grant,
  independently of the network allowance and network-health flags.
  Repeated network failures therefore do not spend `max_attempts`, but an
  eventual ordinary failure still does. With `max_attempts: 3`, an ordinary
  failure after four exempt network failures uses the first ordinary count,
  even at sequence five. That sequence is the counterexample in
  [R1-review-pr150.md, finding 11][review]. The additional-reservation cap
  is a proposed bound in response to that finding, not a measured failure count.

  Finalization, launch failure, unlaunched recovery, lost-attempt recovery and
  cancellation use this same idempotent ledger, keyed by attempt id. Preserve
  monotonic sequence numbers as identity, never as the budget check. Recovery
  reconciles provisional charges from receipts and persisted classifications.
  Neither recovery, provider recovery, route changes nor disabling resets
  counts. Migrate pre-feature `auth-dead` attempts with recorded `lane_fault`
  evidence as lane faults using the release `_uncharged`/`_lane_fault_of`
  predicates quoted above. Preserve their ordinary-budget credit and mark
  the lane-fault allowance used. Do not reclassify old workspace evidence or
  restore a spent allowance. Migrate other finalized pre-feature attempts
  as ordinary, with no retroactive network exemptions. Reconcile a live
  reservation as provisional and carry forward any pending lane-fault
  failover from the persisted attempt/job state. A grant already followed
  by a reservation is consumed; migration cannot mint another. Persist the
  job's accounting version and pending grants with their triggering attempt
  ids and reasons. Migration, counts and grant reconciliation commit atomically.
  A restart cannot grant or charge the same retry twice.

  On network-budget exhaustion, finalize the last failure as `transient`,
  record `network-budget-exhausted`, fail the job and release its wait. Do not
  silently fall back to ordinary retries for that same network failure. The
  existing job wall deadline can end a hold earlier. It begins at first
  reservation, not submission. Never-started queue time remains unbounded
  by `max_wall_s`; cancellation and queue policy are separate controls.

  **Disable and timer behavior.** Setting either flag false releases all
  network-only holds and re-evaluates affected jobs on the next admission
  pass. Removing endpoints or expiring readings does the same. Other guards
  still apply. `enabled: false` also stops sampling and observation.
  No new network exemption or network retry grant is issued while act is false.
  C-4.5 lane-fault grants remain independent of these flags.
  Already persisted classifications, consumed budgets and pending grants
  retain their accounting meaning under the stored job version. Disabling
  does not erase history or mint new budget. C-18.1 timer probes continue
  unchanged, including their offline detection, alerts, reset-credit guards,
  recovery probing, local identity work and desktop/ownership exclusions.
  This clause never treats a skipped usage probe as an online result.

  **Observability.** `net.sample` records configuration hash, rule version,
  boot/network generation, route and endpoint ids, required/ancillary role,
  family, logical and actual attempt counts, failure stage, parity, timings,
  deadlines, completion and evidence age. `net.passive` records source ids,
  cursor gaps, cause, confidence and expiry without transcript payloads.
  `net.state` records old/new state, pooled counts, transition cause, streak,
  contributing sample and passive ids. `net.hold` records hold/release cause,
  job, routes, duration, quota and remaining retry budgets. State changes get
  one daemon log line. Status and `why` show route state, age, mixed providers,
  held duration, retry budgets and live stalled occupancy. Network-only holds
  are expected queueing under C-6.11. Unknown and parity failures are visible.
  The single log line per change is a proposed notification bound.
  [R1-review-pr150.md, finding 18][review]

### Companion amendments

- **C-4.1:** Add `network` to the persisted wait reasons. It does not hold
  later jobs back under C-6.9 while their current network-only refusal stands.
- **C-4.5 and C-9.5:** Apply C-6.17's persisted accounting version and
  terminal network evidence to retry budgets and lane exclusions. Ordinary
  transient behavior is unchanged for jobs without that version or exemption.
  C-4.5's qualifying lane-fault credit, failover eligibility and subsequent
  `auth-dead` termination remain unchanged, including during migration.
  Writable retries still require C-13.3 reconciliation.
- **C-6.9:** Calculate competing demand and the spare slot after actionable
  network refusals. Restore ordinary ordering immediately when they cease.
- **C-6.11:** Add `network`, `network-down` and `network-slow-start` with
  route, endpoint, age, hold duration and next-check evidence.
- **C-6.12:** Permit the stated daemon retry-pin exception for network and
  slot refusals only. Preserve other refusal and route-clock precedence.
  The exception adds no native continuation. Explicit resume/revive jobs
  retain the existing `resume_launch` predicate and source-session checks.

## Invariants

- **Off changes no current network admission decision.** With either flag
  false or no section, network readings cannot change `evaluate`, pin
  exceptions, competing demand or start quotas. Existing versioned accounting
  remains durable. A job previously given network credits keeps that history;
  off does not promise to undo past decisions.
- **Placement stays upward and within the chain.** The chosen pair is the
  ordinary pair, a later eligible pair in the submitted chain, or none.
  Explicit user pins remain binding.
- **Restrictions are monotonic.** For a fixed job, capacity view, route
  evidence and quota counters, removing eligibility cannot place a job that
  had no eligible pair. Down withholds every start degraded would withhold.
  This property concerns pure evaluation, not changing queue demand over time.
- **Unavailable evidence releases its restriction.** Missing, stale,
  invalidated or unsupported evidence imposes no network hold of its own.
  Fresh independent passive pressure may still ration starts. Disabling
  removes both active and passive restrictions. Every other guard remains.
- **Sleep separates generations.** No pre-sleep active or passive reading
  can authorize a post-wake network decision or reservation.
- **Observers and probes are bounded.** Parsing, DNS, sockets and whole
  rounds have explicit caps. A stuck source cannot block admission or create
  overlapping workers. Observers issue no provider requests.
- **Reservations own quota.** Only the reserving transaction consumes it.
  Fast completion, failed launch, repeat evaluation and restart cannot create
  extra starts in a charged interval.
- **Retries are durable and bounded.** Grant consumption and terminal
  accounting are idempotent across all paths. Network retries never exceed
  the configured aggregate allowance. Started jobs remain subject to their
  original wall deadline and writable reconciliation.
- **Lane-fault credit survives.** A qualifying C-4.5 fault and its migration
  preserve the ordinary-budget credit and failover even with `max_attempts: 1`.
  Network accounting and disabling cannot grant another lane fault.
  [review-r2.md, finding 1][review2]
- **Live ownership stays intact.** A live or quarantined process retains its
  existing leases and capacity accounting. A network-only queue hold creates
  no attempt lease or spare-slot reservation.

## Claude watchdog disposition

Defer watchdog enablement to a separate design. Keep lane settings unchanged.
The retry-count setting is not a duration limit. Watchdog capacity retries
bypass the ordinary count; individual no-response waits can last minutes.
Retry and rate-limit frames lack a shared request id. Joining the first retry
to a retained account rejection is unsafe.
[C-mechanisms.md][mechanisms]; [R1-review-pr150.md, finding 15][review]

Any follow-up must persist a separate absolute network-wait deadline within
the job wall deadline. It must establish event ordering, freshness and
account/model/window scope before interpreting a rejection. Child events
must not terminate the parent. Missing, reordered, resetless or replayed
events cannot manufacture quota evidence. Genuine terminal quota and fatal
spend/credit evidence retain their normal limited behavior. Capacity-only
responses remain separate. A deliberate adapter termination must use
containment and a persisted cause so finalization records the intended
`limited` outcome rather than an incidental signal exit. Tests must cover
quota rejection, capacity-only responses, hard spend/credit failure,
missing events and restart before this follow-up can enable the watchdog.

## Rollout and acceptance criteria

The following are proposed activation targets responding to the review's
replay, shadow, execution-bound and rollback requirements.
[R1-review-pr150.md, finding 18][review]
Observe mode may ship first. Act mode must ship with the companion amendments, wake
invalidation, durable accounting and release behavior as one implementation.
Do not enable the admission hold before its accounting and fairness rules.
The corrected terminal-cause contract under #83 is a dependency.

1. Replay the retained episode with versioned configuration. Reproduce rule
   C's post-recovery down totals of 24 minutes for Claude and 15 for Codex.
   Record detection lag, all state spans, stale gaps and successful traffic
   during unhealthy states. Reject a candidate that worsens those totals
   under the same replay assumptions. Also run the production freshness and
   generation rules over the same input; report that result separately.
   [replay_states.py][replay]
2. Pass controlled integration scenarios with real client routes. A total
   required-route outage must reach down within three scheduled intervals
   after measurement becomes available. A down endpoint must release to
   degraded on the first complete reading meeting the recovery threshold.
   Require zero network holds on unavailable or parity-failed evidence,
   and zero surviving network-only holds after disable or generation change.
   These are proposed timing and correctness targets, not live measurements.
   The timing target allows the selected replay window to fill; zero holds
   is the fail-open invariant. [replay_states.py, RULES C][replay];
   [R1-review-pr150.md, findings 8, 12 and 18][review]
3. Run shadow mode through an awake impairment and a sleep/wake episode.
   Measure held-job minutes, detection lag, successful inference during down,
   and failures while healthy, by route. Validate against independently
   timestamped client successes and terminal causes, not probe ratios alone.
   Separate lane and desktop cohorts. Every down span overlapping repeated
   real successes must have a reviewed explanation or a corrected rule before
   activation. Record the hub's go/no-go decision with the configuration hash.
4. Enable act on a bounded lane cohort. Preserve rollback data. Set `act`
   false to roll back, verify hold release and ordinary demand restoration
   on the next pass, and leave counters intact. Set `enabled` false to stop
   observation too. Re-enable only with a recorded configuration/rule version.

| Required integration coverage | Acceptance condition |
|---|---|
| Direct egress blocked, working CONNECT; dead proxy, working direct path; custom/system CA and mTLS mismatch | Only the lane's validated route can supply an actionable state; parity failures stay unknown |
| DNS loss, no local route, asymmetric families, proxy-side DNS | Positive failures count; missing measurements do not; an unverified alternate family cannot prove client recovery |
| Failed inference with working auth; failed auth with valid tokens; overridden workspace origin | Required endpoints control independently; ancillary success or failure cannot change inference admission |
| TLS works but HTTP, WebSocket upgrade or long stream fails | Report the transport-probe blind spot; do not infer full API health or exempt an ambiguous timeout |
| Sleep, wake onto another network, reboot, clock change, stale reading during a long pass | No old-generation reservation; initial unknown state fails open; existing holds release |
| Partial and oversized lines, duplicate/replayed events, truncation, restart backlog, observer failure | Bounded parsing, no false fresh quorum, no observer-only hold |
| Down to partial recovery, pooled two-of-three success, passive pressure expiry ([R1-review-pr150.md, finding 7][review]) | Recovery admits degraded starts; it cannot remain down waiting solely for healthy |
| Older network-pinned retry, mixed refusals, configured cap-one fleet, younger healthy-provider job ([R1-review-pr150.md, finding 10][review]) | Network-only demand reserves no spare slot; recovery restores ordering; mixed guards still apply |
| Retry pin with closure, desktop login, credential latch, identity mismatch, reserve/floor or route error | Correct pin release or route-clock precedence; explicit user pin and writable reconciliation survive |
| Fast completions, failed/unlaunched reservations, restart or policy edit within an interval | Quota is charged once per reservation and cannot reset into extra starts |
| Ordinary failure after exempt network failures, exhausted network budget, lost/unlaunched recovery, duplicate finalization | Every path uses the durable ledger; sequence does not end ordinary retries early; aggregate grants stay bounded |
| Qualifying `auth-dead` with `max_attempts: 1`; migrated lane-fault evidence and pending/consumed grants; network failures before or after a lane fault ([review-r2.md, finding 1][review2]) | Failover is free of ordinary and network charges; migration preserves the credit; no duplicate grant; subsequent `auth-dead` ends the job |
| Explicit resume and revive retried after a network failure | Existing source identity checks and the `resume_launch` predicate survive classification and admission |
| Disable or endpoint removal while held; enable after restart | Holds and pin exceptions release; prior accounting remains; ordinary guards still control |
| Timer offline cycle, identity refresh, alerts and reset credits during outage and recovery | Existing C-18.1 behavior and exclusions remain intact |
| Live attempts waiting on one provider while the other works | Ownership is retained; occupancy and residual wall bound are visible |

The shipped fleet cap is `null`, meaning uncapped, per release C-6.4 and
`subfleet/default_policy.json:104`, quoted above. Fleet-cap starvation is
conditional on a configured cap being filled by live attempts. Other
capacity and machine guards still apply. The existing job wall default is
21,600 seconds, or six hours, from `subfleet/default_policy.json:109`, quoted
above; [R1-review-pr150.md, finding 10][review] also states the six-hour bound.
No healthy-provider capacity reservation is introduced. A never-started
network waiter has no wall deadline. This is the remaining queue-wait limit;
the configured-cap case also has the stated occupancy and enforcement limit.

## Open questions

- Which route helper can prove parity for each supported CLI/runtime,
  especially system/PAC proxies and native/custom TLS roots? Unsupported
  routes stay unknown until that proof exists.
- Should a later design reserve healthy-provider capacity or bound queue
  time before first reservation? This proposal adopts neither policy.
- Which independently timestamped API-success signal is reliable enough to
  supplement transport-only recovery, especially on proxy-buffered paths?
- What separate live network-wait duration and quota-correlation protocol
  should a future watchdog design use? This proposal enables no watchdog.

## Changes from revision 1

This table covers revisions 2 and 3. Finding ids refer to
[R1-review-pr150.md][review] and [review-r2.md][review2]. All are accepted as
valid. Rows for the first round include the later corrections where required.

| Finding id | Disposition | Where addressed |
|---|---|---|
| R1-1 | Corrected; retract overlapping desktop count and independent-corroboration claim | What was observed |
| R1-2 | Corrected launch/loss cohorts, completed cadence, recovery bursts and terminal window; narrow budget-exhaustion inference | What was observed |
| R1-3 | Removed universal client-survival claims; separate eligible retry paths and deadlines | What the clients establish |
| R1-4 | Specify a bounded, restart-safe continuous observer; acknowledge and reuse the existing first-answer reader; unsupported Claude retry cause is nonactionable | Release compatibility; Acceptance-contract clause: Signal sources |
| R1-5 | Require lane proxy, DNS and TLS parity; allow CONNECT; report certificate/configuration failures separately | Acceptance-contract clause: Route and trust parity |
| R1-6 | Required endpoints combine by failure, ancillary endpoints do not pool; positive DNS/route loss counts as failure; narrow family and transport claims | Acceptance-contract clause: Endpoint and family aggregation |
| R1-7 | Select and rerun rule C; add down-to-degraded release and complete transition table; report literal baseline discrepancy | Hysteresis replay; Acceptance-contract clause: State transitions |
| R1-8 | Invalidate across sleep/network generations and at reservation; explicitly retain fail-open startup | Acceptance-contract clause: Freshness and wake; Invariants |
| R1-9 | Amend C-6.12 pin retention and refusal precedence; classification adds no native continuation; preserve explicit resume/revive launch semantics and user pins | Release compatibility; Acceptance-contract clause: Retry pins and route-clock precedence; Companion amendments |
| R1-10 | Add network wait reason, remove network-only competing demand and spare-slot hold; document occupancy and wall bound; correct default to uncapped and condition fleet-cap starvation on configuration | Release compatibility; Acceptance-contract clause: Queue waits and fairness; Rollout and acceptance criteria |
| R1-11 | Define durable terminal ledger and aggregate network-retry reservations across recovery paths; preserve sequence, reconciliation and C-4.5 lane-fault credit in new accounting and migration | Release compatibility; Acceptance-contract clause: Durable retry accounting |
| R1-12 | Release on unknown, stale, removed, unsupported or disabled evidence; preserve recorded budgets and all other guards | Acceptance-contract clause: Retry pins and route-clock precedence; Disable and timer behavior |
| R1-13 | Charge interval reservations atomically; define restart, policy-edit, launch-failure and probe behavior | Acceptance-contract clause: Admission and reservation quotas |
| R1-14 | Withdraw timer suppression; existing offline, identity, alert and reset-credit behavior continues | Acceptance-contract clause: Disable and timer behavior; Integration coverage |
| R1-15 | Defer watchdog changes; reject uncorrelated quota kill; require separate persisted duration and correlated termination design | Claude watchdog disposition; Open questions |
| R1-16 | Require controlled proxy and generic connection fixtures at the terminal-cause interface; ambiguous response timeouts get no exemption; classifier implementation stays under #83 | Acceptance-contract clause: Durable retry accounting; What the clients establish |
| R1-17 | Correct retry-notification count, models-deadline citation and fault timing origin; remove guaranteed repeated-filter claim | What the clients establish |
| R1-18 | Add replay/shadow gates, integration matrix, bounded execution, event fields, versioning and rollback; cite numeric sources and distinguish proposed constants with rationale | Numerical choices and provenance; Rollout and acceptance criteria; Acceptance-contract clause: Policy and execution bounds; Observability |
| R2-1 | Preserve qualifying lane-fault credit, free failover at `max_attempts: 1`, subsequent `auth-dead` termination and migration of recorded credits/pending grants; quote release predicates | Release compatibility; Acceptance-contract clause: Durable retry accounting; Companion amendments; Invariants; Integration coverage |
| R2-2 | Correct default fleet cap to `null`; make fleet-cap starvation conditional on a configured cap; quote shipped policy | Release compatibility; Acceptance-contract clause: Queue waits and fairness; Rollout and acceptance criteria |
| R2-3 | Quote the live first-answer reader and its caller; specify a continuous observer with compatible offset, partial-line and skip handling and separate cursor lifecycle | Release compatibility; Acceptance-contract clause: Signal sources |
| R2-4 | Quote and preserve explicit resume/revive `resume_launch` semantics; network classification itself adds no native continuation | Release compatibility; Acceptance-contract clause: Retry pins and route-clock precedence; Companion amendments; Integration coverage |
| R2-5 | Cite evidence/review source files for numerical choices; label new caps and observer limits as proposals with rationale; quote release defaults | Numerical choices and provenance; Release compatibility; Acceptance-contract clause; Rollout and acceptance criteria |
