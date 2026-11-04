# API credit lanes, core: build report

Build job `20261103-174701-credit-core`, 2026-11-04, 04:22 to 08:35 UTC. Branch
`feat/credit-lane-217`, based on `release/217`, draft PR #292. The design is
`docs/decisions/2026-11-03-api-credit-lanes.md`. This build covers the provider, its
credentials, the one-lane limit, the credit closures and spend cap, the admission rules,
the aliases and enrollment. The harness (`subfleet/api_agent/`, C-31.7 onward) is a
separate build; this one never builds or changes it, and keeps to the fixed interface
between the two.

## Summary

- **This build finished steps 1 to 10 and wired the refused-by-kind alert.** A
  `claude-api` lane enrolls from a plan email, resolves its Console key only for its own
  organization, launches `python -I -m subfleet.api_agent` under a per-attempt dollar
  grant, closes for credit until the plan's renewal, and falls back to its subscription
  twin. Contract section 31 states C-31.1 to C-31.6.
- **Tests:** 177 new tests in seven new files, with Hypothesis properties for invariants
  1, 3, 4, 5, 7 and 8, differential tests against the `claude-credits` reference, an
  example test for every `end.outcome` fixture and every credential refusal.
- **Full suite at `84a6e8778`:** 26,836 tests. 26,685 passed, 1 failed, 40 skipped and
  110 xfailed. The one failure is a person-only frontend test that refuses inside any
  Subfleet lane; it fails the same way on the base commit. The later commit `468ab2f5b`
  changes two refusal texts only, and the five test files that read them pass (848 tests).
- **Testing and review found ten defects in this build before the final pass,** and
  each has a fix commit:
  - five through the full suite;
  - two through the new enrollment tests;
  - one through the refusal-template test;
  - two through my own re-read: refusal fixes that named a mechanism no code has, and a
    credit lane's auth alert that said `claude setup-token`.

  "Test commands and results" lists them.
- **For the hub:** nothing here touches `~/.subfleet/policy.json`. The live policy still
  needs `claude_api.denied_org_ids`, and placing the `-api` aliases in chains is Max's
  call. Enrollment needs the one plan's email and its `--credit-usd`.

## Commits

On top of `cd496463e` (the design record):

| Commit | What |
|---|---|
| `1fcbc97c9` | Step 1: provider `claude-api` in every provider list, store schema 11 (the harness auto-committed it as `fixup! docs(credit-lane)…`) |
| `2537d4637` | Step 2: `resolve_credential` resolves a credit key (C-31.2) |
| `1a167dbc6` | Step 3 and step 6: the adapter, its launch path, the cycle spend cap (C-31.1, C-31.4) |
| `761ea7c39` | Steps 4, 5, 7 and 8: one-lane limit, admission, aliases, enrollment (the harness auto-committed it as `fixup! feat(credit-lane)…`) |
| `f7d8cc027` | Tests for steps 4, 5, 7 and 8, and the defects they found |
| `e92167b95` | Step 9: contract section 31 |
| `a360bf451`, `a7fdb4cdc`, `7367ee76c`, `9976b594e` | Fixes the full suite found |
| `f30b4eda1` | The refused-by-kind alert |
| `3c3c45820` | Invariant 7 property over registry contents |
| `84a6e8778` | A dead credit key's alert names `claude-credits`, not `claude setup-token` |
| `468ab2f5b` | Active wording for the `credential-refused` and `api-lane-limit` refusal texts |
| this report's commit | `docs/reports/credit-lane-core-report.md` |

The two harness-made `fixup!` messages stay as they are: the brief forbids rewriting history.

## Files changed

64 files, about 3,000 lines added (`git diff --stat cd496463e..HEAD`).

**New modules**

- `subfleet/credit_lane.py`: the pure rules. It holds the reference and key-kind
  checks, the fingerprint, the registry reader, renewal and cycle arithmetic,
  `exhausted_until`, the ledger (`attempt_charge`, `spend`), `grant`,
  `denied_org_ids`, and `refused`, the one constructor of a credit credential refusal.
- `subfleet/adapters/claude_api.py`: `ClaudeApiAdapter`. It covers enroll, probe (no
  readings), the launch and resume argv, classify, attest, deliverable and
  `model_answered`.

