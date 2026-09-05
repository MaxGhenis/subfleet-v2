# Four cutover prerequisites: decisions (2026-09-05 18:25 EDT)

Max delegated these to the integrator (Fable) with Astra as the peer. Facts were read from the machine and the repositories this evening; nothing below is assumed.

## 1. Canary Codex account: codex-3 (max@axiom.org)

Facts. Six Codex lanes. The desktop app's login is the same account as codex-4 (policyengine), so that lane is shadowed and carries revocation risk; excluded. codex-1 (maxghenis.com, 38% of the week used, resets Sat 9/12) is v1's only dispatchable Codex lane tonight and logged a refresh-token-revoked error at 14:37; taking it would starve v1 for the whole shadow week and would confound any auth failure the canary sees. codex-2 (gmail, 97% used, resets Tue 9/8 20:47) and codex-3 (axiom.org, 97% used, resets Mon 9/7 07:04) are exhausted for v1 until their resets, so transferring one costs v1 nothing this weekend. codex-5 and codex-6 are limited with reset credits available; Max's standing rule is to redeem a credit only when the policy trigger holds, so they stay with v1.

Decision. Transfer codex-3 to v2 tonight. Submit the 100 canary jobs at once; they wait for capacity behind the weekly closure and admit after Monday 07:04, which exercises the closure and timer path as part of the soak. The seven-day soak clock starts at the transfer. Rollback is `sf2 lanes transfer codex-3 --to v1`.

What the transfer enforces. `lanes transfer --to v2` for a Codex home is refused while any v1 launch agent can still reach `~/.codex-3`; the dry run names the agents and the exact `SUBFLEET_CODEX_HOMES` value their plists need. That plist edit plus the roster file (copied to `.bak-<utc>` first) is the only v1 change, and both are reversible.

## 2. Live acceptance: go, scoped to one account and one turn

Facts. `SUBFLEET_LIVE=1` enables two tests: the read-only import dry run and one Haiku turn under one named Claude account (`tests/live/test_claude_live.py`), to catch a change in the shape of the `rate_limit_event` that fixtures cannot. Cost is one Haiku turn of that account's five-hour window.

Decision. Run once now with `SUBFLEET_LIVE_CLAUDE_ACCOUNT=max@thesisinstitute.org` (its five-hour window reset at 17:24, light weekly use, not the desktop login). Record the result in `docs/release-gates.md`. Never set the variable in a lane or in CI.

## 3. PYTHONPATH and PATH shadows: fix inside v2, change nothing in Max's environment

Facts. `PYTHONPATH` is unset in the login shell and in launchd. v1's own `bin/subfleet` wrapper exports `PYTHONPATH=<v1 checkout>` for its process tree, so every lane, hook, and Claude session v1 launches inherits it, and a v2 console script run inside such a process imports v1's package (the compatibility lane hit exactly this). On PATH, `~/bin/subfleet` is the v1 symlink (the front door the milestone-4 flip repoints) and `codex` resolves to `~/.bun/bin/codex` before v1's `~/bin/codex` shim.

Decision. v2's entry points ignore inherited `PYTHON*` variables instead of asking the machine to be clean: (a) `bin/sf2` and `bin/subfleetd` wrappers exec the venv interpreter with `-E -P -m`; (b) the launchd plist's `ProgramArguments` gain `-E`; (c) the doctor row passes when the running interpreter ignores its environment or when `PYTHONPATH` does not shadow. `~/bin/sf2` is installed for the shadow week; `~/bin/subfleet` stays v1 until milestone 4. No edit to `~/.zshrc`, `~/.zshenv`, or launchd's environment.

## 4. `nimbus_quill`: retain verbatim as an opaque provider window

Facts. It appears in v1's `claude-oauth-raw.json`, the raw Anthropic OAuth usage payload, as a window object with `utilization: 0.0` alongside the documented five-hour and seven-day windows. It is undocumented, nothing in v1 routes on it, and the dry run already lists it as "retain".

Decision. Import it as a reading with its own scope (`window:nimbus_quill`), label `unknown`, never consulted for admission; `extra_usage` stays dropped because it is a billing flag, not a window. Revisit only if a live payload ever shows it non-zero.

## Ownership during the transfer (peer round 1 gap)

