# Audit and review command ledger

All commands below ran in the assigned worktree. Source reads were read-only; no live state or caller checkout was read. Line ranges describe the commands as executed, before or during the implementation's line-number changes. Tool session polls only collected command output and are not separate shell commands. No tests or Git mutations were run by the audit/review agent.

## Discovery and outcome-class audit

| Commands | Result |
| --- | --- |
| `pwd && rg --files -g 'AGENTS.md' -g '*contract*' -g '*CONTRACT*' -g 'pyproject.toml' -g 'store.py'` | Exit 0; verified assigned cwd and located contracts, schema-adjacent Store code, pyproject and relevant tests. No AGENTS.md appeared. |
| `for f in AGENTS.md subfleet/AGENTS.md docs/AGENTS.md; do if test -f "$f"; then printf '%s\n' "$f"; cat "$f"; fi; done` | No files present; no instructions were read from these absent paths. |
| `rg -n 'outcome_class|threading.local|def connection|def transaction' subfleet/store.py subfleet/contracts.py subfleet/daemon.py subfleet/conversations subfleet/providers` | Found Store thread-local and transaction code and outcome references. Guessed `subfleet/providers` did not exist (`rg` error); corrected to `subfleet/adapters` below. |
| `rg -n 'D-WALL1|C-4\.5|Change|change.log|changelog|last_observed_alive_at' docs/acceptance-contract.md` | Exit 0; located normative paragraphs and existing changelog format. Output was large, so subsequent reads were narrowed. |
| `rg -n 'outcome_class' subfleet --glob '*.py' --glob '*.sql'` | Exit 0; exactly two production column writes: `_unlaunched` and `_finalize`. Remaining references were schema restrictions or reads. |
| `rg -n 'def query|_local|def get_attempt|def execute|def insert|def update' subfleet/store.py` | Exit 0; identified reader selection, transaction visibility and generic mutation APIs. Repeated once while inspecting the finalizer. |
| `rg -n 'update_attempt\(|outcome.*cls|cls=OutcomeClass|OutcomeClass\.(LIMITED|TRANSIENT|AUTH_DEAD)' subfleet/daemon.py subfleet/conversations --glob '*.py'` | Exit 0; only production `update_attempt` call writes capability, and conversation classifiers return Outcome objects. |
| `rg -n 'state=.quarantined|state=\?.*quarant|quarantined.*state|SET state=.quarantined' subfleet/daemon.py` | Exit 0; found quarantine's class-preserving state update. |
| `rg -n 'quarantined|SET state=.?|add_attempt\(' subfleet/daemon.py \| tail -n 55` | Exit 0; reviewed state transitions, quarantine release and terminal settlement sites. |
| `rg -n 'outcome_class|UPDATE attempts|update_attempt\(' tests/fake/test_lane_fault.py tests/fake/test_wall_running_time.py tests/fake/test_state_contract.py` | Exit 0; inspected existing expected states and timestamp fixture manipulation. |
| `rg -n 'add_attempt\(' subfleet` | Only generic Store method declaration; no production caller. |
| `rg -n '(update_attempt|add_attempt).*\*\*|\[.outcome_class.\]\s*=|outcome_class\s*=' subfleet --glob '*.py'` | Exit 0; no additional column writer hidden behind assignment or keyword use. |
| `rg -n 'class.*Outcome|OutcomeClass\.(LIMITED|TRANSIENT|AUTH_DEAD)' subfleet/adapters subfleet/conversations/classify.py --glob '*.py'` | Exit 0; reviewed the actual adapter directory and exempt Outcome constructors. |
| `rg --files --hidden -g 'AGENTS.md' -g '!\.git' -g '!\.venv' -g '!node_modules'` | Exit 1, no matches; no worktree AGENTS.md. This was the final command in its shell invocation, so that invocation returned 1 despite preceding successful searches. |
| `rg -n 'INSERT( OR [A-Z]+)? INTO attempts|INSERT.*attempts|\.add_attempt' subfleet` | Exit 0; sole production attempt INSERT at original daemon.py:8825 omits outcome_class, leaving NULL. |
| `rg -n 'OutcomeClass|outcome_class' subfleet --glob '*.py' \| wc -l` | Exit 0; 107 matching lines at audit time. |

Successful `sed -n` source reads were grouped as follows (all paths relative to the worktree):

