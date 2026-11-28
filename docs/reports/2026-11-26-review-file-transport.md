# Review jobs can run the bare and submodule worktree cases (#340)

`date -u +%FT%TZ` recorded `2026-11-26T16:20:04Z` before checkout verification. `git rev-parse --short=9 HEAD` printed `a4d21501e`. `git branch --show-current` printed nothing, so the work proceeded on the assigned detached HEAD.

## Changes

| Lines in `tests/fake/test_unrun_worktrees.py` | Change |
| --- | --- |
| 949–959 | `bare_copy` reads the source's symbolic HEAD, initializes a bare repository with that branch, copies the object database and recreates the source branches and tags. `update-ref` uses `--git-dir` so the explicit-bare fixture can write the refs. |
| 962–970 | `submodule_copy` copies the checkout and its Git directory, writes `.gitmodules`, stages the gitlink, absorbs the Git directory into the superproject and initializes the submodule's local configuration. |
| 977 | The bare-checkout case calls `bare_copy`. |
| 988–1015 | The new regression applies `GIT_DENIAL`, checks that a local clone fails, tags the source to exercise tag copying, then verifies the bare and submodule layouts. |
| 1443, 1448, 1457 | The `shaped` docstring records the local setup. The submodule and explicit-bare parameters call the new helpers. |

Review and gate-review launches receive the pinned environment through `publishing_denied`, `pinned_environment` and `protect_launch` in `subfleet/review_publish.py:67–73,181–199`. The empty `GIT_ALLOW_PROTOCOL` at line 50 takes precedence over Git's `protocol.file.allow` setting. Lines 54–55 supply `GIT_CONFIG_PARAMETERS`. Line 198 puts the wrapper first on `PATH`, and `wrapper_main` reapplies `GIT_DENIAL` at line 103 before executing Git. The inherited environment reaches the fixture calls through `tests/unit/test_salvage.py:15–17`. Git 2.55.0 refused both original setup commands under that environment with `fatal: transport 'file' not allowed`.

The fixture shapes use local objects and refs. Clone-created origin URLs, remote-tracking refs and branch tracking are outside these cases. Admission uses `worktree add --detach` at `subfleet/daemon.py:6615`, and give-back reads the worktree registration.

## Invariants

1. **The restriction is unchanged.** Staging was denied, so `git diff --name-only a4d21501e..HEAD` was empty. The corresponding worktree check, `git diff --name-only a4d21501e` plus `git ls-files --others --exclude-standard`, listed only `tests/fake/test_unrun_worktrees.py` and this report. In the test diff, the only references to the denial variables are the regression's `GIT_DENIAL` import and its `monkeypatch.setenv` loop. The diff adds no `delenv` of those variables or `PATH`. C-12.4 continues to pin `GIT_ALLOW_PROTOCOL`, `GIT_CONFIG_PARAMETERS` and `SUBFLEET_PUBLISH_DENIED`. Item 11 of `docs/reports/2026-10-14-review-needs-shell.md` documents the resulting fetch refusal.
2. **Same shapes.** The regression checks the bare repository flag, `refs/heads/task/example` as HEAD, equality of branch and tag refs including an annotated tag, and `git fsck`. It checks the submodule gitfile, `core.worktree`, source URL, active flag and mode-`160000` gitlink at the source HEAD. The four fixture cases exercise checkout removal, and the two admission cases verify that Git refuses direct entry under both shapes.
3. **No transport.** The five original cases and the new regression passed under both launch environments after the helper replacement. The regression's clone refusal verifies that file transport is refused during its setup checks.
4. **Same assertions.** A byte comparison against the base verified the existing test assertions and both `worktree add` commands. The existing bare test differs by its setup call. Both parameterized test bodies compare equal to the base. The full test-file diff follows below.

## Test commands and results

The runs used Python 3.15.2, pytest 9.1.1, xdist 3.8.0 and Git 2.55.0. This is the command template for each run. `k` is the row number.

```sh
export TMPDIR="$PWD/build/rft-build-r1"
export UV_CACHE_DIR="$TMPDIR/uv-cache"
export UV_PROJECT_ENVIRONMENT="$TMPDIR/venv"
export UV_PYTHON=3.15
P="$(uv run --no-sync --offline python -c 'from subfleet.review_publish import GIT_DENIAL as d; print(d["GIT_CONFIG_PARAMETERS"])')"
BASE_SEL='bare_repositorys_checkout or git_cannot_enter'
SEL='bare_repositorys_checkout or git_cannot_enter or setups_need_no_transport'
```

