"""Remove retention-walk safety rules in a disposable copy and require tests to fail.

Run from the repository with ``uv run python tools/mutants_retention_repo_walk.py``.
The source defaults to this script's repository, regardless of the working directory;
``--scratch DIR`` chooses the parent of an automatically removed scratch directory.
Tests run serially, with no xdist. Each mutant first runs its focused tests, then
the two complete fix test files if needed. JSON lines record every result, and
the command fails for any survivor, unapplied mutation or infrastructure error.
Use ``--only NAME ...`` to reproduce selected mutants, including their baseline.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GIT, ARCHIVE, RETENTION, TMP = "subfleet/retention_git.py", "subfleet/retention_archive.py", "subfleet/retention.py", "subfleet/attempt_tmp.py"
MUTANTS = [
    ("fallback-removed", GIT, "    if common is None:\n        remaining = timeout", "    if False:\n        remaining = timeout"),
    ("no-repository-at-boundary", GIT, "            if parent.stat().st_dev != device:\n                return None", "            if parent.stat().st_dev != device:\n                return _NO_REPOSITORY"),
    ("head-in-walk-ignored", GIT, '            if os.path.lexists(current / "HEAD"):\n                return None\n', ""),
    ("negative-goes-to-git", GIT, "    if common is _NO_REPOSITORY:\n        return None\n", "    if common is _NO_REPOSITORY:\n        common = None\n"),
    ("sections-unrefused", GIT, '            if not subsection and section not in ({"core", "extensions", *_PLAIN_SECTION_KEYS} if repository\n                                                   else {"core"}):\n                return None\n', ""),
    ("section-keys-unchecked", GIT, "        elif not subsection and key not in _PLAIN_SECTION_KEYS[section]:\n            return None\n", ""),
    ("include-unrefused", GIT, '            if section in {"include", "includeif"}:\n                return None\n', ""),
    ("unknown-extensions-pass", GIT, '            elif key != "partialclone" or not value:\n                return None\n', ""),
    ("extension-boolean-unchecked", GIT, '                if value.lower() not in _BOOLEANS:\n                    return None\n                per_worktree', "                per_worktree"),
    ("version-2-passes", GIT, 'value not in {"0", "1"}', 'value not in {"0", "1", "2"}'),
    ("core-keys-unchecked", GIT, "            elif key not in _PLAIN_CORE_ANY:\n                return None\n", ""),
    ("core-booleans-unchecked", GIT, '                if value.lower() not in _BOOLEANS or (key == "bare" and not repository and value.lower() in _TRUE):\n                    return None\n', "                pass\n"),
    ("worktree-key-unchecked", GIT, '                if not repository or not value or _resolve(common, value) != worktree:\n                    return None\n', "                pass\n"),
    ("config-worktree-unread", GIT, 'if per_worktree and os.path.lexists(admin / "config.worktree") and _plain_config(', 'if False and _plain_config('),
    ("unterminated-quote-passes", GIT, '|"[^"\\\\\\x00-\\x1f]*")\', line)', '|"[^"\\\\\\x00-\\x1f]*"?)\', line)'),
    ("reftable-unrefused", GIT, 'os.path.lexists(common / "reftable")', "False"),
    ("loose-ref-unchecked", GIT, 'os.path.lexists(common / prefix.rstrip("/"))', "False"),
    ("packed-ref-unchecked", GIT, '    return (" " + prefix).encode() not in packed', "    return True"),
    ("packed-limit-ignored", GIT, 'rfs.read_regular(common / "packed-refs", limit=PACKED_REFS_LIMIT)', 'rfs.read_regular(common / "packed-refs")'),
    ("census-deadline-ignored", ARCHIVE, "    if deadline is not None and clock() >= deadline:\n        raise Defer(\"repository discovery deadline\", DEFER_CHANGED_S)\n", ""),
    ("context-drops-deadline", ARCHIVE, "deadline=self.deadline, clock=self.clock)\n        return self.repositories", "deadline=None, clock=self.clock)\n        return self.repositories"),
    ("pass-drops-deadline", RETENTION, "remote_less_history_bytes=remote_less_history_bytes, deadline=deadline)\n        self.sizes", "remote_less_history_bytes=remote_less_history_bytes, deadline=None)\n        self.sizes"),
    ("partial-census-published", ARCHIVE, "            _repository_checkpoint(self.cancel, self.deadline, self.clock)\n            self.repositories = known_repositories(", "            self.repositories = []\n            _repository_checkpoint(self.cancel, self.deadline, self.clock)\n            self.repositories = known_repositories("),
    ("allocation-replay-removed", ARCHIVE, "        if not unchanged:\n", "        if False:\n"),
    ("allocation-casefold-removed", ARCHIVE, 'unicodedata.normalize("NFC", name).casefold() for name in os.listdir(allocated_root)', "name for name in os.listdir(allocated_root)"),
    ("salvage-deadline-removed", ARCHIVE, "            # The listing search over the census answers to the census's bound.\n            _repository_checkpoint(self.cancel, self.deadline, self.clock)\n", ""),
    ("salvage-proof-removed", ARCHIVE, "            if any(rgit.no_refs_under(common, ref) for ref in wanted):\n                continue\n", ""),
    ("salvage-proof-needs-all", ARCHIVE, "            if any(rgit.no_refs_under(common, ref) for ref in wanted):", "            if all(rgit.no_refs_under(common, ref) for ref in wanted):"),
    ("salvage-proof-inverted", ARCHIVE, "            if any(rgit.no_refs_under(common, ref) for ref in wanted):", "            if not any(rgit.no_refs_under(common, ref) for ref in wanted):"),
    ("probe-kind-index", TMP, "{'kind' if job_id is None else '+kind'}", "kind"),
    ("repository-subsections-unchecked", GIT,
     "subsection and (not repository or section not in _PLAIN_SUBSECTIONS)",
     'subsection and (not repository or section in {"core", "extensions"})'),
    ("worktree-subsections-allowed", GIT,
     "subsection and (not repository or section not in _PLAIN_SUBSECTIONS)",
     "subsection and section not in _PLAIN_SUBSECTIONS"),
    ("remote-subsections-refused", GIT, 'frozenset({"remote", "branch", "submodule"})',
     'frozenset({"branch", "submodule"})'),
    ("branch-subsections-refused", GIT, 'frozenset({"remote", "branch", "submodule"})',
     'frozenset({"remote", "submodule"})'),
    ("submodule-subsections-refused", GIT, 'frozenset({"remote", "branch", "submodule"})',
     'frozenset({"remote", "branch"})'),
    ("ambient-listing-unchecked", GIT,
     'config = probe(["config", "--list", "--show-scope"], outside).stdout.decode("utf-8", "surrogateescape")',
     'config = ""'),
    ("ambient-includes-allowed", GIT, '                if key.startswith(("include.", "includeif.")):\n',
     "                if False:\n"),
    ("ambient-unconditional-includes-allowed", GIT,
     'key.startswith(("include.", "includeif."))', 'key.startswith("includeif.")'),
    ("ambient-conditional-includes-allowed", GIT,
     'key.startswith(("include.", "includeif."))', 'key.startswith("include.")'),
    ("ambient-init-removed", GIT, '            probe(["init", "--quiet", str(repository)], outside)\n', ""),
    ("ambient-startup-unchecked", GIT, '            probe(["rev-parse", "--git-common-dir"], repository)\n', ""),
    ("ambient-errors-accepted", GIT,
     "    except (GitError, OSError):\n        return False\n    return True\n",
     "    except (GitError, OSError):\n        return True\n    return True\n"),
    ("ambient-unfiltered-environment", GIT,
     "        return run(args, cwd=cwd, timeout=remaining, cancel=cancel)\n",
     "        return run(args, cwd=cwd, timeout=remaining, cancel=cancel, env=dict(os.environ))\n"),
    ("ambient-outside-cwd-ignored", GIT, "            outside = Path(folder)\n",
     "            outside = Path.cwd()\n"),
    ("ambient-probe-budget-restarted", GIT,
     "        remaining = deadline - time.monotonic()\n", "        remaining = timeout\n"),
    ("ambient-expired-probe-started", GIT, "        if remaining <= 0:\n", "        if False:\n"),
    ("ambient-probe-cancellation-ignored", GIT,
     '        if cancel is not None and cancel.is_set():\n            raise Cancelled("ambient Git config probe cancelled")\n', ""),
    ("ambient-census-check-removed", ARCHIVE,
     "    ambient_plain = rgit.ambient_plain(timeout=ambient_timeout, cancel=cancel)\n",
     "    ambient_plain = True\n"),
    ("ambient-census-gate-removed", ARCHIVE, "            if ambient_plain:\n", "            if True:\n"),
    ("ambient-census-check-repeated", ARCHIVE, "            if ambient_plain:\n",
     "            if rgit.ambient_plain(timeout=timeout, cancel=cancel):\n"),
    ("ambient-fallback-log-removed", ARCHIVE,
     '    if not ambient_plain:\n        logging.getLogger(__name__).info("Repository census uses Git for every workdir: ambient config is not plain")\n', ""),
    ("redirected-ref-store-unchecked", GIT, 'os.path.lexists(common / "commondir")', "False"),
    ("dangling-ref-redirect-unchecked", GIT, 'os.path.lexists(common / "commondir")',
     '(common / "commondir").exists()'),
    ("unicode-ref-prefix-allowed", GIT, "not prefix.isascii() or any(character", "any(character"),
    ("pattern-ref-prefix-allowed", GIT,
     '    if not prefix.isascii() or any(character in prefix for character in "*?[\\\\"):\n',
     "    if not prefix.isascii():\n"),
    ("refs-listing-timeout-unforwarded", GIT,
     '    out = run(["for-each-ref", "--format=%(refname) %(objectname)", prefix], git_dir=common,\n              timeout=timeout, cancel=cancel).stdout.decode("utf-8", "surrogateescape")',
     '    out = run(["for-each-ref", "--format=%(refname) %(objectname)", prefix], git_dir=common,\n              cancel=cancel).stdout.decode("utf-8", "surrogateescape")'),
    ("salvage-listing-time-left-ignored", ARCHIVE,
     "            timeout = 120 if self.deadline is None else min(120, self.deadline - self.clock())\n",
     "            timeout = 120\n"),
    ("salvage-listing-timeout-cap-removed", ARCHIVE,
     "            timeout = 120 if self.deadline is None else min(120, self.deadline - self.clock())\n",
     "            timeout = 120 if self.deadline is None else self.deadline - self.clock()\n"),
    ("salvage-listing-failure-deadline-ignored", ARCHIVE,
     "            except (rgit.GitError, OSError):\n                _repository_checkpoint(self.cancel, self.deadline, self.clock)\n                self.salvage_listings[key] = {}\n",
     "            except (rgit.GitError, OSError):\n                self.salvage_listings[key] = {}\n"),
    ("salvage-expired-failure-cached", ARCHIVE,
     "            except (rgit.GitError, OSError):\n                _repository_checkpoint(self.cancel, self.deadline, self.clock)\n                self.salvage_listings[key] = {}\n",
     "            except (rgit.GitError, OSError):\n                self.salvage_listings[key] = {}\n                _repository_checkpoint(self.cancel, self.deadline, self.clock)\n"),
    ("salvage-ordinary-failure-uncached", ARCHIVE,
     "                self.salvage_listings[key] = {}\n", "                raise\n"),
]
TESTS = ["tests/unit/test_retention_git_common_dir.py", "tests/unit/test_retention_repository_walk.py"]

EQUIVALENTS = {
    "include-unrefused": (
        "The plain-section allowlist rejects include/includeIf without subsections; "
        "the subsection allowlist rejects them with subsections. Removing the explicit "
        "include guard cannot admit either form."
    ),
}

# Focus each mutation on its rule. A survivor still runs all of TESTS, so these
# selections improve cost without hiding a kill elsewhere in the fix suite.
FOCUSED = {
    "fallback-removed": (TESTS[0], "generated_matrix"),
    "no-repository-at-boundary": (TESTS[0], "filesystem_boundary"),
    "head-in-walk-ignored": (TESTS[0], "generated_matrix"),
    "negative-goes-to-git": (TESTS[0], "no_repository_up_to_the_root"),
    "sections-unrefused": (TESTS[0], "config_line_matches_real_git"),
    "section-keys-unchecked": (TESTS[0], "config_line_matches_real_git"),
    "include-unrefused": (TESTS[0], "doubtful_config"),
    "unknown-extensions-pass": (TESTS[0], "config_line_matches_real_git"),
    "extension-boolean-unchecked": (TESTS[0], "config_line_matches_real_git"),
    "version-2-passes": (TESTS[0], "config_line_matches_real_git"),
    "core-keys-unchecked": (TESTS[0], "config_line_matches_real_git"),
    "core-booleans-unchecked": (TESTS[0], "config_line_matches_real_git"),
    "worktree-key-unchecked": (TESTS[0], "unusual_metadata"),
    "config-worktree-unread": (TESTS[0], "config_worktree_matches_real_git"),
    "unterminated-quote-passes": (TESTS[0], "config_line_matches_real_git"),
    "reftable-unrefused": (TESTS[0], "leaves_a_reftable_store"),
    "loose-ref-unchecked": (TESTS[0], "no_refs_under_never_contradicts_git"),
    "packed-ref-unchecked": (TESTS[0], "no_refs_under_takes_a_whole_ref_name"),
    "packed-limit-ignored": (TESTS[0], "leaves_an_oversized_or_unreadable_store"),
    "census-deadline-ignored": (TESTS[1], "known_repositories_deadline_during_registration"),
    "context-drops-deadline": (TESTS[1], "context_does_not_publish_incomplete"),
    "pass-drops-deadline": (TESTS[1], "discovery_deadline_defers_without_partial_retirement"),
    "partial-census-published": (TESTS[1], "context_does_not_publish_incomplete"),
    "allocation-replay-removed": (TESTS[1], "allocation_created_during_census"),
    "allocation-casefold-removed": (TESTS[1], "allocation_snapshot_aliases"),
    "salvage-deadline-removed": (TESTS[1], "salvage_listing_stops_at_the_deadline"),
    "salvage-proof-removed": (TESTS[1], "salvage_search_lists_only"),
    "salvage-proof-needs-all": (TESTS[1], "salvage_search_skips_a_repository"),
    "salvage-proof-inverted": (TESTS[1], "salvage_search_lists_only"),
    "probe-kind-index": (TESTS[1], "one_jobs_probe_rows_use_the_job_index"),
    "repository-subsections-unchecked": (TESTS[0], "doubtful_config_goes_to_git"),
    "worktree-subsections-allowed": (TESTS[0], "config_worktree_subsections_are_always_left_to_git"),
    "remote-subsections-refused": (TESTS[0], "safe_config_needs_no_process"),
    "branch-subsections-refused": (TESTS[0], "safe_config_needs_no_process"),
    "submodule-subsections-refused": (TESTS[0], "safe_config_needs_no_process"),
    "ambient-listing-unchecked": (TESTS[1], "ambient_config_matches_git"),
    "ambient-includes-allowed": (TESTS[1], "ambient_config_matches_git"),
    "ambient-unconditional-includes-allowed": (TESTS[1], "ambient_config_matches_git"),
    "ambient-conditional-includes-allowed": (TESTS[1], "ambient_config_matches_git"),
    "ambient-init-removed": (TESTS[1], "ambient_config_matches_git"),
    "ambient-startup-unchecked": (TESTS[1], "ambient_guard_has_constant_process_cost"),
    "ambient-errors-accepted": (TESTS[1], "ambient_config_matches_git"),
    "ambient-unfiltered-environment": (TESTS[1], "ambient_guard_uses_retention_environment"),
    "ambient-outside-cwd-ignored": (TESTS[1], "ambient_guard_has_constant_process_cost"),
    "ambient-probe-budget-restarted": (TESTS[1], "ambient_guard_requires_each_probe_and_shares_its_timeout"),
    "ambient-expired-probe-started": (TESTS[1], "ambient_guard_starts_no_probe_after_timeout_or_cancellation"),
    "ambient-probe-cancellation-ignored": (TESTS[1], "ambient_guard_starts_no_probe_after_timeout_or_cancellation"),
    "ambient-census-check-removed": (TESTS[1], "ambient_guard_has_constant_process_cost"),
    "ambient-census-gate-removed": (TESTS[1], "ambient_config_matches_git"),
    "ambient-census-check-repeated": (TESTS[1], "ambient_guard_has_constant_process_cost"),
    "ambient-fallback-log-removed": (TESTS[1], "ambient_config_matches_git"),
    "redirected-ref-store-unchecked": (TESTS[0], "leaves_a_redirected_common_directory"),
    "dangling-ref-redirect-unchecked": (TESTS[0], "refuses_even_a_dangling_commondir"),
    "unicode-ref-prefix-allowed": (TESTS[0], "leaves_unicode_normalization"),
    "pattern-ref-prefix-allowed": (TESTS[0], "leaves_pattern_matching"),
    "refs-listing-timeout-unforwarded": (TESTS[1], "slow_git_listing_obeys_real_deadline"),
    "salvage-listing-time-left-ignored": (TESTS[1], "salvage_listing_timeout_is_remaining_time"),
    "salvage-listing-timeout-cap-removed": (TESTS[1], "salvage_listing_timeout_is_remaining_time"),
    "salvage-listing-failure-deadline-ignored": (TESTS[1], "failed_salvage_listing_only_caches"),
    "salvage-expired-failure-cached": (TESTS[1], "failed_salvage_listing_only_caches"),
    "salvage-ordinary-failure-uncached": (TESTS[1], "failed_salvage_listing_only_caches"),
}


def run(copy: Path, env: dict[str, str], tests: list[str]) -> tuple[int, str, float]:
    started = time.perf_counter()
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests],
                          cwd=copy, env=env, capture_output=True, text=True)
    lines = [l for l in done.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))]
    output = (done.stdout or done.stderr).strip().splitlines()
    note = lines[0] if lines else (output[-1] if output else "no pytest output")
    return done.returncode, note[:200], round(time.perf_counter() - started, 1)


def emit(**result) -> None:
    print(json.dumps(result), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--scratch", type=Path, default=None)
    parser.add_argument("--only", nargs="+", default=[])
    args = parser.parse_args()
    source_root = args.repository.resolve()
    only = set(args.only)
    unknown = only - {name for name, *_ in MUTANTS}
    if unknown:
        parser.error("unknown mutants: " + ", ".join(sorted(unknown)))

    with tempfile.TemporaryDirectory(prefix="retention-mutants-", dir=args.scratch) as folder:
        scratch = Path(folder).resolve()
        copy = scratch / "copy"
        copy.mkdir()
        shutil.copytree(source_root / "subfleet", copy / "subfleet", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(source_root / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__", "e2e"))
        shutil.copy2(source_root / "pyproject.toml", copy / "pyproject.toml")
        (scratch / "tmp").mkdir()
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(copy),
               "TMPDIR": str(scratch / "tmp") + "/", "UV_CACHE_DIR": str(scratch / "tmp" / "uv-cache"),
               "HYPOTHESIS_STORAGE_DIRECTORY": str(scratch / "hyp")}
        where = subprocess.run([sys.executable, "-c", "import subfleet; print(subfleet.__file__)"],
                               cwd=copy, env=env, capture_output=True, text=True)
        if where.returncode or not where.stdout.strip().startswith(str(copy) + os.sep):
            emit(name="import-check", rc=where.returncode, note="tests must import the scratch package")
            return 1

        rc, note, seconds = run(copy, env, TESTS)
        emit(name="baseline", rc=rc, note=note, seconds=seconds)
        if rc:
            return 1
        killed = equivalent = survived = unapplied = errors = 0
        for name, file, old, new in MUTANTS:
            if only and name not in only:
                continue
            path = copy / file
            source = path.read_text()
            if source.count(old) != 1:
                emit(name=name, killed=False, rc=None, note=f"pattern matched {source.count(old)} times")
                unapplied += 1
                continue
            path.write_text(source.replace(old, new))
            try:
                selection = FOCUSED.get(name)
                tests = [selection[0], "-k", selection[1]] if selection else TESTS
                rc, note, seconds = run(copy, env, tests)
                if selection and rc == 0:
                    rc, note, complete_seconds = run(copy, env, TESTS)
                    seconds = round(seconds + complete_seconds, 1)
            finally:
                path.write_text(source)
            equivalence = EQUIVALENTS.get(name) if rc == 0 else None
            result = dict(name=name, killed=rc == 1, rc=rc, note=note, seconds=seconds)
            if equivalence:
                result.update(equivalent=True, reason=equivalence)
            emit(**result)
            killed += rc == 1
            equivalent += bool(equivalence)
            survived += rc == 0 and not equivalence
            errors += rc not in (0, 1)
        emit(name="summary", killed=killed, equivalent=equivalent, survived=survived, unapplied=unapplied, errors=errors)
        return int(bool(survived or unapplied or errors))


if __name__ == "__main__":
    raise SystemExit(main())