**Changed modules**

- `subfleet/contracts.py`: the provider, `API_PROVIDER`, read-only capability, and the
  `max_in_flight_per_api_lane` default.
- `subfleet/adapters/registry.py`: the adapter registration.
- `subfleet/credentials.py`: `resolve_credit_key` and the keychain helper lookup.
- `subfleet/home_paths.py`: the registry path in the test-session registry.
- `subfleet/launch_env.py`: the provider-to-vendor map and `SUBFLEET_API_LANE`.
- `subfleet/store.py`, `subfleet/store_schema.sql`: migration 11 rebuilds `lanes` for the
  provider check and adds `credit_usd`; `put_lane(credit_usd=…)`.
- `subfleet/daemon.py`:
  - the launch environment's key handling;
  - the guard override for credit launches;
  - the ledger and grant under one lock;
  - the spent-credit closure;
  - enrollment (`--credit-usd`, the one-lane limit);
  - `lanes.json` credit.
- `subfleet/scheduler.py`: the one-lane admission refusal and capacity block, the C-11.4
  exemption, and the provider-aware slot cap.
- `subfleet/policy.py`: the `claude_api` section's validation and the API slot cap.
- `subfleet/capacity.py`, `subfleet/timers.py`:
  - the provider-aware slot cap;
  - the probe cycle skips credit lanes;
  - the view's refused-by-kind rows.
- `subfleet/alerts.py`: the refused-by-kind alert, and a credit lane's repair text.
- `subfleet/refusals.py`:
  - closed templates for every new refusal reason;
  - lane ids `claude-api-<n>`;
  - provider `claude-api`.
- `subfleet/lanes_transfer.py`: refuses a credit lane (`transfer-api-lane`); the roster
  row keeps `credit_usd`.
- `subfleet/review_publish.py`: the publish-hook self-test reads `--publish-hook <path>`.
- `subfleet/protocol.py`: `LanesArgs.credit_usd`.
- `subfleet/cli.py`: `lanes enroll --api <email> [--credit-usd N]`.
- `subfleet/default_policy.json`: the aliases `opus-api`, `sonnet-api` and `haiku-api`,
  and the `claude_api` section. No chain changed.
- `docs/acceptance-contract.md`: section 31.

**New tests and fixtures**

- `tests/unit/test_credit_lane.py` (54)
- `tests/unit/test_claude_api_adapter.py` (59)
- `tests/unit/test_credit_lane_admission.py` (25)
- `tests/fake/test_credit_lane_enroll.py` (17)
- `tests/unit/test_cli_credit_lane.py` (12)
- `tests/unit/test_credit_lane_alert.py` (5)
- `tests/unit/test_store_api_lane_migration.py` (5)
- `tests/fixtures/claude_api/`: twelve `stream.jsonl` and `last.md` pairs, one per outcome
- `tests/fixtures/claude_credits_ref.py`: the reference functions, copied verbatim

**Tests updated for the change**

- `tests/unit/test_policy.py`: the API slot cap is the one count cap with a default.
- `tests/unit/test_policy_ramp.py` and `tests/unit/test_provider_active_cap.py`: the
  shipped policy's hash.
- `tests/unit/test_scheduler.py`: its lane helper maps `claude-api-<n>`.
- `tests/unit/test_scheduler_tier_preferences.py`: one credit lane, and closures scoped
  to their provider.
- `tests/unit/test_store_migration_2.py` and `tests/unit/test_store_qos_migration.py`:
  schema version 11.
- `tests/unit/test_launch_env_round8.py`: the new helper call site.

## Invariants and their tests

The record's invariants 2 (no identity prompt) and 6 (the key stays in the harness) are
the harness's and its tests'. This build covers the other six.

**1. Credit keys only.**

- `test_only_a_credit_launch_carries_anthropic_api_key_and_only_its_own` is a property
  test that runs through `Daemon._launch_environment`.
  - Its inputs:
    - a daemon environment with or without its own `ANTHROPIC_API_KEY`;
    - a policy that passes the variable or not;
    - a non-credit launch that leaks it in `env_add`.
  - It checks that a credit launch carries exactly its resolved key and that no other
    launch carries the variable. The space is small (24 cases) and Hypothesis covers all
    of them.
