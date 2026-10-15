# D-SH1: review launch permissions, capability and publication

The round-2 design at `~/reviews/subfleet-hub/shell-downgrade-2026-10-14/design-r2.md` supersedes round 1's default. The original evidence was 466 successful read-only Claude reviews during October 7–14, 314 prompts asking for commands, 407 deliverables reporting no shell, and two Track B verdicts caused solely by inability to run tests. Fleet ops' addendum establishes that Codex read-only can run commands but cannot write even temporary files, so it cannot run pytest either.

## Round 1 retained

- `--needs-shell` / `--no-needs-shell` and boolean batch `needs_shell` override optional task policy `requires`. Only the distinct requirement `shell` is valid. Resolved requirements persist across retries, recovery and native resume; explicit choices affect idempotency.
- Admission filters incompatible launches before ranking and again at reservation. Exhausted candidates wait with an explanatory `why`; later compatible chain entries remain eligible. Incompatible explicit pins fail at submit with exit 7 and fixes naming workspace-write or a Codex model. Competing demand uses the same requirement.
- The daemon's policy model map is authoritative for `-m`, including sol61, luna and custom names. Empty pins are rejected. Retired sol still maps to astra with its note.
- Round 1's boolean shell evidence is superseded by capability below. Its shipped review shell requirement is removed; shell remains opt-in.

Round-1 commits: `e7bb5fb15`, `3ef714a79`, `205ff2fff`, `8eb36a490`, `bde120c1e`, `da07a2758`, `b5165a845`, `05869349d`, `8d96571ac`. The starting merge `a2a3e8f2cc1e` includes release #205. The source was already present in this checkout; no unpublished staging tail needed recovering. Round 1 left verification unfinished; the verification below covers its retained behavior too.

## Round 2 implementation and invariants

The shipped policy grants `permissions.review = "worktree-write"` and sets no `requires`. This is a policy permission, never a provider sandbox. At submission it resolves to workspace-write only for a new job worktree cut from a committed Git repository. In-place, non-git, isolated reviews and gate rounds resolve directly to read-only. This does not produce a writable result and downgrade it under d261. Explicit workspace-write outside Git remains refused, now with the required read-only escape. Existing writer admission and worktree ownership rules apply to writable reviews.

Attempts record `capability = none | commands | writable` at reservation and confirm it when constructing the launch. Claude read-only is none; Codex read-only is commands; either writable provider is writable. Schema 9 migrates known version-8 shell flags using the stored job sandbox and leaves unknown historical evidence unknown. The deprecated has_shell column remains for additive schema compatibility but new attempts use capability. Historical attempts never receive a fictitious publish denial.

Completion notices, runs show, its JSON and wait summaries expose the evidence. A none launch says it could not run any command; commands says nothing could be written, temp files included, so pytest could not run. The status response and text count distinct run ids with any restricted attempt reserved in the last 24 hours. Deliverable and `-o` bytes remain unchanged.

Review tasks and every gate round receive the same launch-scoped publishing guard on both providers, including resumes. Git/gh wrappers have embedded targets and policy, placed beside the attempt outside the job worktree. Wrapped git refuses push even with config overrides or marker removal. An inherited Git push URL rewrite and empty GIT_ALLOW_PROTOCOL transport allowlist also deny absolute-path pushes and explicit remote push URLs, even with protocol config overrides. Local Git reads still execute; remote Git transports, including fetch, are unavailable. gh allows pr view/diff/checks and field-free API GETs, and fails closed on write verbs, non-GET methods, fields, aliases, extensions and unknown commands. The never-rules hook is untouched. Explicit host-push submission for a review is refused. Attempts and notices say `publish: denied`.

The wrapper layer was chosen because both providers inherit the launch environment and it avoids provider-specific hook parsing or changes to the pinned never-rules hook. It is a command-surface restriction, not an OS boundary against hostile arbitrary code. Code discarding its environment and selecting alternate or absolute binaries can bypass it; raw HTTPS using a token found on disk is also outside its boundary. It must not be represented as token revocation or an immutable host security boundary.

The #222 branch `fix/launch-env-allowlist-217` was read locally (tip observed `1a2287ca300414b361406c92a3c50c8451be4dc1`). Its `build_environment` applies explicit launch additions after ambient filtering; `guardian_pass_args(env)` carries selected names across the guardian boundary. This change puts PATH, GIT_ALLOW_PROTOCOL and the exact GIT_CONFIG_COUNT/KEY_n/VALUE_n entries in Launch.env_add, using that mechanism rather than broadening ambient inheritance. At merge, keep the daemon's final guard application before #222 captures guardian_pass_args, and retain both adapters' additions. No new ambient allowlist entry or SUBFLEET prefix grant is needed.

Contract clauses C-11.1, C-11.2, C-12.3, C-12.4, C-15.1 and C-17.2 and version history describe the change.

## Verification

Every pytest invocation uses its own foreground lockf hold, no lock timeout, the ci profile and temporary Hypothesis storage. There is only one pytest process at a time. The writable-worktree regression invokes pytest in-process to exercise its tmp_path fixture without starting a second pytest process. All Git targets are disposable local file:// repositories and gh is fake. No provider, live store or GitHub endpoint is used.

Tests-first commit `a643b05d1` produced 45 expected failures against the round-1 head. Implementation commit `497b13787` includes the policy, evidence, launch guard and contract. The first implementation check found the old notice invariant parser, a repeated in-process pytest module basename and an existing stale migration-version expectation; these fixtures were corrected without changing deliverable semantics.

The focused corrected run passed 107 tests in 123.82 seconds: review launch permissions 11, publish guard 33, retained needs-shell 23, migration 15 and policy sandbox 25. After hardening, the unit run completed in 190.93 seconds with 923 passes and three failures. All launch, CLI and adapter tests passed. The stale policy-ramp hash was corrected in `3680dcbdf`; the two retention fixture failures are being checked against the baseline before attributing them to this change.

| Unit file (`tests/unit/`) | Passed | Failed in first full run |
| --- | ---: | ---: |
| test_policy.py | 186 | 0 |
| test_policy_disk.py | 57 | 0 |
| test_policy_ramp.py | 45 | 1 (hash corrected; rerun pending) |
| test_policy_support.py | 12 | 2 (baseline check pending) |
| test_policy_tier_preferences.py | 15 | 0 |
| test_cli.py | 207 | 0 |
| test_codex_adapter.py | 93 | 0 |
| test_claude_adapter.py | 172 | 0 |
| test_review_publish_guard.py | 66 | 0 |
| test_store_migration_2.py | 15 | 0 |
| test_offline.py | 33 | 0 |
| test_notice_rendering.py | 21 | 0 |
| test_test_module_names.py | 1 | 0 |

The integration suite invocation remains queued under the shared lock. Baseline and mutation results will be recorded after it completes.

## Delivery and remaining work

The shared Git index is not writable in this sandbox. All round-2 commits use workspace-local `.git-local` metadata on `fix/review-needs-shell-217`, with original ancestry and the required co-author trailer. Files in the assigned worktree contain the changes. No caller checkout write, live policy edit, history rewrite, network remote or push occurred.

The hub owns the live policy installation and notifying Fleet ops. Verification and immutable-publication-boundary caveats are tracked above; final test counts follow before delivery.