```sh
uv run --no-sync --offline --with 'pytest-xdist>=3.6' python -m pytest \
  -n 4 --timeout=540 --basetemp "$TMPDIR/pytest-$k" -q <arguments>

env GIT_ALLOW_PROTOCOL= GIT_CONFIG_PARAMETERS="$P" \
  uv run --no-sync --offline --with 'pytest-xdist>=3.6' python -m pytest \
  -n 4 --timeout=540 --basetemp "$TMPDIR/pytest-$k" -q <arguments>
```

The first command is ambient and the second is restricted. Runs 1–2 used `tests/fake/test_unrun_worktrees.py -k "$BASE_SEL"`. Runs 3–10 used `tests/fake/test_unrun_worktrees.py -k "$SEL"`. Runs 3–4 and 7–10 also used `PYTEST_ADDOPTS=--showlocals` to show captured Git stderr and exception details. Run 11 used the eight modules below. Run 12 used `tests/fake/test_unrun_worktrees.py` without a selection and `PYTEST_ADDOPTS="-o tmp_path_retention_policy=failed"`.

```text
tests/fake/test_unrun_worktrees.py
tests/fake/test_admission_tree_thrash.py
tests/fake/test_admission_tree_thrash_head.py
tests/unit/test_salvage.py
tests/fake/test_state_contract.py
tests/unit/test_invariants_index.py
tests/unit/test_test_module_names.py
tests/unit/test_handler_refusals.py
```

| Run | Stage | Environment | Pass | Fail | Error | Skip | Basetemp KiB | Pytest summary |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | Base | Restricted | 0 | 1 | 4 | 0 | 3,568 | `1 failed, 4 errors in 57.72s` |
| 2 | Base | Ambient | 5 | 0 | 0 | 0 | 4,344 | `5 passed in 26.77s` |
| 3 | Original-command helpers + regression | Ambient | 5 | 1 | 0 | 0 | 4,444 | `1 failed, 5 passed in 45.69s` |
| 4 | Original-command helpers + regression | Restricted | 0 | 2 | 4 | 0 | 3,692 | `2 failed, 4 errors in 58.71s` |
| 5 | Local helpers | Restricted | 6 | 0 | 0 | 0 | 4,836 | `6 passed in 68.47s (0:01:08)` |
| 6 | Local helpers | Ambient | 6 | 0 | 0 | 0 | 4,816 | `6 passed in 79.79s (0:01:19)` |
| 7 | M1 | Restricted | 2 | 2 | 2 | 0 | 4,372 | `2 failed, 2 passed, 2 errors in 36.57s` |
| 8 | M2 | Restricted | 4 | 0 | 2 | 0 | 4,684 | `4 passed, 2 errors in 28.92s` |
| 9 | M3 | Restricted | 3 | 3 | 0 | 0 | 4,708 | `3 failed, 3 passed in 64.31s (0:01:04)` |
| 10 | M4 | Restricted | 5 | 1 | 0 | 0 | 4,808 | `1 failed, 5 passed in 50.92s` |
| 11 | Eight modules | Ambient | 604 | 0 | 0 | 0 | 194,448 | `604 passed in 645.74s (0:10:45)` |
| 12 | Whole worktree module | Restricted | 99 | 0 | 0 | 0 | 1,192 | `99 passed in 239.36s (0:03:59)` |

Every basetemp was measured with `du -sk` and removed before the next run after checking its real parent and `pytest-` name.
The largest was 194,448 KiB.

### Pre-run disk and load

Each `df -H /` row was for `/dev/disk3s1s1`, mounted on `/`.
Columns follow the command output. The uptime rows include the host's local clock.