- `test_a_credit_launch_never_runs_the_claude_binary` checks the argv.
- `test_resolution_accepts_a_value_only_when_the_reference_rules_do` is a property test,
  differential against `claude_credits.classify` and `fingerprint`. A value resolves
  exactly when the reference accepts it and its fingerprint matches, and no refusal names
  it.
- `test_the_daemon_resolves_a_credit_key_for_the_lanes_own_organization_only` checks what
  the daemon hands resolution.

**3. One credit lane.**

- `test_no_sequence_of_enrollments_enables_a_second_account` is a property test through
  the real daemon. Its inputs are sequences of enrollments and disables. After each step,
  the enabled credit lanes hold at most one account, and every refusal is
  `api-lane-limit` or `enroll-bound`.
- `test_no_credit_lane_is_chosen_while_two_are_enabled` is a property test through
  `scheduler.evaluate`, over random rosters of the three providers.
- Example tests:
  - enrollment refuses a second account;
  - the same account re-enrolls once its lane has `enabled` at 0, and keeps its credit;
  - a disabled lane does not hold the limit;
  - two enabled lanes hold both, as a capacity block.

**4. Fallback only when the credit is gone.**

`test_the_chain_falls_back_only_when_the_credit_lane_is_not_admissible` is a property
test.
- Its inputs:
  - each `[X-api, X]` pair;
  - the credit lane enabled or not, closed or not;
  - zero to three attempts in flight;
  - a second credit lane;
  - zero to two Claude lanes, closed or not.
- Its oracle reads the lane states alone. The scheduler picks `X` only when no
  credit lane is admissible. It picks the credit lane whenever one is admissible, and
  picks `X` whenever a credit closure holds the credit lane and `X` has an admissible lane.

**5. Closure length.**

- `test_closure_length_agrees_with_claude_credits_exhausted_until` is a differential
  property test against the reference.
- `test_renewal_arithmetic_agrees_with_claude_credits` is another, over every renewal day
  and reference date.
- `test_a_credit_closure_ends_where_exhausted_until_says` is a property test through
  `classify`. Its inputs are any renewal day, or none, and any instant from 2020 to 2040.
  The closure ends at local midnight of `exhausted_until`, strictly after the exhaustion.

**7. One organization per lane.**

- `test_a_key_resolves_only_for_the_lanes_own_undenied_organization` is a property test.
- Its inputs:
  - the registry's organization: ours, another, missing, blank or not a string;
  - the lane's identity;
  - the denied list.
- A key resolves exactly when the registry's organization is a nonblank id equal to the
  lane's identity and not denied, and every other case refuses before the keychain read.
- `test_the_first_foreign_organization_holds_the_lane` is a property test over sequences
  of responses. Any response from another organization, or with no organization id,
  marks the lane a mismatch and fails attestation.

**8. Spend stays under the cap.**

`test_spend_stays_under_the_cap` is a property test over sequences of launches and
endings, with three credits and three caps.
- With spends no larger than their grants, the ledger plus the grants in flight never
  exceeds 95% of `credit_usd`.
- With overspends of up to twice the grant, it checks what the core can promise:
  - every grant fits in what remained at its launch;
  - no grant is negative;
  - nothing launches under $1.

  Keeping the total under the cap then rests on the harness stopping within one response
  of its grant, as the record says. See deviation 21.

## Test commands and results

