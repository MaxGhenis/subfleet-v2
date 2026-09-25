# Agreement gates

`subfleet gate plan|pr|continue` and `subfleet-gate plan|pr|continue` use the same native implementation. Main approval always names the exact reviewed plan SHA-256 or the full PR head and base OIDs. The peer is `fable`, `opus`, or `astra`. Its independence comes from the isolated read-only round, not from the model family, so an Opus peer may review an Opus or Fable main (Max, 2026-09-22); v1's complementary-family rule is gone. Optional `--main-model` records the main's family in the gate state, resolving the name as `-m` does (a retired alias such as `sol`, or an exact model id, is accepted; an unknown name exits 2, in `--dry-run` too when a policy is readable); without it the family is left unknown rather than inferred from the peer.

```sh
subfleet gate plan plan.md --peer astra --dry-run
subfleet gate plan plan.md --peer opus --main-approve --expect-sha256 <sha256>
subfleet gate plan plan.md --peer astra --main-approve --expect-sha256 <sha256>
subfleet gate pr 42 --peer astra --main-approve --expect-head <head> --expect-base <base> --on-agreement merge --merge-method squash
subfleet gate continue <gate-id> --main-approve --expect-sha256 <new-sha256> --response response.md
```

`--peer sol` maps to Astra. Claude peers (Fable and Opus) accept per-dispatch `--peer-account` and repeated `--exclude-account`; repeat these on continuation when needed. PR methods are `merge` and `squash`. `--dry-run` reads the artifact and prints the revision and its canonical fingerprint, without opening a writable store, changing state, or submitting a job/action.

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

A mismatched round, any downgrade record, and an unattested Claude (Fable or Opus) round are discarded and re-run; an unattested Codex round counts (C-23.43, below). These physical rounds consume the policy budget. `caps.gate_max_rounds` defaults to 4; a CLI `--max-rounds` can reduce that limit, and the v1 spelling `0` uses the policy limit. A stopped gate names the last verdict or explains why output was not a verdict.

## Merge actions

A merge uses the existing `Actions.claim/publish` helpers shared with reset credits. Its unique operation key is `<repository>:<PR>:<approved-head>`. An intent is pending before remote reads, then the recorded holder performs preflight and the `gh pr merge --match-head-commit` call. All commands pass through the injectable runner.

Preflight requires a clean checkout, open non-draft PR, the approved head and base, clean mergeability, and terminal green checks. Landing verification compares the merge commit's own parents with the approved base, plus the approved head for method `merge`. Neither a mismatch nor an ambiguous call triggers another merge or a revert.

A timeout leaves the action row `unknown`. Read-only reconciliation appends `action.reconciled`, preserving the original terminal row and holder result; the event carries the effective outcome. Restart recovery marks an orphan executing action unknown before admission resumes. A new approved head can receive a new round and action. A reused operation key with another base or method is rejected.

## Integration constraints

The current local v1 source defaults to unlimited rounds and allows explicit retries of some failed merges. C-23.53 and C-23.13 require the stronger behavior above. No acceptance clause was changed. Because the merge key excludes the base, a base-only update or CI recovery cannot retry a terminal failed action for the same head. This is a contract consequence to resolve before changing that behavior.

C-23.2 requires isolated Codex `--ephemeral`; the installed CLI suppresses the persisted rollouts that the current adapter uses for served-model attestation. Local authentic exec fixtures contain no alternative served-model event. Request argv and startup headers are not attestations. So a real isolated Astra round is usually `unattested`, and under C-23.43 it still counts: a Codex peer pins its model at launch and its CLI has no silent fallback, and the round record notes the attestation. A Claude peer (Fable or Opus) round counts only when attested for the requested model, because the provider can serve another one. The tests do not claim live provider or GitHub verification.

An Opus round is ordinary Opus work for admission, so the Fable reserve (C-11.7) applies whenever the policy's `reserve.models` names `fable`, as the shipped default does: a lane without a fresh usage reading is `reserve:fable:unmeasured` and refuses it, and a round pinned with `--peer-account` to such a lane waits until the lane is measured. With `reserve.models` empty (the live policy since 2026-09-24, decision d131) no reserve verdict applies. A gate-review job cannot carry C-11.7a's unmeasured-reserve authorization. `subfleet why <job>` names the verdict. A queued round has no deadline for any peer; cancelling it with `subfleet kill` spends the round.
