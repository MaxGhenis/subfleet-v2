# Agreement gates

`subfleet gate plan|pr|continue` and `subfleet-gate plan|pr|continue` use the same native implementation. Main approval always names the exact reviewed plan SHA-256 or the full PR head and base OIDs. Selecting the complementary peer is the caller's family attestation, as in v1; optional `--main-model` checks an explicit model against the peer's family.

```sh
subfleet gate plan plan.md --peer astra --dry-run
subfleet gate plan plan.md --peer astra --main-approve --expect-sha256 <sha256>
subfleet gate pr 42 --peer astra --main-approve --expect-head <head> --expect-base <base> --on-agreement merge --merge-method squash
subfleet gate continue <gate-id> --main-approve --expect-sha256 <new-sha256> --response response.md
```

`--peer sol` maps to Astra. Fable accepts per-dispatch `--peer-account` and repeated `--exclude-account`; repeat these on continuation when needed. PR methods are `merge` and `squash`. `--dry-run` reads the artifact and prints the revision and its canonical fingerprint, without opening a writable store, changing state, or submitting a job/action.

| Exit | Meaning preserved from v1 |
|---|---|
| 0 | Agreement/completion |
| 1 | Operational error |
| 2 | Invalid input |
| 3 | Changes requested |
| 4 | Blocked/invalid review, including changed revision |
| 5 | Failed, unverified, or queued merge action |

## Ownership and persistence

Only the daemon writes database rows. The CLI calls `gate.start`, `gate.poll`, or `gate.continue`; these run on filesystem workers. `GateStartArgs` and `GateContinueArgs` define the wire shapes. Gate state lives in `events` (`gate.state` with the transition and complete state) and is projected atomically to `$SUBFLEET_HOME/gates/<id>/gate.json`. `certificate.json`, `rounds/<number>-<token>/artifact.json`, the snapshot/patch, prompts, responses, outputs and verdicts retain v1's layout. The journal restores interrupted projections. A certificate replaced by fresh review remains in the prior round's historical state.

A peer round is an ordinary `gate-review` job, pinned to the selected model with one attempt and isolated read-only execution. Inputs are copied under `$SUBFLEET_HOME/reviews/<id>/<round>/`; PR source access points to the clean reviewed checkout. A state root inside a repository cannot supply a neutral review cwd and is refused.

Schema 3 adds `isolated_review`, `review_root`, and `round_lease` to jobs. The ordinary attempt admission transaction reserves `gate:<id>:round:<n>` with holder `gate-round:<job-id>`. The distinct holder survives normal terminal export. Gate consumption checks the accepted attempt's model attestation and artifact hash, re-captures the artifact revision, then checks and releases the round lease in the same transaction that records the verdict. A missing or replaced lease discards the output. A client disconnect does not cancel its durable peer job.

Unattested or mismatched rounds, and any downgrade record, are discarded and re-run. These physical rounds consume the policy budget. `caps.gate_max_rounds` defaults to 4; a CLI `--max-rounds` can reduce that limit, and the v1 spelling `0` uses the policy limit. A stopped gate names the last verdict or explains why output was not a verdict.

## Merge actions

A merge uses the existing `Actions.claim/publish` helpers shared with reset credits. Its unique operation key is `<repository>:<PR>:<approved-head>`. An intent is pending before remote reads, then the recorded holder performs preflight and the `gh pr merge --match-head-commit` call. All commands pass through the injectable runner.

Preflight requires a clean checkout, open non-draft PR, the approved head and base, clean mergeability, and terminal green checks. Landing verification compares the merge commit's own parents with the approved base, plus the approved head for method `merge`. Neither a mismatch nor an ambiguous call triggers another merge or a revert.

A timeout leaves the action row `unknown`. Read-only reconciliation appends `action.reconciled`, preserving the original terminal row and holder result; the event carries the effective outcome. Restart recovery marks an orphan executing action unknown before admission resumes. A new approved head can receive a new round and action. A reused operation key with another base or method is rejected.

## Integration constraints

The current local v1 source defaults to unlimited rounds and allows explicit retries of some failed merges. C-23.53 and C-23.13 require the stronger behavior above. No acceptance clause was changed. Because the merge key excludes the base, a base-only update or CI recovery cannot retry a terminal failed action for the same head. This is a contract consequence to resolve before changing that behavior.

C-23.2 requires isolated Codex `--ephemeral`; the installed CLI suppresses the persisted rollouts that the current adapter uses for served-model attestation. Local authentic exec fixtures contain no alternative served-model event. Request argv and startup headers are not attestations. Consequently real isolated Astra rounds stay unattested and stop at the cap until an authentic provenance transport is available. Fable and test peers use their established attestation paths. The tests do not claim live provider or GitHub verification.
