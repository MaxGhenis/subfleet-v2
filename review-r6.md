VERDICT #198: REQUEST_CHANGES

Review round 6 of MaxGhenis/subfleet-v2#198 (D-ST1) at `5f4a5956a`, base `release/217`. The workspace was read-only for the PR; no push and no network remote were used. The real keychain, `/usr/bin/security` and the live stores were never touched. Every probe ran `security` as a recording stand-in placed first on PATH. An audit-hook safety net (`r6_audit_plugin.py`) refused any exec resolving to `/usr/bin/security`; it logged 0 such attempts across all holds.

## Findings

### P1: delegating runners reach `security` again; one is production code

`subfleet/credentials.py:38`, `:58-60`; `subfleet/adapters/claude.py:386-388`, `:539`, `:564`, `:606`; `subfleet/daemon.py:1068`, `:8695`.

Round 5 checks the callable that will execute (`execute = runner or subprocess.run`, `runner=self._runner`). The guard still refuses by identity, so it refuses only when that callable is `None` or `_NATIVE_RUNNER`. A callable that delegates to the native `subprocess.run` is a different object, so it passes. It then executes `security find-generic-password -s <ref> -w`. At `d49195394` every one of the cases below was refused.

Executed (stand-in on PATH; same probe file at both heads):

| Probe | Input | `5f4a5956a` | `d49195394` |
|---|---|---|---|
| p1a | `monkeypatch.setattr(subprocess, "run", w)` for 8 delegating `w`, × 5 readers (`resolve_credential`, `_keychain_blob`, `home_login`, `ClaudeAdapter()._resolve_keychain_token`, `ClaudeAdapter()._plan_from_keychain`): recording pass-through, the suite's selective pass-through idiom, `lambda *a, **k: real(*a, **k)`, `functools.partial(real)`, `partial(real, env=...)`, `Mock(wraps=real)`, `MagicMock(wraps=real)`, a `Popen`-based wrapper; plus `mock.patch.object(subprocess, "run", wraps=real)` | **41/41 executed the stand-in**, returning its token | 41/41 refused, stand-in never ran |
| p1e | `ClaudeAdapter()` built under a delegating patch; the patch is undone; then `_resolve_keychain_token` | **executed**; fixture undo does not remove the bypass | refused |
| p1f | Production wrapper with no test patch: `adapter._runner = enrollment_runner(ClaudeAdapter()._runner, turn)` then token and plan reads | **executed twice** | refused |
| p1g | Through the daemon: enrol a `claude-quota-…` keychain lane with a stub adapter, disable it, register the default `ClaudeAdapter()` (native runner), stub `_enrollment_turn`, re-enrol | **stand-in ran 2×; lane `claude-2` enrolled** | `ProtocolError: refusing personal keychain read under pytest` |

p1f and p1g need no test-side wrapper. `Daemon._enroll_lane_locked` wraps the adapter's native runner in `enrollment_runner`. That wrapper sends keychain argv to `original`, the native `subprocess.run`. Round 4 passed the constructor's runner (`self._credential_runner`, `None`), so this read was refused. Round 5 passes the wrapper, so it is admitted.

The suite already has at least 10 pass-through wrappers of the p1a shape: `tests/unit/test_conversation_service.py:1138`, `tests/fake/test_salvage_finalization.py:784`, `tests/fake/test_workspace_transient.py:209/231/262/295/418`, `tests/unit/test_salvage_unindexable.py:54/349` and `tests/unit/test_soak_tools.py:393/415`. A sweep of 14 related files (610 tests, below) with the audit hook found no test reaching `security` today, so this is latent. However, it reopens exactly what rounds 1–4 closed: no test reaches the login keychain, even after fixture undo. Round 3 graded a comparable host-credential reach as P1.

To make the CI stubs pass, the round-1 regressions were also edited (`tests/unit/test_live_store_guard.py:543`, `:570`). They now redefine `credentials._NATIVE_RUNNER = record`, so "refuses the default backend" is tested only against a substituted sentinel.

**Should the guard refuse by binary rather than runner identity? Yes.** No identity rule can tell a stub from a delegate. The smallest rule that blocks every case above and keeps the CI stub passing:

> Under pytest, refuse any process whose executable, resolved through the call's PATH and `realpath`, is `/usr/bin/security`. Enforce it at the exec layer with `sys.addaudithook`, which monkeypatch cannot undo. Handle the `subprocess.Popen` (fires in `_execute_child` before spawn, with `executable = args[0]`), `os.posix_spawn` and `os.exec` events, and check `under_pytest()` at event time.

Install it in `tests/conftest.py::pytest_configure`. For subfleet Python children that inherit the markers, also install it at `subfleet.credentials` import when the markers are present. Keep `keychain_command`'s identity check as the early, explanatory refusal.