| Run | UTC | Size | Used | Available | Capacity | iused | ifree | %iused | `uptime` |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 2026-11-26T16:24:20Z | 3996G | 14G | 76G | 16% | 487k | 743M | 0% | `17:24  1 user, load averages: 36.55 48.02 56.66` |
| 2 | 2026-11-26T16:26:02Z | 3996G | 14G | 76G | 16% | 487k | 741M | 0% | `17:26  1 user, load averages: 62.02 51.26 56.79` |
| 3 | 2026-11-26T16:28:17Z | 3996G | 14G | 75G | 16% | 487k | 740M | 0% | `17:28  1 user, load averages: 51.36 48.89 54.93` |
| 4 | 2026-11-26T16:29:28Z | 3996G | 14G | 76G | 16% | 487k | 740M | 0% | `17:29  1 user, load averages: 75.29 56.16 57.17` |
| 5 | 2026-11-26T16:33:21Z | 3996G | 14G | 75G | 16% | 487k | 734M | 0% | `17:33  1 user, load averages: 59.66 52.89 55.21` |
| 6 | 2026-11-26T16:34:56Z | 3996G | 14G | 75G | 16% | 487k | 735M | 0% | `17:34  1 user, load averages: 69.89 58.67 57.22` |
| 7 | 2026-11-26T16:36:59Z | 3996G | 14G | 75G | 16% | 487k | 732M | 0% | `17:36  1 user, load averages: 41.54 55.42 56.39` |
| 8 | 2026-11-26T16:37:57Z | 3996G | 14G | 75G | 16% | 487k | 731M | 0% | `17:37  1 user, load averages: 28.27 49.40 54.09` |
| 9 | 2026-11-26T16:38:38Z | 3996G | 14G | 75G | 16% | 487k | 735M | 0% | `17:38  1 user, load averages: 24.32 45.75 52.54` |
| 10 | 2026-11-26T16:39:56Z | 3996G | 14G | 75G | 16% | 487k | 731M | 0% | `17:39  1 user, load averages: 25.81 40.90 50.05` |
| 11 | 2026-11-26T16:41:18Z | 3996G | 14G | 75G | 16% | 487k | 735M | 0% | `17:41  1 user, load averages: 51.40 45.59 51.02` |
| 12 | 2026-11-26T16:53:16Z | 3996G | 14G | 75G | 16% | 487k | 732M | 0% | `17:53  1 user, load averages: 37.57 49.44 53.89` |

### Fail-first output

The base restricted run's default traceback displayed the Git command and exit status 128. A direct replay of both original Git commands in synthetic repositories under the same environment exposed the first stderr line, `fatal: transport 'file' not allowed`.

With the original commands behind the helpers, the ambient run failed at regression part (b). The restricted run also failed in the bare test and errored during all four `shaped` setups. The first denial line in both pytest outputs was `stderr     = "fatal: transport 'file' not allowed\n"`.

### Mutants

The regression is `test_the_bare_and_submodule_setups_need_no_transport`. The bare integration case is `test_a_job_from_a_bare_repositorys_checkout_runs_after_the_checkout_goes`. The two parameterized integration cases are `test_a_job_runs_after_a_checkout_whose_repository_git_cannot_enter_goes` and `test_a_give_back_takes_the_registration_from_a_repository_git_cannot_enter`.

| Mutant | Cases that caught it | Evidence |
| --- | --- | --- |
| M1 omits the object copy | Regression (b), bare case, both `explicit bare` cases | `update-ref` failed on a ref whose object was missing. |
| M2 uses `git -C <bare>` for ref updates | Both `explicit bare` cases | Git reported `safe.bareRepository is 'explicit'`. |
| M3 omits `submodule absorbgitdirs` | Regression (c), both `submodule` cases | The regression still found a directory at `sm/.git`. The admission case failed to run the job and the give-back case found the expected registration missing. |
| M4 omits `submodule init` | Regression (c) | `git config submodule.sm.url` exited 1. |

Every mutant was caught. After each run the saved transport-free file was restored, and `git diff --no-index --quiet` against its snapshot confirmed a byte-for-byte match.


## Limits and deviations


The whole-module restricted run reported no additional failing cases.

### Other transport-dependent suites

The base grep for quoted `clone` and `submodule add` returned 41 matches outside this module. They include 35 transport calls in 18 files and six textual matches.

| File | Calls |
| --- | ---: |
| `tests/fake/test_retention_dependency_review_r1.py` | 2 |
| `tests/fake/test_retention_dependency_review_r11.py` | 2 |
| `tests/fake/test_retention_dependency_review_r12.py` | 1 |
| `tests/fixtures/retention_fs_bc108d3ad.py` | 0 |
| `tests/unit/retention_delete_world.py` | 2 |
| `tests/unit/retention_world.py` | 1 |
| `tests/unit/test_host_push_admission.py` | 4 |
| `tests/unit/test_retention_81_notes.py` | 2 |
| `tests/unit/test_retention_archive.py` | 9 |
| `tests/unit/test_retention_archive_properties.py` | 1 |
| `tests/unit/test_retention_dependency_units.py` | 1 |
| `tests/unit/test_retention_git_common_dir.py` | 1 |
| `tests/unit/test_retention_location_readable.py` | 1 |
| `tests/unit/test_retention_location_review_r13.py` | 1 |
| `tests/unit/test_retention_locations.py` | 1 |
| `tests/unit/test_retention_ordinary_trees.py` | 0 |
| `tests/unit/test_retention_packed_retirement_r2.py` | 1 |
| `tests/unit/test_retention_salvage_properties.py` | 2 |
| `tests/unit/test_retention_sweep_race.py` | 1 |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 2 |

