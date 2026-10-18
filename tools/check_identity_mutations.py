"""Run identity mutations serially; restore source bytes after every run.

Run with the development Python from the repository root. All run evidence and
Hypothesis caches live in a fresh Darwin user temporary directory and are
removed when the check ends. A mutation counts as killed only on pytest exit 1.
"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/fake/test_canonical_identity.py"
RECHECK = '''                        if native_session and job["kind"] in ("resume", "revive"):
                            # C-26.13: public open may have bound it during route
                            # preparation, after the early ownership check.
                            try:
                                self._refuse_conversation_session(native_session, job["kind"])
                            except AdapterError as exc:
                                self._fail_queued(job, str(exc) + (f"; fix: {exc.fix}" if exc.fix else ""), rc=exc.code)
                                status = "settled"
                                break
'''
MUTATIONS = (
    ("drop casefold", "subfleet/folders.py", 'unicodedata.normalize("NFD", comparable).casefold()',
     'unicodedata.normalize("NFD", comparable)',
     "test_minimized_absent_output_alias_has_one_owner"),
    ("drop canonical normalization", "subfleet/folders.py",
     'unicodedata.normalize("NFD", unicodedata.normalize("NFD", comparable).casefold())', 'comparable.casefold()',
     "test_minimized_absent_output_alias_has_one_owner"),
    ("raw native lease key", "subfleet/daemon.py",
     "resource_leases.native_key(provider, native_session)", 'f"native:{provider}:{native_session}"',
     "test_native_aliases_collide_and_distinct_uuids_admit"),
    ("remove reservation recheck", "subfleet/daemon.py", RECHECK, "",
     "test_binding_during_preparation_is_refused_inside_reservation"),
    ("drop OSError fallback", "subfleet/folders.py", "return identity_fallback(raw, exc)", "raise",
     "test_identity_failure_falls_back_to_exact_string_through_all_operations"),
    ("drop superseded-export release", "subfleet/daemon.py",
     '            tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (job_id, aid))',
     '            if not export_error:\n                tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (job_id, aid))',
     "test_oldest_legacy_alias_exports_and_superseded_releases_all_leases"),
    ("drop per-pass memo", "subfleet/daemon.py", "identities=output_identities", "identities=None",
     "test_identity_work_is_linear_per_admission_pass_and_fresh_next_pass"),
    ("drop public-open lease check", "subfleet/conversations/service.py", '            if live:\n                raise ConversationError("native-held"',
     '            if False:\n                raise ConversationError("native-held"',
     "test_public_open_between_reservation_and_launch_refuses_live_native_lease"),
    ("drop canonical caseless rule", "subfleet/folders.py",
     'unicodedata.normalize("NFD", unicodedata.normalize("NFD", comparable).casefold())',
     'unicodedata.normalize("NFC", comparable).casefold()',
     "test_conservative_rule_also_guards_absent_names"),
    ("drop default-ignorable equivalence", "subfleet/folders.py", '_DEFAULT_IGNORABLES.sub("", spelled)', 'spelled',
     "test_conservative_rule_also_guards_absent_names"),
    ("drop dotless-i equivalence", "subfleet/folders.py", '.replace("ı", "i")', '',
     "test_conservative_rule_also_guards_absent_names"),
)


def main():
    darwin = subprocess.check_output(["getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()
    failures = []
    with tempfile.TemporaryDirectory(prefix="sf-identity-mutations-", dir=darwin) as directory:
        assert "tmp" not in Path(directory).parts
        env = {**os.environ, "TMPDIR": directory, "PYTHONDONTWRITEBYTECODE": "1",
               "HYPOTHESIS_STORAGE_DIRECTORY": str(Path(directory) / "hypothesis")}
        for name, relative, before, after, selector in MUTATIONS:
            path = ROOT / relative
            original = path.read_bytes()
            source = original.decode()
            assert source.count(before) == 1, (name, source.count(before))
            try:
                path.write_text(source.replace(before, after))
                test = ("tests/unit/test_filesystem_identity_agreement.py" if selector.startswith("test_conservative")
                        else "tests/fake/test_canonical_identity_fix2.py" if name.startswith("drop ") and name not in
                        ("drop casefold", "drop canonical normalization") else TEST)
                result = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-x",
                                         "-p", "no:cacheprovider", "-p", "tests.canonical_identity_privacy",
                                         test, "-k", selector], cwd=ROOT, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                print(f"{name}: {'KILLED' if result.returncode == 1 else 'FAILED CHECK'} (pytest {result.returncode})", flush=True)
                print(result.stdout, flush=True)
                if result.returncode != 1:
                    failures.append(name)
            finally:
                path.write_bytes(original)
                assert path.read_bytes() == original
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
