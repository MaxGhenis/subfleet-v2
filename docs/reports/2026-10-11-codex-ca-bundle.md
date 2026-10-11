# Codex CA bundle — 2026-10-11

Base: `be7afd5992a660b68c3ccd2edf3fc16e92578e50`, assigned branch `fix/codex-ca-bundle`.

Codex launches select a PEM trust bundle in code. Existing policies that omit
`providers.codex.ca_bundle` use `/etc/ssl/cert.pem`; `null` disables injection.
The loader accepts only an absolute path or null. Launch-time validation requires
a readable regular file with at least one parseable PEM certificate. An unusable
path adds no variable and logs once per daemon, including concurrent launches.

Operator `SSL_CERT_FILE` and `SSL_CERT_DIR` values pass through unchanged, even
empty values. If an operator supplies both, both remain: the two requirements
“at most one source for every environment” and “preserve every operator value”
cannot both hold for that input. The property checks that Subfleet adds at most
one source, adds none alongside an operator source, and never changes either
operator value.

| Change | Location |
| --- | --- |
| Shared CA selection, certificate validation, once-only warning, environment assembly | `subfleet/provider_env.py:19` |
| Default and validated nullable policy | `subfleet/policy.py:23`, `subfleet/policy.py:613`, `subfleet/default_policy.json:4` |
| Credentials, guardian environments for attempts and probes, restart reconstruction | `subfleet/daemon.py:742`, `subfleet/daemon.py:2759`, `subfleet/daemon.py:6729` |
| Version and app-server preflight environments; CA selection in cache key; standalone policy loading | `subfleet/guard/preflight.py:280`, `subfleet/guard/preflight.py:535` |
| Doctor preflight uses the selected state-root policy | `subfleet/doctor.py:407` |
| Launch clause C-10.8, guard clause C-14.2, cache clause C-23.5 | `docs/acceptance-contract.md:276`, `docs/acceptance-contract.md:314`, `docs/acceptance-contract.md:389` |
| Unit and 200-example property coverage, including doctor and recovery | `tests/unit/test_codex_ca_bundle.py:1` |
| Preflight subprocess environment recording and cache/policy regressions | `tests/unit/test_guard_trust.py:925`, `tests/unit/test_guard_trust.py:956`, `tests/unit/test_guard_trust.py:977` |
| Real fake daemon → guardian → provider → tool inheritance | `tests/fake/test_codex_ca_bundle_fake.py:12`, `tests/bin/fakeprov:82` |
| Real launch builders → recording fake provider → tool, without requiring boot-identity inspection | `tests/unit/test_codex_ca_bundle_provider.py:23` |
| Public root certificate fixture; no private key | `tests/fixtures/codex-ca.pem:1` |

The D-NF1 contract entry records restart R = 19:00:31Z, 2026-10-11: by 19:40Z,
54 post-R attempts had zero UnknownIssuer errors; 4 of the 36 pre-R attempts still
running logged 10 after R. About 300 fresh probes had passed during the lane
failures. Intermittent native root loading remains inferred, not established.

Claude receives no CA injection. Static inspection of the installed native
`~/.local/share/claude/versions/2.1.284` executable found `SSL_CERT_FILE`,
`SSL_CERT_DIR`, and `NODE_EXTRA_CA_CERTS`. [Bun's runtime documentation](https://bun.sh/docs/runtime/environment-variables)
documents `SSL_CERT_FILE` support, including `/etc/ssl/cert.pem` as its unset
default. This establishes runtime support; it does not establish that every
Claude API request uses that runtime default. [Claude's network documentation](https://code.claude.com/docs/en/network-config)
documents its bundled/system CA selection and `NODE_EXTRA_CA_CERTS` for custom
roots. No measured Claude incident warrants changing its launch environment;
no live Claude TLS probe was run.

Codex tool subprocesses inherit the variable. The default is the normal system
CA bundle, and certificate verification stays enabled; inheritance is harmless
for the measured workaround. A custom bundle remains the operator's trust
choice. The fake provider also records its child's environment to verify this.

No live plist, installer state, daemon, keychain, or caller checkout was edited.
The stopgap launchd `SSL_CERT_FILE=/etc/ssl/cert.pem` entry becomes redundant
after this ships, but is harmless: code preserves it as an operator override.
The installer needs no separate injection because code supplies the default
for existing policy files too.

## Validation

The first locked batch completed: **224 passed, 4 failed in 612.23s**. It covered
`test_codex_ca_bundle.py`, `test_policy.py`, `test_daemon_integration.py`, and
`test_test_module_names.py`. All 42 new CA selection cases passed, including the
200-example property. Four existing daemon integration tests failed before CA
environment initialization because `procs.boot_id()` could not inspect the host:
the sandbox rejects both `kern.bootsessionuuid` and `kern.boottime` with
`Operation not permitted`. These are
`test_c10_3_no_desktop_login_means_no_credential_is_read`,
`test_c10_3_a_verified_desktop_identity_is_recorded_and_flags_its_lane`,
`test_c10_6_a_mismatch_is_recorded_on_the_lane_and_never_cleared`, and
`test_c1_4_a_label_only_lane_learns_the_identity_its_credential_reports`.

The second locked batch completed: **165 passed, 2 failed, 6 skipped in
714.98s**. It covered `test_doctor.py`, `test_guard_overlay.py`,
`test_codex_adapter.py`, `test_codex_ca_bundle_fake.py`, and the selected guard
trust tests for provider/preflight environment equality, CA cache invalidation,
standalone policy loading, secret stripping, and the existing cache-key inputs.
All nine new guard environment cases passed. The two existing doctor failures
were `test_daemon_lock_row_fails_on_a_lock_whose_process_is_gone` and
`test_daemon_lock_row_passes_against_a_live_daemon`: denied process-identity
inspection produced `unknown` instead of a stale/live verdict.

The six recording-provider process cases remain queued for the shared suite
lock. The six guarded fake-daemon cases skipped because their fixture requires
real boot identity; its existing skip protects ownership checks.
The narrower provider process tests exercise the real credential and launch
builders and child-tool inheritance without fabricating guardian ownership.

Python syntax and Git whitespace checks have passed. Pytest uses one process at a time under
`/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`, with a fresh
`TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-cab-$$/` and Python `shutil.rmtree`
cleanup. Dependencies were installed with `uv sync --frozen` in this workspace.

The full repository suite and live Codex/Claude TLS probes were not run. The
guarded daemon/guardian/provider/tool tests need rerunning on a host where
macOS process-identity inspection is permitted.

## Commit delivery

The worktree's `.git` points outside the writable sandbox. A shared-ref write
was rejected with `Operation not permitted`; the requested branch is also
checked out in the caller's worktree. The caller's metadata was left unchanged.
Commits use isolated local Git storage on `fix/codex-ca-bundle`, with the original
base as their parent and the required co-author trailer. No history was rewritten
and no Git remote was contacted.

Implementation: `5509f1741152594526065e11e899f3cdc836c1ca`.
Standalone preflight policy and contract numbering:
`ce6707c0fe8555c076298f37e91b99f140a4d48a`.

The final commit series will be exported as
`docs/reports/2026-10-11-codex-ca-bundle.bundle`; the bundle is a separate
delivery artifact and is ignored by the repository. The original linked worktree
HEAD remains at the base because the sandbox forbids updating it.