`retention_world.py:117–118` also pushes and fetches over its local transport. The depth and filter cases in `test_host_push_admission.py` explicitly exercise `file://`.

### Environment, footprint and delivery

- Creating `$(getconf DARWIN_USER_TEMP_DIR)/rft-build-r1` was denied. The pre-existing `uv-cache` in that temp root was readable, and its lock and cache metadata were read-only in the sandbox. I copied it into `build/rft-build-r1/uv-cache` and used the workspace fallback for all runs. The venv was absent in the detached checkout. The initial import probe and the offline sync with Python 3.13 failed because the required wheel for that interpreter was uncached. The existing Python 3.15.2 interpreter and the copied cache supported offline sync and xdist.
- The baseline pytest command's default traceback hid the shared helper's captured stderr. The direct Git replay recorded the refusal. Later fail-first and mutation runs used `--showlocals` for the captured diagnostics.
- Run 11 retained 194,448 KiB in basetemp. A measurement of the whole job directory during that run reached 250,232 KiB, exceeding the 200 MB scratch cap. Run 12 used `PYTEST_ADDOPTS="-o tmp_path_retention_policy=failed"` so pytest removed each passing test's fixture directory at teardown. The explicit basetemp was still measured and cleaned after the run.
- Staging for each intended commit failed with `fatal: Unable to create '/Users/maxghenis/subfleet-v2/.git/worktrees/20261126-171629-rft-build-r1/index.lock': Operation not permitted`. The original-command and local-command stages were tested in the specified order in the working tree. The finished test file and report are available for Subfleet salvage. `git log --oneline a4d21501e..HEAD` was empty.
- The staging denial left HEAD at the base. For mutant cleanup I restored the saved transport-free file and checked it with `git diff --no-index --quiet` after each mutation. Restoring from HEAD would have restored the original transport-dependent tests.
- The fallback job directory, cache copy, venv and generated test caches were removed from the workspace. The pre-existing cache in the Darwin temp root remains in place. I left no job-created folders or files outside the worktree.

## Test-file diff

With HEAD locked at the base, the diff below comes from `git diff a4d21501e -- tests/fake/test_unrun_worktrees.py`.