Every run used `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, `UV_CACHE_DIR=$TMPDIR/uv-cache`,
no `PYTHONPATH`, the worktree's `.venv` and a new `--basetemp` under TMPDIR. The repository
does not list `pytest-xdist` as a dependency (CI runs shards serially), so this build
installed it into the worktree's `.venv` only (`uv pip install --python .venv/bin/python
pytest-xdist`, 3.8.0).

**New tests, run per file**

```
.venv/bin/python -m pytest -q -p no:cacheprovider <file> --timeout=300 --basetemp=…
```

| File | Result |
|---|---|
| `tests/unit/test_credit_lane.py` | 54 passed |
| `tests/unit/test_claude_api_adapter.py` | 59 passed |
| `tests/unit/test_credit_lane_admission.py` | 25 passed |
| `tests/fake/test_credit_lane_enroll.py` | 17 passed |
| `tests/unit/test_cli_credit_lane.py` | 12 passed |
| `tests/unit/test_credit_lane_alert.py` | 5 passed |
| `tests/unit/test_store_api_lane_migration.py` | 5 passed |
| `tests/unit/test_handler_refusals.py` (new reasons) | 390 passed |

**Full suite.** A Bash call here may run at most 600 s, so the suite ran as 16 shards of
the repository's own partition, at most four workers each:

```
.venv/bin/python -m pytest -n 4 -q -p no:cacheprovider -p tools.ci_shard_plugin \
  --ci-shard I/16 --timeout=300 --basetemp="$TMPDIR/…"      # I = 1..16
```

`pytest --collect-only` counted 26,830 tests at the first pass and 26,836 at the final
head, after six new tests. Each pass's shard counts sum to its count, so the shards ran
every test once.

- **First pass, 06:05 to 07:20 UTC:** 12 tests failed, in shards 1, 6, 7, 12 and 16.
  Eleven came from five defects of this build, in shards 6, 7, 12 and 16, and those four
  shards passed when rerun after the fixes. The twelfth is the environmental failure
  described below.
  - `test_launch_env_round3`: `SUBFLEET_API_LANE` was not among the names a launch sets.
  - `test_codex_ca_bundle_provider`, six cases: a Daemon built without `__init__` has no
    `policy`.
  - `test_codex_ca_bundle`: stand-ins replace `resolve_credential` with a one-argument
    function. The daemon now passes the new keywords only for a credit lane.
  - `test_lanes_reenroll`: a stand-in replaces `_append_lanes_json` with a one-argument
    function. Only a credit lane's enrollment passes `credit_usd`.
  - `test_launch_env_round8`: its audit had no class for the new `build_environment` call site.
  - The enrollment tests found two defects before the suite ran. Re-enrollment reset the
    credit to $200, and `lanes.json` dropped `credit_usd`.
  - `test_handler_refusals` found the new refusal reasons without closed templates, and
    two reasons that each had two texts.
  - My re-read found two more. The `api-lane-limit` and `transfer-api-lane` fixes told the
    operator to remove the lane from `lanes.json`, which frees nothing (deviation 23). A
    disabled credit lane's auth alert said to run `claude setup-token`.
- **Final pass at `84a6e8778`, 07:32 to 08:26 UTC:** 26,685 passed, 1 failed, 40 skipped,
  110 xfailed.

  | Shard | Passed | Failed | Skipped | Xfailed |
  |---|---|---|---|---|
  | 1 | 980 | 1 | 0 | 1 |
  | 2 | 22 | 0 | 0 | 0 |
  | 3 | 154 | 0 | 0 | 0 |
  | 4 | 178 | 0 | 2 | 0 |
  | 5 | 1,513 | 0 | 2 | 2 |
  | 6 | 1,724 | 0 | 7 | 12 |
  | 7 | 1,973 | 0 | 19 | 2 |
  | 8 | 1,574 | 0 | 0 | 0 |
  | 9 | 1,800 | 0 | 2 | 8 |
  | 10 | 1,326 | 0 | 0 | 10 |
  | 11 | 2,467 | 0 | 0 | 6 |
  | 12 | 4,475 | 0 | 5 | 25 |
  | 13 | 2,089 | 0 | 1 | 31 |
  | 14 | 1,261 | 0 | 0 | 3 |
  | 15 | 1,845 | 0 | 1 | 6 |
  | 16 | 3,304 | 0 | 1 | 4 |
- **After the final pass,** `468ab2f5b` rewords the `credential-refused` template and the
  `api-lane-limit` fix. `test_handler_refusals`, `test_protocol_secrecy`,
  `test_credit_lane_enroll`, `test_credit_lane` and `test_claude_api_adapter` pass on it:
  848 passed.
- **The one failure is environmental.** It is
  `tests/frontend/test_core_live.py::test_the_app_core_drives_a_development_daemon`. The
  daemon answers `person-only: … the caller carries Subfleet's attempt markers`, because
  this build runs inside a Subfleet lane. The same test, run from `git archive cd496463e`
  with the same interpreter, fails with the same refusal.

