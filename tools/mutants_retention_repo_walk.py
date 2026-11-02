"""D-RET-WALK1 mutants: remove each safety rule from a scratch copy and require a test to fail.

Usage: .venv/bin/python mutants.py <lane> <scratch>. The lane itself stays untouched. Each mutant runs the two
fix test files with -x, serially. Prints one JSON line per mutant; a survivor is a test gap or an equivalent mutant,
and each needs a note in the report.
"""
import json, os, shutil, subprocess, sys, time
from pathlib import Path

lane, scratch = Path(sys.argv[1]), Path(os.path.realpath(sys.argv[2]))
assert "/sf-" in str(scratch)
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
    ("reftable-unrefused", GIT, 'os.path.lexists(common / "reftable") or ', ""),
    ("loose-ref-unchecked", GIT, ' or os.path.lexists(common / prefix.rstrip("/"))', ""),
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
]
TESTS = ["tests/unit/test_retention_git_common_dir.py", "tests/unit/test_retention_repository_walk.py"]
only = set(sys.argv[3:])
copy = scratch / "copy"
if copy.exists():
    shutil.rmtree(copy)
copy.mkdir(parents=True)
shutil.copytree(lane / "subfleet", copy / "subfleet", ignore=shutil.ignore_patterns("__pycache__"))
shutil.copytree(lane / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__", "e2e"))
shutil.copy2(lane / "pyproject.toml", copy / "pyproject.toml")
env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(scratch / "tmp") + "/", "HYPOTHESIS_STORAGE_DIRECTORY": str(scratch / "hyp")}
(scratch / "tmp").mkdir(exist_ok=True)
python = str(lane / ".venv/bin/python")
where = subprocess.run([python, "-c", "import subfleet; print(subfleet.__file__)"], cwd=copy, env=env, capture_output=True, text=True).stdout.strip()
assert where.startswith(str(copy)), where


def run():
    started = time.perf_counter()
    done = subprocess.run([python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS], cwd=copy, env=env, capture_output=True, text=True)
    lines = [l for l in done.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))]
    return done.returncode, (lines[0][:120] if lines else done.stdout.strip().splitlines()[-1][:120] if done.stdout.strip() else ""), round(time.perf_counter() - started, 1)


rc, note, seconds = run()
print(json.dumps(dict(name="baseline", rc=rc, note=note, seconds=seconds)), flush=True)
assert rc == 0, note
for name, file, old, new in MUTANTS:
    if only and name not in only:
        continue
    path = copy / file
    source = path.read_text()
    if source.count(old) != 1:
        print(json.dumps(dict(name=name, rc=None, note=f"pattern matched {source.count(old)} times")), flush=True)
        continue
    path.write_text(source.replace(old, new))
    try:
        rc, note, seconds = run()
    finally:
        path.write_text(source)
    print(json.dumps(dict(name=name, killed=rc != 0, rc=rc, note=note, seconds=seconds)), flush=True)