An account has exactly one owner at every instant, or none. The v2 daemon is installed and running under launchd with the imported store (every lane `owner: v1`) before the transfer starts, so v2 can own the account the moment the store flips. The transfer's own order (`lanes_transfer.py`): for `--to v2` it drops the account from v1's roster first, then flips the store; a failure in between leaves nobody dispatching, which is the safe side. Today none of v1's four launch agents (`com.maxghenis.cos.subfleet`, `-keepalive`, `-mirror`, `-revive`) sets `SUBFLEET_CODEX_HOMES`, so each still globs `~/.codex-1` to `~/.codex-9`; the transfer refuses until their plists carry the five remaining homes. The dry run prints the exact value. Each plist is edited, booted out and bootstrapped again, and the dry run is repeated until it stops refusing; only then does the transfer run. `com.maxghenis.codex-health` is read before that step: if it redeems or rotates, it gets the same variable; if it only reads usage, it is left alone (two readers are permitted, two schedulers or two redeemers are not). Verification after the transfer: v1's `subfleet status` no longer lists codex-3; v1's roster file carries it under `transferred_to_v2`; `sf2 lanes` shows `owner: v2`; the events row exists.

## Read-only canary execution, enforced (peer round 1 gap)

Every canary job is submitted with `-s read-only` and no `-o`. For Codex that becomes `--sandbox read-only` in the guardian's argv (C-12.2), which the OS sandbox enforces, and the argv is recorded in each attempt's `launch.json`. The workdir is a dedicated clone, `~/subfleet-v2-canary/work`, of this repository (no secrets, nothing Max is editing), never one of Max's live checkouts. Prompts are the text of 100 `prompt.md` files sampled from the v1 ledger, each prefixed with a one-line canary preamble stating the run is read-only and asks for analysis of the repository at hand; the canary measures v2's machinery (admission, guardian, receipts, classification, attestation, deliverable capture, notices), not the prompts' original tasks. `tools/canary_check.py` (committed) reads the v2 store and asserts, for every canary job: `launch.json` argv contains `--sandbox read-only`; the job is terminal with a deliverable artifact or a classified failure; no attempt is `quarantined` or `lost`; and the canary clone's `git status --porcelain` is empty with HEAD unchanged. Its output is the release-gate record.

## Soak evidence (peer round 1 gap)

The soak is seven days from the transfer, and it produces a daily record, not a feeling. `tools/soak_report.py` (committed) queries the v2 store and appends `docs/soak/<date>.md` with: counts of attempts by terminal state; every `quarantined` or `lost` attempt with its census evidence; jobs with more than one accepted attempt (must be zero); actions left `unknown` (must be zero); timer events fired that day (probes, keepalive, closures opened and expired) so the record shows the timers ran; canary job progress. It runs daily at 09:00 from a launchd agent installed for the soak and removed after, and is also run by hand on the day of the transfer and on the morning after the Monday reset. A day with a `quarantined` or `lost` attempt, a duplicate acceptance, or a stuck action stops the soak clock until the cause is written down; a clean seven days is the gate.

## Execution order after peer agreement

1. Wrappers, plist `-E`, doctor row; `tools/canary_check.py`; `tools/soak_report.py`; tests; commit.
2. Real import into `~/.subfleet` (every account `owner: v1`); `sf2 daemon install` without `--hooks`; `sf2 doctor`.
3. `sf2 lanes transfer codex-3 --to v2 --dry-run`; apply the named plist environment change to v1's agents; transfer with `--i-understand-v1-edit`; verify v1's roster and v1's `subfleet status` no longer list codex-3.
4. Submit 100 read-only canary jobs with prompts sampled from the v1 ledger's 499 `prompt.md` files, no `-o`, a read-only scratch workdir; they wait for capacity.
5. The live Claude test; record.
6. Record in `docs/release-gates.md`, `docs/migration.md`, and memory. Soak: seven days from the transfer; any `quarantined` or `lost` attempt in `events` is a stop-and-diagnose.

## Risks named

- v1's launch agent plists may be regenerated by v1's own bootstrap; if so the `SUBFLEET_CODEX_HOMES` change must go where v1 generates them from, or v1 will re-grab the home. Check before step 3.
- One hundred jobs become admissible at once on Monday; `max_in_flight_per_lane` is 2 in `default_policy.json`, so the burst is two at a time and the anti-starvation rule orders the rest.
- The v2 daemon under launchd runs with a PATH snapshot taken at install; `codex` and `claude` must be on it.