## Deviations from the design, and why

1. **Store migration.** The `lanes` table's provider check listed only `codex` and
   `claude`. Migration 11 rebuilds the table with foreign keys off and a foreign-key check
   after, and adds `credit_usd` (positive or null).
2. **One vendor, one key.** `launch_env.PROVIDER_VENDORS` makes `claude-api` Anthropic's,
   so an Anthropic credential is its own vendor's. The daemon removes `ANTHROPIC_API_KEY`
   from every launch, then puts back only a credit launch's resolved key. Neither the
   daemon's value nor a policy's `env.pass` reaches any launch.
3. **Read-only capability.** A read-only credit attempt records capability `commands`, as
   a read-only Codex attempt does. The record gives the harness a `bash` tool under a
   read-only sandbox.
4. **Kept to Codex and Claude:**
   - `pick` and the picker;
   - conversations;
   - the fleet-empty alert loop;
   - status.json's lane rows.

   status.json publishes no credit lane yet (open question 3).
5. **Credit lanes and the timers.**
   - The probe cycle skips credit lanes: a standard key cannot read its balance, and the
     daemon sends Anthropic no request of its own.
   - Keepalive already runs on Claude lanes only.
   - `_relaunch_env` never resolves a credit key.
6. **The registry variable is `SUBFLEET_API_PLAN_REGISTRY`, not `…_CREDITS_REGISTRY`.** The
   environment backstop treats any name with `CRED` as a credential, and seven tests
   failed on the first name.
7. **Two readings of an unreadable registry.** Resolution fails closed on an unreadable
   or malformed registry (`credential-refused`). The closure and cycle arithmetic read it
   as "renewal unknown", as the record says.
8. **Resolution checks before it reads the key.** The registry and organization checks
   come before the keychain read, so a mismatch never touches the keychain. Enrollment
   reads no key; the first request of the first attempt checks the lane. Only a test sets
   the `enrolling` flag of `resolve_credit_key`; production code never does.
9. **`workspace-limit` closes with reason `credits`, clock guessed,** until local midnight
   of the day after the harness saw it. `claude_credits.exhausted_until` computes the same
   for a workspace limit, and the record's "one day" means that day, not 24 hours.
10. **A 401 is `auth-dead`.** The record says only that other errors fail the attempt. A
    revoked key now disables the lane at finalize, as on the other providers, so jobs
    stop routing to it instead of each failing there. A 5xx stays `unknown` (open
    question 1).
11. **A response with no organization id counts as foreign,** and holds the lane: fail
    closed.
12. **Attestation accepts the requested model id or its dated snapshot,** such as
    `claude-haiku-5-5-20261101`.
13. **Effort.** `--effort` is `high` when the alias names none: the `-api` aliases copy
    their twins, which name none. A resume reuses the source attempt's effort. Open
    question 2.
14. **Resume.** The native session id is the attempt id. A resume copies the source
    attempt's `messages.jsonl` into the new attempt folder; a missing or blank history
    refuses (`api-resume-history`).
15. **A credit lane refuses isolated reviews and conversation turns**
    (`api-isolated-review`, `api-turn`): the harness has no mode for either.
16. **Network.** `--network` goes only to a writable job whose policy allows network
    (`network.codex_workspace_write`, the flag `JobSpec.network` already reads).
17. **The policy's pass names.** `env_add` also carries the names the policy's
    `env.pass` selected, as on Codex, except `ANTHROPIC_API_KEY` (deviation 2).
18. **The hooks reach the harness as JSON in `guard_override`.** The publish-hook self-test
    (C-12.4) now reads `--publish-hook <path>`, so the daemon tests a credit review's hook
    before launch, as it tests the others.
19. **The ledger.**
    - It spans every lane of the account, so a re-enrolled lane keeps its predecessor's
      spend.
    - The daemon decides and records each grant under one lock.
    - An attempt that ended counts its `end` event's `usd`. Without an `end`, it counts
      the larger of its responses' cost and its grant.
    - An attempt whose harness never started counts only its responses.