```diff
diff --git a/tests/fake/test_unrun_worktrees.py b/tests/fake/test_unrun_worktrees.py
index 4147ecb24..07148f286 100644
--- a/tests/fake/test_unrun_worktrees.py
+++ b/tests/fake/test_unrun_worktrees.py
@@ -946,11 +946,35 @@ def test_a_job_from_a_subfolder_starts_there_after_the_checkout_goes(state_daemo
     assert (daemon.root / "worktrees" / job_id / "pkg" / "module.txt").is_file()


+def bare_copy(repo: Path, bare: Path) -> Path:
+    """Copy branches, tags, objects and HEAD locally because review jobs refuse file transport (#340)."""
+    branch = git(repo, "symbolic-ref", "HEAD").removeprefix("refs/heads/")
+    git(bare.parent, "init", "-q", "--bare", "-b", branch, str(bare))
+    shutil.copytree(repo / ".git" / "objects", bare / "objects", dirs_exist_ok=True)
+    refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags")
+    for line in refs.splitlines():
+        name, oid = line.split()
+        # The explicit bare fixture forbids discovering this repository with -C.
+        git(bare.parent, f"--git-dir={bare}", "update-ref", name, oid)
+    return bare
+
+
+def submodule_copy(sup: Path, repo: Path, name: str = "sm") -> Path:
+    """Build the submodule layout with local commands for review jobs (#340)."""
+    checkout = sup / name
+    shutil.copytree(repo, checkout, symlinks=True)
+    (sup / ".gitmodules").write_text(f'[submodule "{name}"]\n\tpath = {name}\n\turl = {repo}\n')
+    git(sup, "-c", "advice.addEmbeddedRepo=false", "add", ".gitmodules", name)
+    git(sup, "submodule", "absorbgitdirs", name)
+    git(sup, "submodule", "init", name)
+    return checkout
+
+
 def test_a_job_from_a_bare_repositorys_checkout_runs_after_the_checkout_goes(state_daemon, repo, tmp_path,  # noqa: F811
                                                                              offers):
     daemon, harness = state_daemon
     bare = tmp_path / "bare.git"
-    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
+    bare_copy(repo, bare)
     checkout = tmp_path / "bare-checkout"
     git(bare, "worktree", "add", "-q", "-b", "session/bare", str(checkout))
     close_every_lane(daemon)
@@ -961,6 +985,36 @@ def test_a_job_from_a_bare_repositorys_checkout_runs_after_the_checkout_goes(sta
     assert job_id in registered(bare)


+def test_the_bare_and_submodule_setups_need_no_transport(repo, tmp_path, monkeypatch):
+    from subfleet.review_publish import GIT_DENIAL
+
+    for name, value in GIT_DENIAL.items():
+        monkeypatch.setenv(name, value)
+    with pytest.raises(subprocess.CalledProcessError) as exc:
+        git(tmp_path, "clone", "-q", "--bare", str(repo), str(tmp_path / "cloned.git"))
+    assert "transport 'file' not allowed" in exc.value.stderr
+
+    git(repo, "tag", "-a", "fixture-tag", "-m", "fixture tag")
+    bare = bare_copy(repo, tmp_path / "bare.git")
+    assert git(tmp_path, f"--git-dir={bare}", "rev-parse", "--is-bare-repository") == "true"
+    assert git(tmp_path, f"--git-dir={bare}", "symbolic-ref", "HEAD") == "refs/heads/task/example"
+    assert git(tmp_path, f"--git-dir={bare}", "for-each-ref", "--format=%(refname) %(objectname)") == git(
+        repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags"
+    )
+    git(tmp_path, f"--git-dir={bare}", "fsck", "--no-progress")
+
+    sup = tmp_path / "sup"
+    sup.mkdir()
+    git(sup, "init", "-q", "-b", "main")
+    checkout = submodule_copy(sup, repo)
+    modules = sup / ".git" / "modules" / "sm"
+    assert (checkout / ".git").read_text().strip() == "gitdir: ../.git/modules/sm"
+    assert git(tmp_path, f"--git-dir={modules}", "config", "core.worktree") == "../../../sm"
+    assert git(sup, "config", "submodule.sm.url") == str(repo)
+    assert git(sup, "config", "submodule.sm.active") == "true"
+    assert git(sup, "ls-files", "-s", "sm").split()[:2] == ["160000", git(repo, "rev-parse", "HEAD")]
+
+
 def test_a_tree_cut_from_the_repository_is_the_tree_the_checkout_would_cut(state_daemon, repo, linked,  # noqa: F811
                                                                            offers):
     """Differential: the two places `_tree_git` can send the cut agree on HEAD, a detached
@@ -1386,12 +1440,12 @@ def test_a_recorded_repository_moved_away_reads_as_no_record(state_daemon, repo,
 def shaped(request, repo, tmp_path, monkeypatch):
     """A checkout, and a common directory git refuses to run in once that checkout has gone: a
     submodule's enters its `core.worktree`, and a bare repository is refused under
-    `safe.bareRepository=explicit`."""
+    `safe.bareRepository=explicit`. Both repositories are built without file transport."""
     if request.param == "submodule":
         sup = tmp_path / "sup"
         sup.mkdir()
         git(sup, "init", "-q", "-b", "main")
-        git(sup, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(repo), "sm")
+        submodule_copy(sup, repo)
         git(sup, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "sm")
         return sup / "sm", sup / ".git" / "modules" / "sm"
     home = tmp_path / "home"
@@ -1400,7 +1454,7 @@ def shaped(request, repo, tmp_path, monkeypatch):
     monkeypatch.setenv("HOME", str(home))
     monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
     bare = tmp_path / "bare.git"
-    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
+    bare_copy(repo, bare)
     checkout = tmp_path / "bare-checkout"
     subprocess.run(["git", f"--git-dir={bare}", "worktree", "add", "-q", "-b", "session/bare", str(checkout)], check=True)
     return checkout, bare
```