- `subfleet/store.py`: `285,435p`, `435,480p`, `483,509p`, `580,615p`, `629,654p`, `855,883p`.
- `subfleet/contracts.py`: `1,125p`.
- `subfleet/daemon.py`: `10740,10840p`, `11690,12040p`, `11690,11985p`, `11938,11972p`, `11576,11712p`, `11212,11306p`, `8798,8844p`.
- `docs/acceptance-contract.md`: `1,22p`, `443,481p`, `447,467p`.
- A Python `Path.read_text().splitlines()` inspection printed contract lines 8, 9 and 456. Exit 0.

The audit established same-connection finalization visibility: `transaction()` yields `self.connection`; `_reads_elsewhere()` returns false while the same thread holds the writer lock. It also identified the cached transient stream-capture verdict edge that may settle `lost` after its exit receipt disappears.

## Contract edit and verification

The agent ran one Python exact-text replacement script against `docs/acceptance-contract.md`. Each replacement asserted exactly one old-text match before writing. It changed D-WALL1's first sentence to the literal three-condition exemption rule, inserted the per-attempt cap sentence with existing overrun cases, added C-4.5's permitted-attempt provider-time bound, and inserted the dated D-WALL-RETRY1 changelog entry. Exit 0.

| Commands | Result |
| --- | --- |
| `git diff --check -- docs/acceptance-contract.md` | Exit 0, no whitespace errors; repeated after the edit and during final inspection. |
| `git diff --stat -- docs/acceptance-contract.md` | Exit 0; one file, 3 insertions and 2 deletions. |
| Python `subprocess.check_output(['git', 'show', 'HEAD:docs/acceptance-contract.md'], text=True)`, select the original sentence containing `its charged end is no later than its last confirmed liveness`, and assert it remains in the edited text | Exit 0; printed `D-WALL1 last_observed_alive_at sentence preserved byte for byte`. This was a read-only Git operation. |
| `git diff --numstat -- docs/acceptance-contract.md` | Exit 0; `3 2 docs/acceptance-contract.md`. |
| `rg -n 'D-WALL-RETRY1|max_wall_s.*counts summed|total provider time' docs/acceptance-contract.md` | Exit 0; new rule, cap, bound and changelog present. |
| `rg -n 'outcome_class|attempt_state =|provider_finished_at' subfleet/daemon.py \| tail -n 18` | Exit 0; rechecked current implementation references while parent implementation progressed. |

## Independent implementation and test review

| Commands | Result |
| --- | --- |
| `git diff -- subfleet/contracts.py subfleet/daemon.py docs/acceptance-contract.md` | Exit 0; reviewed constant sharing, NULL-safe SQL, terminal-state boundary, finishing id override and contract text. No production correctness finding. |
| `rg -n '_running_s|running_s' tests/fake/test_wall_retry* tests/fake/test_wall_running_time.py tests/unit/test_daemon_verbs.py` | Exit 0; located relevant assertions and overrun comparison. |
| `rg -n '(_running_s.*lambda|setattr.*_running_s|patch.*_running_s|def .*running_s)' tests subfleet --glob '*.py'` | Exit 0; found the existing one-argument census stub. |
| `rg -n '_running_s' tests subfleet --glob '*.py'` | Exit 0; inspected every caller, assignment and reuse. Sole incompatible replacement was census-round2's lambda. |
| `rg --files tests \| rg 'wall_retry|wall.*propert'` | Exit 0; actual new modules are fake/test_wall_retry.py and unit/test_wall_retry_properties.py. |
| `sed -n '1,250p' tests/fake/test_wall_retry.py` | Exit 0; reviewed all four fake-provider scenarios. |
| `sed -n '1,330p' tests/property/test_wall_running_time.py` | Exit 1; guessed path absent. Corrected to the actual unit module next. |
| `sed -n '70,150p' tests/unit/test_census_round2.py` | Exit 0; confirmed the one-argument `_running_s` lambda reaches real finalization through `_lost`. Finding sent to parent for correction. |
| `sed -n '1,330p' tests/unit/test_wall_retry_properties.py` | Exit 0; reviewed independent reference, frozen readers, generated states/classes/stops/timestamps, all invariants and incident replay. |

The review agent did not run tests, apply mutants, or mutate the source/Git state during review. The parent corrected the census stub signature.

## Test commands reported by the implementation agents

