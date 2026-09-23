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

Only the daemon writes database rows. The CLI calls `gate.start`, `gate.poll`, or `gate.continue`; these run on filesystem workers. `GateStartArgs` and `GateContinueArgs` define the wire shapes. Gate state lives in `events` (`gate.state` with the transition and complete state) and is projected atomically to `$SUBFLEET_HOME/gates/<id>/gate.json`. `certificate.json`, `rounds/<number>-<token>/artifact.json`, the snapshot/patch, prompts, responses, outputs and verdicts retain v1's layout. A round's format re-ask adds `peer-prompt.retry1.md` and `peer-output.retry1.md` beside the first attempt's files. The journal restores interrupted projections. A certificate replaced by fresh review remains in the prior round's historical state.

A peer round is an ordinary `gate-review` job, pinned to the selected model with one attempt and isolated read-only execution. Inputs are copied under `$SUBFLEET_HOME/reviews/<id>/<round>/`; PR source access points to the clean reviewed checkout. A state root inside a repository cannot supply a neutral review cwd and is refused.

Schema 3 adds `isolated_review`, `review_root`, and `round_lease` to jobs. The ordinary attempt admission transaction reserves `gate:<id>:round:<n>` with holder `gate-round:<job-id>`. The distinct holder survives normal terminal export. Gate consumption checks the accepted attempt's model attestation and artifact hash, re-captures the artifact revision, then checks and releases the round lease in the same transaction that records the verdict or the round's format re-ask. A missing or replaced lease discards the output and is never re-asked. A client disconnect does not cancel its durable peer job.

Unattested or mismatched rounds, and any downgrade record, are discarded and re-run. These physical rounds consume the policy budget. `caps.gate_max_rounds` defaults to 4; a CLI `--max-rounds` can reduce that limit, and the v1 spelling `0` uses the policy limit. A stopped gate names the last verdict or explains why output was not a verdict.

## Format re-ask

The peer prompt ends with its output rule: the final message is only the verdict block, and any reasoning goes in `summary`. The parser stays strict and never extracts a verdict from surrounding text (C-23.9). When output still fails on form alone, the gate re-asks the peer once within the round. Form failures raise `VerdictFormatError`: a missing, duplicated or misordered sentinel, text outside it, bytes that are not UTF-8, invalid JSON, a payload that is not an object, or a missing or ill-typed required field other than `artifact_revision`. The parser checks the revision binding as soon as the payload is an object, before it reports text outside the sentinels, so a payload bound to another revision (or none) is never a form failure.

An isolated review cannot resume its provider session: the daemon and both adapters refuse, and isolated Codex runs are `--ephemeral`. The re-ask is therefore a new isolated `gate-review` job with the same neutral directory, revision, model, exclusions and round lease key, pinned to the lane the first dispatch ran on. Its prompt, `peer-prompt.retry1.md`, is the first prompt, then the parser's error and the rejected output as JSON-encoded untrusted data, then the output rule. Its output goes to `peer-output.retry1.md`, and the parser judges it by the same rules.

`rejected_output_evidence` scans every block in the rejected output, including duplicated JSON members. It can only refuse or constrain a re-ask; nothing it reads counts. The gate does not re-ask when any block names another revision or none, approves with findings or notes, or requests changes without a finding. It also does not re-ask when the lease was lost, the artifact changed, the dispatch failed, or attestation failed; C-23.43 re-runs an attestation failure as a new round. If any block named a verdict other than `approve` (the template's placeholder aside), an approval from the re-ask is not a verdict and the round blocks.

The transaction that releases the first dispatch's lease also records the re-ask as `format_reask` on the round in `gate.json`, with the journal transition `round-format-reask`. It holds the reason, and the first attempt's job, request, prompt, output, lane, attestation, deliverable hash and candidate verdicts. It also holds the re-ask's job, request, prompt and output. After that, the round's `peer_run_id`, `peer_output`, `submit_args` and `peer_argv` describe the re-ask. After a crash, the recorded re-ask is submitted on the next poll. If its job was created but not yet journaled, resubmitting the same request id returns that job. A round gets at most one re-ask, and the re-ask does not count against `gate_max_rounds`. A second form failure blocks the round and names both errors. The gate result names the re-ask while it runs and after its verdict counts, and the CLI announces each peer job it follows. The certificate is unchanged.

## Merge actions

A merge uses the existing `Actions.claim/publish` helpers shared with reset credits. Its unique operation key is `<repository>:<PR>:<approved-head>`. An intent is pending before remote reads, then the recorded holder performs preflight and the `gh pr merge --match-head-commit` call. All commands pass through the injectable runner.

Preflight requires a clean checkout, open non-draft PR, the approved head and base, clean mergeability, and terminal green checks. Landing verification compares the merge commit's own parents with the approved base, plus the approved head for method `merge`. Neither a mismatch nor an ambiguous call triggers another merge or a revert.

A timeout leaves the action row `unknown`. Read-only reconciliation appends `action.reconciled`, preserving the original terminal row and holder result; the event carries the effective outcome. Restart recovery marks an orphan executing action unknown before admission resumes. A new approved head can receive a new round and action. A reused operation key with another base or method is rejected.

## Integration constraints

The current local v1 source defaults to unlimited rounds and allows explicit retries of some failed merges. C-23.53 and C-23.13 require the stronger behavior above. No acceptance clause was relaxed to match v1. Because the merge key excludes the base, a base-only update or CI recovery cannot retry a terminal failed action for the same head. This is a contract consequence to resolve before changing that behavior.

C-23.2 requires isolated Codex `--ephemeral`; the installed CLI suppresses the persisted rollouts that the current adapter uses for served-model attestation. Local authentic exec fixtures contain no alternative served-model event. Request argv and startup headers are not attestations. Consequently real isolated Astra rounds stay unattested and stop at the cap until an authentic provenance transport is available. Fable and test peers use their established attestation paths. The tests do not claim live provider or GitHub verification.
