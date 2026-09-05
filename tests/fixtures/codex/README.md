# Codex fixture provenance

The corpus implements C-12.7. Discovery on 2026-09-05 used
`ls -t ~/chief-of-staff/state/subfleet/runs | head -150`, followed by individual
`meta.json` reads and individual reads of each selected run's `err.log`,
`raw.jsonl` (when present), and equivalent recorded output. No recursive state
search or provider command ran.

These v1 manifests use `family: "codex"`; none has the brief's `provider` key.
Twenty-nine selected runs identified Codex through `family`. All completed
selected runs had rc 0, and none had a `raw.jsonl`. Error phrases found in some
successful runs were quotations from reviewed source and documentation. Those
quotations were not treated as observed provider failures.

| Case | Provenance |
| --- | --- |
| `success` | Real redacted output from `20260905-111854-fix-06-lane-g-r2`; JSONL envelope normalization described below. |
| `limit-with-clock` | Synthetic experiment-0 style `turn.failed` usage rejection with an explicit UTC reset. |
| `limit-no-clock` | Synthetic usage rejection without a reset. |
| `model-scoped-limit` | Synthetic explicit model rejection with reset. |
| `credits-rejection` | Synthetic insufficient-credit rejection. |
| `auth-401` | Synthetic HTTP 401 from the wham usage endpoint; account email retained. |
| `refresh-token-revoked` | Synthetic explicit refresh-token revocation. |
| `cli-too-old` | Synthetic CLI rejecting the required `--json` flag. |
| `content-filter` | Synthetic nonretryable content-filter rejection. |
| `stream-disconnect` | Synthetic interrupted provider stream. |
| `model-at-capacity` | Synthetic temporary model-capacity rejection. |
| `spawn-fail` | Synthetic rc 127 spawn failure. |

The real success source has `meta.json`, human-formatted `err.log`, `out.md`,
and the rollout named in `expected.json`. Its actual thread id and final
assistant text are represented in `thread.started` and `item.completed`
envelopes because v1 did not request JSON output. The envelopes are normalized;
their identity and deliverable data are recorded provider output. The fixture
keeps the provider header from stderr and the redacted `out.md` as `last.md`.
The prompt and tool transcript are omitted. Absolute personal paths are
redacted, along with any token, cookie, or authorization value.

Each `expected.json` records classification, scope, clock source, closure
presence, native session id, a `synthetic` flag, and detailed provenance.
Reported clocks additionally have `until_at`; guessed clocks deliberately do
not pin the test runner's current time. `closure_reason` records the expected
reason for limited outcomes.

`tests/bin/codex` replays fixture stdout and stderr exactly, consumes stdin,
honors `--output-last-message`/`-o`, and supports `SUBFLEET_FAKE_DELAY_S`.
`SUBFLEET_FAKE_DIAGNOSTICS_PATH` optionally captures argv, cwd, stdin, and the
credential/attempt environment fields for the process isolation tests. The
`nested-setsid` scenario starts a grandchild in a new session, records its pid
in those diagnostics, and keeps both the fake and grandchild alive for 30 s
so the process lane can test containment (C-12.8).