The following commands/results were available in the shared drafted report when this ledger was written. They were run by the property/fake-test agents, not by the audit/review agent. Each used the sandbox-permitted task TMPDIR, UV_CACHE_DIR beneath it, and a fresh basetemp.

| Command | Result |
| --- | --- |
| `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest -q tests/unit/test_wall_retry_properties.py --basetemp "$TMPDIR/properties-01"` | 11 passed in 243.89 s. |
| `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest -q tests/unit/test_wall_retry_properties.py -k 'tick_projection or nonexempt or row_order' --basetemp "$TMPDIR/properties-02"` | 3 passed, 8 deselected in 309.50 s. |
| `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest tests/fake/test_wall_retry.py tests/fake/test_wall_running_time.py --basetemp "$TMPDIR/pytest-fake-initial"` | 38 passed, 1 fixture failure in 666.04 s; fixture mistakenly described a natural provider exit after the wall stop. Corrected with signal 15 and killed_by=max_wall_s. |
| `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest tests/fake/test_wall_retry.py::test_lane_fault_extra_attempt_has_its_own_wall_cap --basetemp "$TMPDIR/pytest-fake-lane-fixed"` | 1 passed in 195.68 s. |
| `python3 -m py_compile tests/fake/test_wall_retry.py` | Passed. |

The parent report and `mutants.json` record environment setup, commit attempts, subsequent regressions and all mutant commands/results. Those ongoing results are intentionally not duplicated here as provisional values.

## Property-agent discovery and source commands

The property agent reported the following complete read-command groups. A group ending in a missing-path search can have a nonzero result even though earlier reads succeeded.

| Commands | Result |
| --- | --- |
| `pwd && rg --files -g 'AGENTS.md' -g 'pyproject.toml' -g 'test_wall_running_time.py' -g 'test_invariants_index.py' -g 'contracts.py' -g 'daemon.py' -g 'store.py' -g 'store_schema.sql'` | Exit 0. |
| `rg --files -g 'AGENTS.md' -g 'conftest.py' -g '*wall*' tests subfleet .agents` | Exit 2; `.agents` absent, no AGENTS found. |
| `sed -n '90,175p' subfleet/daemon.py; sed -n '4910,4975p' subfleet/daemon.py; sed -n '11910,11985p' subfleet/daemon.py` | Exit 0. |
| `cat pyproject.toml; sed -n '1,240p' tests/fake/test_wall_running_time.py` | Exit 0. |
| `sed -n '1,220p' subfleet/store.py; sed -n '1,150p' subfleet/contracts.py; sed -n '1,200p' tests/unit/test_invariants_index.py` | Exit 0. |
| `cat tests/unit/conftest.py; sed -n '1,220p' tests/conftest.py; rg -n 'def (add_job\|add_attempt\|add_lane\|__init__\|tx\|query\|close)' subfleet/store.py; sed -n '20,170p' subfleet/store_schema.sql` | Exit 0. |
| `sed -n '1,140p' tests/unit/test_test_module_names.py; rg -n 'Store\(\|Daemon\.__new__\|object.__new__' tests/unit/test_daemon_verbs.py tests/unit/test_store.py tests/unit/test_store_invariants.py tests/unit/test_models.py` | Exit 2; three guessed store/model test names absent. |
| `sed -n '270,330p' subfleet/store.py; sed -n '805,885p' subfleet/store.py; sed -n '170,240p' subfleet/store_schema.sql; rg -n 'def (upsert_lane\|transaction\|snapshot)' subfleet/store.py; rg -n 'Store\(\|Daemon\.__new__\|object.__new__' tests/unit/test_daemon_verbs.py tests/unit/test_store_contract.py tests/unit/test_store_concurrency.py` | Exit 2; the two guessed store test names absent. |
| `rg --files tests/unit \| rg '(hypothesis\|wall\|store\|bound\|property)'; git status --short` | Exit 0. |
| `sed -n '5660,5710p' subfleet/daemon.py; sed -n '1,160p' tests/unit/test_store_properties.py; sed -n '720,770p' subfleet/store.py; sed -n '1,100p' tests/unit/test_daemon_verbs.py; sed -n '400,485p' subfleet/store.py` | Exit 0. |
| `sed -n '1,80p' subfleet/store_schema.sql; rg -n 'def _seconds_between' subfleet/daemon.py; rg -n 'def _attempts_left\|lane_fault' subfleet/daemon.py \| head -20` | Exit 0. |
| `sed -n '1,280p' tools/wall_retry_mutants.py`; `sed -n '4925,4970p' subfleet/daemon.py; rg -n 'finishing_attempt_id\|WALL_EXEMPT\|test_transient_overrun_counts' subfleet/daemon.py tests/fake/test_wall_retry.py` | Exit 0; independent harness review. |

