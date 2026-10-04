# Lane brief: identity (Claude identity binding, C-1.4, C-10.3, C-10.6, C-10.7)

You are implementing the identity-binding clauses of subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/identity`). On 2026-09-05 the v1 system attributed one account's usage to another because it trusted the cached identity in `~/.claude.json` instead of asking the credential itself who it was. v2's contract now says identity comes from the provider profile endpoint using the same credential that produces a usage reading, and a reading whose identity does not match is stored as evidence, never as capacity. This lane makes the code say the same.

## Read first, in this order

1. `docs/acceptance-contract.md`: "Changes in version 2" at the top, then C-1.4, C-10.1 to C-10.7, C-9.1, C-9.8, C-11.2, C-11.3, and C-23.44 to C-23.47 (auth-dead, one account one lane, shadowing, the auth store belongs to the provider CLI). Cite clauses in every test docstring.
2. The incident evidence, read-only: `~/axiom-eng-briefs/opus-expiry-20260905/identity-fix-live-result.json` (a live capacity row whose profile identity is `max@axiom.org` while the cached identity said RulesAtlas; note `identity_binding.status`, `profile_status`, and the `identity` triple) and `~/axiom-eng-briefs/opus-expiry-20260905/subfleet-identity-binding.patch` (the v1 fix: `OAUTH_PROFILE_URL`, `probe_oauth_profile`, `same_identity`, `verified_identity`, the account id form `claude-desktop:<account_uuid>:<org_uuid>`). Port the semantics, not the code; v1's data shapes differ.
3. `subfleet/adapters/claude.py` and `subfleet/adapters/claude_stream.py` (enrol, probe, classify), `subfleet/capacity.py` (the capacity view: how `desktop` and readings are derived), `subfleet/scheduler.py` (where `desktop` and `owner` filter candidates), `subfleet/credentials.py` (how a credential becomes environment), `subfleet/doctor.py` if present on `main`, `subfleet/contracts.py` (`Lane`, `Reading`, `LaneInfo`), `subfleet/store_schema.sql` (the `lanes` table).
4. `docs/lanes/reports/claude-adapter-OUTPUT.md` for the adapter's own notes on enrolment and probing.

## Scope

- `subfleet/adapters/claude.py`: `probe_profile(credential_env) -> ProfileResult` calling `https://api.anthropic.com/api/oauth/profile` with the bearer from `CLAUDE_CODE_OAUTH_TOKEN` (or the home's credential) using only the standard library, a 15 s timeout, and no logging of the token; result is `ok` with (`email`, `account_uuid`, `org_uuid`), `no-scope` (403 on a setup token), `unavailable` (network or 5xx), or `invalid` (200 without the fields). `enroll` records the identity when `ok`, records `identity-enrolled` with the operator's label when `no-scope`, and refuses on `invalid`. `probe` and `classify` attach the identity check to every reading they produce: the readings a run yields are stored only if the profile identity (fetched with the same credential in the same probe cycle, cached for `READING_TTL_S`) equals the lane's recorded identity; otherwise the adapter returns no `Reading` rows and an `Outcome`/probe result carrying evidence `identity-mismatch` (with the observed triple) or `identity-unverified`. A `no-scope` setup token keeps `identity-enrolled` and its stream readings are stored with that evidence.
- `subfleet/contracts.py`: additive fields only: `Lane.identity: str | None` (`<account_uuid>:<org_uuid>`), `Lane.label: str | None` (the email), and `LaneInfo.identity`, `LaneInfo.identity_status`. `subfleet/store_schema.sql`: additive columns `identity TEXT`, `label TEXT`, `identity_status TEXT` on `lanes`, applied as migration 2 in `store.py` with the schema version bumped (C-3.1: additive, numbered).
- `subfleet/capacity.py`: the `desktop` flag is derived from the identity of the desktop app's keychain credential (a probe of that credential's profile each cycle; the credential reference is the same keychain item v1 reads, `claude.keychain_credentials` in `~/chief-of-staff/subfleet/subfleet/claude.py`, read-only), never from `~/.claude.json`. While the desktop identity is unverified, every lane whose label equals either the cached `oauthAccount` email or the last verified desktop identity's label is `desktop` (C-10.3). Record the last verified desktop identity in the store (`events` row of kind `desktop.identity`) so the fallback has something to compare against. A lane with `identity_status` of `mismatch` is not a candidate (C-10.6).
- `subfleet/doctor.py` (or the CLI's doctor if that is where it lives on `main`): a check that compares the cached `~/.claude.json` `oauthAccount` with the profile of the keychain credential and reports agreement, disagreement (with both identities), or unverified; and a check that reports two enabled lanes sharing one identity (C-10.7).
- Fixtures and tests: `tests/fixtures/claude/identity/` with `profile-ok.json`, `profile-mismatch.json` (built from the evidence file's identity triple), `profile-no-scope` (403), `profile-unavailable`; `tests/unit/test_claude_identity.py` covering enrol, probe, and classify under each profile result, and the rule that a mismatch yields evidence and zero readings; `tests/unit/test_capacity_desktop.py` covering the desktop derivation from the profile, the unverified fallback, and the cached-file disagreement; `tests/unit/test_store_migration_2.py` proving the additive migration on a version-1 store; `tests/unit/test_doctor_identity.py`. Extend `tests/bin/claude` so the fake can serve a profile result chosen by `SUBFLEET_FAKE_PROFILE` (the adapter calls a URL, so route the profile fetch through an injectable opener the tests replace; do not shell out).
- One end-to-end case in `tests/e2e/test_milestone2_claude.py`: a lane whose fake profile identity differs from its recorded identity produces no percentage in `status`, is not a candidate in `why`, and shows `identity-mismatch` in `runs show --json` for the attempt that discovered it.

## Out of scope

Codex identity (already `account_id` from `auth.json`), the importer, timers, hooks. Do not change the meaning of any existing clause; if the contract must change to be implementable, say exactly what and why in your final message and do not edit it yourself.

## Acceptance for this lane

- The evidence file's scenario reproduced with the fake: a credential whose profile says `max@axiom.org` while the lane was recorded as RulesAtlas stores no usage windows for that lane and marks it `identity-mismatch`.
- A setup-token lane (profile 403) keeps working: stream readings stored with `identity-enrolled`.
- `doctor` reports the cached-versus-profile disagreement in words and a fix line.
- `uv run pytest -q tests/unit/test_claude_identity.py tests/unit/test_capacity_desktop.py tests/unit/test_store_migration_2.py tests/unit/test_doctor_identity.py` passes in under 20 s; the full suite stays green (`uv run pytest -q`; if `sysctl` is not found, `export PATH=/usr/sbin:/sbin:$PATH`).

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

Standard library only at runtime. Commit after every coherent step and push after every commit: `git push -u origin lane/identity`. Never commit to `main`, never force-push. Never read a real credential value into a file, a log, or a test fixture. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Built; Tests (command, count, time); Clauses covered; Seam changes (every additive field and column); Contract questions; Open questions for the integrator. No preamble.
