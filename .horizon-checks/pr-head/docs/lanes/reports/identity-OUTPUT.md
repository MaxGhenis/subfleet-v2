# Lane report: identity binding (C-1.4, C-10.3, C-10.6, C-10.7)

Branch `lane/identity`. The journal of how this was built, and why three of its
rules are stricter than the plan first drafted, is in `PROGRESS.md`.

## Built

**The endpoint.** `subfleet/adapters/claude.py` gains `OAUTH_PROFILE_URL`, a
`ProfileResult`, and `probe_profile(credential_env)`: one `urllib` GET with the
lane's own bearer, 15 s, no third-party import, and the token in exactly one
Authorization header and nowhere else — not a log, not a result, not an
exception's text (a urllib exception can quote the request headers, so only the
exception's *type* is ever kept). Four statuses and no fifth: `ok` (200 naming an
account and an organization), `no-scope` (403, a setup token), `invalid` (a 200
naming nobody), `unavailable` (network, timeout, 5xx, any other code including
401, and no credential at all, with the reason in `detail`). A 401 here is
deliberately not `auth-dead`: C-9.3 reserves that for a 401 from a *usage*
endpoint. One request per credential per `READING_TTL_S`, keyed by a digest of
the token, so the identity beside a reading was fetched in the same probe cycle.

The response shape the adapter reads — `account.email`, `account.uuid`,
`organization.uuid` — was verified against the live endpoint on 2026-09-05, not
only against v1's patch.

**The check.** `identity_check(identity, label, credential_env)` answers C-10.6
for one reading:

| the lane records | the endpoint says | status | its readings |
| --- | --- | --- | --- |
| an identity | that identity | `verified` | stored |
| an identity | another account | `mismatch` | dropped, kept as evidence |
| an identity | 403, or nothing | `unverified` | dropped, kept as evidence |
| only a label | that label's account | `verified`, and the identity is handed back to bind | stored |
| only a label | another account | `mismatch` | dropped |
| only a label | 403 | `enrolled` | stored |
| nothing | *not asked* | `unverified` | dropped |

`enroll`, `probe`, `probe_with_model`, `probe_outcome` and `classify` all go
through it; `build_launch` stamps the lane's identity and label into the launch
notes so a classification can ask the credential that produced the reading.

**The rest of the fleet.** Schema version 2 adds `identity`, `label` and
`identity_status` to `lanes` as a numbered additive migration (C-3.1), applied
ahead of the schema file so the new index finds its column; `offline.py` follows
the store to version 2, or every offline read of a migrated store would warn and
`kill` would refuse (C-3.5, C-17.5). A recorded `mismatch` latches: the store
refuses to move it and only enrolment clears it, because C-10.6 says such a lane
is not a candidate "until an operator re-enrols it". `capacity.py` derives the
`desktop` flag from the profile of the desktop app's own keychain item, with
C-10.3's two fallbacks while that is unverifiable, and keeps the last verified
desktop identity in the store as a `desktop.identity` event. `scheduler.py`
refuses a mismatched lane and gained the lane label in `_identities`, so `-a
max@example.org` still resolves now that a verified Claude account key is a pair
of uuids (C-1.4). `status` and `lanes` say *why* a lane has no readings.

**Doctor.** Offline: two enabled lanes sharing one identity is a defect (C-10.7,
C-23.45), and a mismatched or unbound Claude lane is named with the command that
fixes it. `--live` (until now "not implemented") compares the cached
`~/.claude.json` login with the profile of the desktop credential and reports
agreement, disagreement — naming both identities and telling the operator to
trust the credential — or unverified.

**Harnesses.** `tests/fake/profile.py` serves the endpoint from fixtures for
every harness, by deriving an identity from the bearer or by a fixture named in
`SUBFLEET_FAKE_PROFILE`; `tests/bin/claude --print-profile` serves the same
answer by hand. `Daemon(desktop_prober=…)` makes the desktop probe an injected
seam. Three autouse guards keep the suite off the network, off the login
keychain, and out of the operator's own `~/.claude.json`.

## What it cost to get right

An adversarial review of the plan (43 agents) and of the finished code found what
prose review would not have:

- The first draft exempted a lane that recorded no identity from the check. Every
  lane seeded from `lanes.json` and every row migrated from schema 1 is exactly
  that shape, so the exemption would have left the incident reproducible for the
  lanes the incident actually had.
- The setup-token carve-out applied to any 403, including a lane that already had
  an identity — a credential swapped under a bound lane would have kept storing
  windows.
- Nothing latched a mismatch, so one healthy-looking cycle would have returned a
  lane proven to hold another account to the candidate pool.
- `offline.KNOWN_SCHEMA_VERSION` stayed at 1 while the store moved to 2.
- The desktop probe was gated on `~/.claude.json` existing, which is true on any
  developer machine: `pytest` would have read the real Claude Code keychain item
  and sent that token to the profile endpoint.

Writing the daemon tests then found two more: `_desktop_identity` was overwriting
the profile cache with its own result, and `last_desktop_identity` was reading
the audit event C-3.2 writes beside every mutation instead of the payload.