Four successful `apply_patch` edits created and strengthened the single property module. No production source was edited by this agent.

## Fake-test agent discovery and source commands

| Commands | Result |
| --- | --- |
| `pwd && rg --files -g 'AGENTS.md' -g 'pyproject.toml' -g 'test_wall_running_time.py' -g 'test_lane_fault.py' -g 'test_wall_cancel_races.py' -g 'test_wall_stop_deliveries.py'` | Exit 0. |
| `rg --files -g 'AGENTS.md' -g '!node_modules' -g '!vendor' . && git status --short` | Exit 1, no AGENTS; status was not reached. |
| `sed -n '1,260p' tests/fake/test_wall_running_time.py`; `sed -n '1,300p' tests/fake/test_lane_fault.py`; `sed -n '1,220p' tests/fake/test_state_contract.py`; `sed -n '260,520p' tests/fake/test_wall_running_time.py`; `sed -n '11900,12030p' subfleet/daemon.py` | All exit 0. |
| `rg -n 'wall_overrun\|lost' tests/fake/test_wall* tests/fake/test_state_contract.py tests/fake/test_workspace_transient.py` | Exit 0. |
| `rg -n '^def reserve\|^def receipt_fixture\|def _lost\|def _has_work\|def _process_attempt\|def _enforce_wall\|def _running_s' tests/fake/test_state_contract.py subfleet/daemon.py && cat subfleet/adapters/registry.py` | Exit 0. |
| `sed -n '240,410p' tests/fake/test_state_contract.py && sed -n '5630,5735p' subfleet/daemon.py && sed -n '4900,4975p' subfleet/daemon.py` | Exit 0. |
| `rg -n 'test_module\|cross.*import\|fixture' tests/unit/test_test_module_names.py && cat tests/unit/test_test_module_names.py` | Exit 0. |
| `rg -n 'class .*Limited\|OutcomeClass.LIMITED\|def test_.*limited' tests/fake/test_retry_for_good_refusals.py tests/fake/test_state_contract.py tests/fake/test_lane_fault.py tests/fake_adapter.py && cat tests/fake_adapter.py` | Exit 0. |
| `sed -n '11301,11375p' subfleet/daemon.py && sed -n '9941,10075p' subfleet/daemon.py` | Exit 0. |
| `rg -n 'hypothesis\|pytest\|addopts\|testpaths' pyproject.toml && rg -n '_factories\|registry' tests/fake/conftest.py tests/conftest.py` | Exit 1, second search had no matches. |
| `sed -n '650,715p' tests/fake/test_state_contract.py && sed -n '35,65p' pyproject.toml && rg -n '_running_s\(' tests/fake/test_lane_fault.py` | Exit 1, final search had no matches. |
| `git diff --check -- tests/fake/test_wall_running_time.py && git diff --stat -- tests/fake/test_wall_running_time.py && python3 -m py_compile tests/fake/test_wall_retry.py` | Exit 0. |
| `git diff --numstat -- tests/fake/test_wall_running_time.py && git status --short -- tests/fake/test_wall_retry.py tests/fake/test_wall_running_time.py` | Exit 0. |
| `rg -n 'def _lane_fault' subfleet/daemon.py && sed -n '11725,11795p' subfleet/daemon.py` | Exit 0. |

Successful `apply_patch` edits created the fake module, changed the existing transient-prior expectation, and corrected the stopped-provider receipt. This agent did not edit production source or commit.

## Ledger construction

- `rg --files docs/reports/2026-11-07-wall-retry-data` and `sed -n '1,220p' docs/reports/2026-11-07-wall-retry.md`: exit 0; found the existing pristine source/mutant artifacts and copied the already reported test command results above.
- `apply_patch`: created this command ledger only; no source or report changes made.
- Python `Path.read_text()` validation of this ledger's heading and coverage of the absent property path, nonzero result and census stub: exit 0; audit/review ledger checks passed.