This was demonstrated with the stand-in registered as "native" (p1k). Every one of the 8 wrappers × 5 readers was blocked, raising `PermissionError`, which the readers report as their normal "could not read" errors, and the stand-in never ran. The `test_policy_support` stub shape still passed (p1k-ci), because a non-delegating stub never execs. With this layer, the round-1 regression can again exercise the real default backend instead of redefining `_NATIVE_RUNNER`.

A narrower patch would fix only p1f/p1g: read the keychain through the construction-time runner rather than `self._runner`. It would leave p1a/p1e open.

### P3: the binary check is string-only (pre-existing)

`subfleet/credentials.py:39`. `keychain_command("fixture-ref", s, runner=None | _NATIVE_RUNNER)` builds argv without refusing for `s` in `//usr/bin/security`, `/usr/bin/./security`, `/usr/bin/../bin/security` and `/usr/../usr/bin/security`. That is 8/8 at both heads. It is only reachable through an explicit unusual `security_bin`. The `realpath` rule above covers it.

### Note: explicit delegating runners (pre-existing, by round 4's contract)

`runner=partial(subprocess.run)`, `runner=Mock(wraps=subprocess.run)` and `runner=lambda…` executed the stand-in at both heads (p1b, 3/3). The exec-layer rule covers these too.

## Checked and sound

- **Mid-call patch races are closed.** `execute` is bound once and used for both the check and the call. p1c: a stub bound, then undone inside `keychain_command`, still runs the stub (2 stub calls, stand-in 0). p1d: native bound, then a stub applied mid-call, is refused.
- **No caller passes `runner=None` while executing natively.** The only callers are `credentials.py:60`, `claude.py:388`, `claude.py:564` and `claude.py:606`; all pass their executor. `daemon._keychain_read` only classifies argv.
- **Item 2, `protected_provider_homes()`.**
  - Outside pytest (subprocess with no markers, fake HOME), the list and the `_check_workspace` spelling order equal round 4's and base's `[HOME/.claude, HOME/.codex]` exactly. This held in 4 environments: plain, CLAUDE_CONFIG_DIR/SUBFLEET_CODEX_APP_HOME set (ignored as before), stale session markers, and HOME unset.
  - Under pytest with HOME equal to the account home, the list equals round 4's: the real home string with `samefile` patched to fail (proving no stat), a pwd fixture, and a trailing slash.
  - With a synthetic HOME, the workspace HOME and its parent are refused. `HOME/proj`, `HOME/.claude/projects` and a sibling are allowed, so a test's workspace under a fake HOME is not newly protected.
  - The added paths match base `release/217`'s under-pytest list (`Path.home()/.claude`, `.codex`) plus round 4's overrides, so no test that passed on base can be newly refused by them. `_check_workspace` is the only caller.

## Tests run

All runs used Python 3.12.14 and `/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`, one pytest per hold, each under 20 min.

| Hold | Tree | Files | Result |
|---|---|---|---|
| 1 | `5f4a5956a` | `test_folder_spelling.py`, `test_app_cutover_daemon.py`, `test_policy_support.py` | **34 passed** |
| 2 | `d49195394` (git archive; imports confirmed from that tree) | same | **7 failed, 27 passed**: exactly CI's 7 node ids |
| 3 | `5f4a5956a` | `test_live_store_guard.py` + `_round3` + `_round4` + `_round5` | **365 passed** |
| 4 | `5f4a5956a` | `test_r6_probes_198.py` (66 probes) | 55 failed (41 p1a, p1e, p1f, p1g, 3 p1b, 8 p1h), 11 passed |
| 5 | `d49195394` | same probes | 13 failed (3 p1b, 8 p1h; p1c and p1k-ci "fail" because round 4 refuses stubs, the CI defect itself; stand-in never ran), 53 passed |
| 6 | `5f4a5956a` | `test_r6_probes_198_homes.py` (item 2) | **12 passed** |
| 7 | `5f4a5956a` | 14 files with pass-through wrappers or keychain lanes (`test_conversation_service`, `test_soak_tools`, `test_salvage_unindexable`, `test_workspace_transient`, `test_salvage_finalization`, `test_claude_adapter`, `test_enrollment_runner`, `test_agent_secret_credentials`, `test_lanes_reenroll_keychain`, `test_lanes_reenroll`, `test_lanes_enroll`, `test_admission_liveness`, `test_admission_priority`, `test_unadmittable_pin_admission`) under the audit hook | **610 passed, 0 `security` exec attempts** |

## Not run

- The full suite and Python 3.14.
- A non-Python child invoking `security`; none was found in the tree.

Evidence is in `~/reviews/subfleet-2110/test-live-store-guard/review-r6-evidence/`: hold logs, `probes-*.jsonl`, audit logs and copies of the probe files.