20. **A spent budget leaves the launch unmade.** With a grant under $1 the adapter refuses
    with `api-credit-spent`. The daemon then closes the lane (reason `credits`, source
    `api-credit:spent`) and returns the job to the queue through `_unlaunched`, which
    spends one of the job's attempts.
21. **Invariant 8, as the tests state it.** The record states it "with any spends". The core can
    guarantee it only while each attempt spends at most its grant. With an overspend, the
    core still guarantees that each grant fits what remained; the 5% margin covers the
    harness's overshoot of one response, and that part is the harness's to keep (C-31.7
    onward).
22. **The one-lane limit counts enabled lanes.** The admission refusal is standing and is
    also a capacity block, so C-6.3's check inside the reservation sees a lane enabled
    after the decision's snapshot.
23. **No lanes verb disables a lane.** The record says the operator removes the old lane
    first, but no such mechanism exists:
    - seeding never removes a stored lane;
    - only an auth-dead, identity-mismatch or duplicate verdict sets `enabled` to 0.

    The `api-lane-limit` fix therefore reads "enroll another account only after
    claude-api-1 leaves the enabled roster", and no message says that editing
    `lanes.json` frees the limit. A
    revoked key does disable the lane, at its next attempt's 401 (deviation 10).
24. **The API slot cap is the one count cap with a default (2).**
    `max_in_flight_per_lane` caps it lower when the policy sets that.
25. **`lanes.json` carries `credit_usd`** for a credit lane, so a store rebuilt from it
    keeps the credit. Re-enrollment keeps the lane's credit unless `--credit-usd` is
    given.
26. **`lanes transfer` refuses a credit lane** (`transfer-api-lane`): v1 has no credit
    lanes.
27. **Every credential check reads one public form.** Each refusal reason has one closed
    set of templates (C-25.7), so every credit-credential check reads "API credit lane
    credential refused" on the wire. The specific check stays in the exception, for
    daemon.log. A malformed `claude-api-…` reference at enrollment reads as
    `enroll-credential`, which now names `claude-api-<email>`. Templates now render lane
    ids of the form `claude-api-<n>`.
28. **The refused-by-kind alert recovers a day after the last such attempt.** The
    timers' view lays each credit lane's latest refused-by-kind attempt of the last day
    on its row. `evaluate_conditions` raises a critical
    `claude-api-refused-by-kind:<email>` alert from it.
29. **A dead credit key's alert gives the credit tools' repair:**
    `claude-credits add <email>; subfleet lanes enroll --api <email>`.
30. **Two refusal reasons were split** so each reads one template:
    - `api-resume-session` is a bad session id, and `api-resume-history` is no history;
    - `enroll-credit-provider` is `--credit-usd` on a subscription credential, and
      `enroll-credit-usd` is a bad value.

## Open questions

1. **Should a 5xx be `transient`?** It stays `unknown` here, so the attempt fails rather
   than retrying. The harness's retry policy should decide.
2. **What default effort should a credit launch use?** This build passes `high` when the
   alias names none. The harness build should confirm it, or the aliases should name one.
3. **How should `subfleet lanes` and `subfleet status` show the cycle?** The record leaves
   this open. Today the enroll output prints `credit=$N/cycle`, and the closure alert names
   the reopening time. status.json publishes no credit lane, and nothing shows the ledger
   or the grants in flight.
4. **Lane retirement.** Replacing the plan's account, or changing an enabled lane's
   credit, needs the lane disabled first, and no verb does that (deviation 23). Should a
   `lanes retire` verb disable a lane?
5. **The sandbox for the harness's tools** (`codex sandbox` or a Subfleet-owned
   `sandbox-exec` profile) is the harness build's choice.
6. **How does a job continue after the credit lane closes mid-attempt?** The record leaves
   this open. This build adds no cross-provider resume. A credit attempt resumes only on
   a credit lane, from its own `messages.jsonl`.
7. **The live policy needs a decision.**
   - `claude_api.denied_org_ids` stays empty in the repository; the hub sets the real list
     in the live policy.
   - Putting `X-api` ahead of `X` in any chain is Max's call. No shipped chain names an
     alias.
8. **The `--credit-usd` ceiling of $100,000** is a sanity bound of this build's choosing,
   not the record's.
