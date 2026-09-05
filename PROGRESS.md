# Lane progress: identity binding (C-1.4, C-10.3, C-10.6, C-10.7)

## State

Planning complete, baseline green (`uv run pytest -q`: 1018 passed, 4 skipped, 99 s).
Implementation not started.

## The rule this lane encodes

A Claude account is whoever the profile endpoint says holds *this* credential
(C-10.6). `~/.claude.json` is a hint the doctor compares against, never the
authority (C-10.3). A usage reading whose profile identity does not match the
lane's recorded identity is evidence, never capacity.

## Design decisions (recorded before code so the tests can cite them)

1. **`probe_profile(credential_env) -> ProfileResult`** lives in
   `subfleet/adapters/claude.py`. Bearer from `CLAUDE_CODE_OAUTH_TOKEN`, else the
   home's `.credentials.json` (`claudeAiOauth.accessToken`). `urllib` only, 15 s,
   never logs the token and never puts exception text (which can echo headers)
   into a result. Status map: 200 with `account.{email,uuid}` and
   `organization.uuid` → `ok`; 403 → `no-scope`; 200 without the fields, or an
   unparseable 200 body → `invalid`; everything else (network, timeout, 5xx, and
   any other HTTP code including 401) → `unavailable` with a code in `detail`.
   401 is deliberately *not* `auth-dead`: C-9.3 reserves that for a 401 from a
   usage endpoint, and the profile endpoint is not one.
2. **One profile fetch per credential per `READING_TTL_S`**, cached in the
   adapter under a SHA-256 of the token (never the token itself), on the
   adapter's injectable clock. "Same credential in the same probe cycle" (C-10.6)
   is then literally true for the probe and the classification of one attempt.
3. **Four identity statuses.** `verified` (profile identity equals the lane's),
   `enrolled` (a setup token with no profile scope), `mismatch`, `unverified`.
   The lane column holds those short names; the evidence the outcome carries uses
   the contract's wording: `verified`, `identity-enrolled`, `identity-mismatch`,
   `identity-unverified`.
4. **An unbound lane is not checked at all.** A lane with neither `identity` nor
   `label` recorded makes no identity claim, so there is nothing to contradict,
   and no profile request is made for it. This keeps every pre-existing lane (and
   the whole offline test suite) working unchanged, and it is what C-10.6's "the
   lane's recorded identity" means when there is none.
5. **A setup-token lane** (enrolled `no-scope`: label recorded, identity NULL)
   keeps storing its stream readings, with evidence `identity-enrolled`. If its
   token later gains profile scope and reports an email different from the
   recorded label, that is a `mismatch` — the label is the only claim such a lane
   has. A genuinely *unavailable* profile is `unverified` for every bound lane,
   setup token included: C-10.6's second sentence has no exception.
6. **Mismatch and unverified yield zero `Reading` rows** — window readings and the
   probe's `admission-observed` alike. The finding is recorded as evidence on the
   attempt (`evidence_json.classification.identity`) and on the lane row
   (`lanes.identity_status`). The outcome *class* is untouched: C-10.6 governs
   readings and candidacy, not whether the turn worked.
7. **Desktop identity** (C-10.3) is the profile of the desktop app's own keychain
   item, `Claude Code-credentials` — the item v1's `claude.keychain_credentials`
   reads, read-only. Verified: a lane is `desktop` when its identity equals that
   identity. Unverified: a lane is `desktop` when its label equals either the
   cached `oauthAccount` email or the label of the last verified desktop identity,
   which is kept in the store as an `events` row of kind `desktop.identity`.
   The daemon probes it at most once per `READING_TTL_S`, and only when a desktop
   login is actually recorded for this `HOME` (a `~/.claude.json` `oauthAccount`)
   or the store already holds a `desktop.identity` event — so no test and no
   fresh install ever reaches for the real keychain.
8. **Doctor.** The cached-versus-profile comparison needs the keychain and the
   network, so it is `doctor --live` (until now: "not implemented"). The
   two-enabled-lanes-one-identity check (C-10.7, C-23.45) only reads the store, so
   it is an ordinary offline check.

## Done

- (nothing yet)

## Next

1. `contracts.py`: `IdentityStatus`, evidence names, `Lane.identity`, `Lane.label`,
   `LaneInfo.identity`/`identity_status`/`label`.
2. `store_schema.sql` + `store.py` migration 2 (`identity`, `label`,
   `identity_status` on `lanes`; `SCHEMA_VERSION = 2`).
3. `adapters/claude.py`: `probe_profile`, the identity check, enrol, probe,
   `probe_outcome`, `classify`, launch notes.
4. `capacity.py`: desktop identity derivation; `mismatch` is not a candidate.
5. `scheduler.py`: the `identity-mismatch` rejection reason.
6. `daemon.py`: seed identity columns, persist `identity_status`, wire the
   desktop probe.
7. `cli.py` doctor: live identity checks and the duplicate-identity check.
8. Fixtures and unit tests; then the milestone-2 e2e case.
