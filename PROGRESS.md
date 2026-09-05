# Lane progress: identity binding (C-1.4, C-10.3, C-10.6, C-10.7)

## State

Implementation complete; the lane's own tests and the full suite pass. An
adversarial review of the finished code is the last step before the hand-off.

`uv run pytest -q`: 1094 passed, 4 skipped (from 1018 passed, 4 skipped at the
branch point). The four lane files run in 0.9 s:

```
uv run pytest -q tests/unit/test_claude_identity.py tests/unit/test_capacity_desktop.py \
                 tests/unit/test_store_migration_2.py tests/unit/test_doctor_identity.py
72 passed
```

## The rule this lane encodes

A Claude account is whoever the profile endpoint says holds *this* credential
(C-10.6). `~/.claude.json` is a hint the doctor compares against, never the
authority (C-10.3). A usage reading whose profile identity does not match the
lane's recorded identity is evidence, never capacity.

## Decisions

Numbered as first drafted; 4, 5 and 6 were rewritten after a 43-agent
adversarial review of this plan found them too permissive, and 7 after it found
the gate would have read the developer's own keychain during the test suite.

1. **`probe_profile(credential_env) -> ProfileResult`** in
   `subfleet/adapters/claude.py`. Bearer from `CLAUDE_CODE_OAUTH_TOKEN`, else the
   home's `.credentials.json` (`claudeAiOauth.accessToken`, read and never
   written — C-23.47). `urllib` only, 15 s, and the token never reaches a log, a
   result, or an exception's text. Status map: 200 with `account.{email,uuid}`
   and `organization.uuid` → `ok`; 403 → `no-scope`; a 200 without those fields
   or with an unparseable body → `invalid`; everything else — network, timeout,
   5xx, any other HTTP code including 401, and no credential at all — →
   `unavailable`, with the reason in `detail` (`http-401`, `no-token`, the
   exception's type). 401 is deliberately not `auth-dead`: C-9.3 reserves that
   for a 401 from a usage endpoint, and this is not one.
2. **One profile request per credential per `READING_TTL_S`**, cached in the
   adapter under a SHA-256 of the token (never the token itself), on the
   adapter's injectable clock. "The same credential in the same probe cycle"
   (C-10.6) is then literally true for a probe and for the classification of one
   attempt.
3. **Four identity statuses.** `verified`, `enrolled` (a setup token with no
   profile scope), `mismatch`, `unverified`. `lanes.identity_status` holds those
   names; the evidence an outcome carries uses C-10.6's own wording —
   `verified`, `identity-enrolled`, `identity-mismatch`, `identity-unverified`.
4. **A lane that recorded nothing is `unverified`, and is not asked.** C-10.6's
   "recorded only when the profile identity equals the lane's recorded identity"
   is a necessary condition: with nothing recorded, equality cannot hold, so no
   window of such a lane is capacity — and no request is made for it, because
   there is nothing an answer could be compared against. Re-enrolment binds it.
5. **The setup-token carve-out belongs to the lane it names.** `no-scope` yields
   `enrolled` only when the lane recorded no identity. A lane that has one and
   now answers 403 has had its credential changed under it — the incident's own
   shape — and is `unverified`. A label-only lane is judged on its label, the one
   claim it has, and the identity the profile reports is handed back so the
   daemon can bind it once (C-1.4) and compare uuids from then on.
6. **Mismatch and unverified yield zero `Reading` rows** — window readings and a
   probe's `admission-observed` alike. The finding is recorded as evidence on the
   attempt (`evidence_json.classification.identity`) and as a status on the lane
   row, and **a recorded mismatch latches**: C-10.6 says such a lane is not a
   candidate "until an operator re-enrols it", so no later probe may clear it and
   the store refuses the move. The outcome *class* is untouched: C-10.6 governs
   capacity and candidacy, not whether the provider did the work.
7. **Desktop identity** (C-10.3) is the profile of the desktop app's own keychain
   item, `Claude Code-credentials` — the item v1's `claude.keychain_credentials`
   reads, read-only. Verified: a lane is `desktop` when its identity equals that
   identity, and a lane with no identity of its own falls back to that identity's
   label. Unverified: C-10.3's two fallbacks, the cached `oauthAccount` email and
   the label of the last verified desktop identity, which the store keeps as an
   `events` row of kind `desktop.identity`. Who does the asking is an injected
   seam, `Daemon(desktop_prober=...)`: the fake harness runs under a real `HOME`,
   and a filesystem heuristic alone would have read the operator's own credential
   during `pytest`. `~/.claude.json` is re-read every cycle (C-10.3) while the
   network answer is cached for one reading window.
8. **Doctor.** The cached-versus-profile comparison needs the keychain and the
   endpoint, so it is `doctor --live` (until now: "not implemented"). The
   two-enabled-lanes-one-identity check (C-10.7, C-23.45) only reads the store,
   so it is an ordinary offline check, and it also names a mismatched lane and a
   Claude lane that recorded nothing. Both print the command that fixes them.

## Done

- `contracts.py`: `IdentityStatus`, `IDENTITY_EVIDENCE`,
  `IDENTITY_STATUS_BY_EVIDENCE`, `Lane.identity`, `Lane.label`,
  `LaneInfo.identity`/`identity_status`/`label`.
- `store_schema.sql` + `store.py`: schema version 2, `MIGRATIONS`, `_migrate`,
  `put_lane(identity_status=…)` with learn-once identity, `update_lane`'s
  mismatch latch, `lane_from_row` reading the new columns.
- `adapters/claude.py`: `OAUTH_PROFILE_URL`, `_urlopen`, `ProfileResult`,
  `IdentityCheck`, `probe_profile`, `identity_check`, `lane_identity_check`,
  `desktop_credential`, `probe_desktop_profile`; identity in `enroll`,
  `probe_with_model`, `probe_outcome`, `classify`, and the launch notes.
- `capacity.py`: `DesktopIdentity`, `desktop_identity`, `last_desktop_identity`,
  `cached_desktop_identity`, `lane_labels`, `identity_blocked`,
  `build_view(desktop=…)`.
- `scheduler.py`: the `identity-mismatch` rejection, and `label` in `_identities`
  so an email pin survives C-1.4's new account key.
- `daemon.py`: identity columns seeded from `lanes.json`, `_desktop_identity`,
  `_desktop_profile`, `_record_identity`, `_identity_binds`, `_relaunch_env`, and
  `desktop_prober`.
- `cli.py` + `offline.py`: the two doctor checks, `Offline.lanes()`, and
  `KNOWN_SCHEMA_VERSION` following the store to 2.
- Tests: `tests/fixtures/claude/identity/`, `tests/fake/profile.py`,
  `tests/bin/claude --print-profile`, four new unit files (72 tests), four daemon
  tests, two e2e cases, and autouse guards that keep every test off the network,
  off the login keychain, and out of the operator's `~/.claude.json`.

## Next

- Adversarial review of the implementation, then the integrator hand-off.
