"""D-TMP1 mutation witnesses reuse the lifecycle suite's named assertions."""

import os
from pathlib import Path
from types import ModuleType

import pytest

from subfleet import attempt_tmp
from tests.unit import test_attempt_tmpdir_lifecycle as oracle


def _follow_open(name, *, dir_fd=None, expect=None):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)


MUTATIONS = {
    "reuse": [("path = Path(tempfile.mkdtemp(prefix=PREFIX + safe_id + \"-\", dir=self.root))",
               "path = self.root / (PREFIX + 'shared'); path.mkdir(exist_ok=True)")],
    "no-census": [("if (not verified_empty or subject.get", "if (subject.get")],
    "no-prefix": [(" or not path.name.startswith(PREFIX)", "")],
    "follow-symlink": [("found = path.lstat()", "found = path.stat()"),
                       ("follow_symlinks=False", "follow_symlinks=True"),
                       ("fd = os.open(path, rfs.O_DIR)", "fd = os.open(path, rfs.O_DIR & ~os.O_NOFOLLOW)"),
                       ("rfs.open_dir(", "_follow_open(")],
    # Review r1's round-1 decisions, each killed by its own regression.
    "salvage-hold": [("if (not verified_empty or subject.get",
                      "if (not verified_empty or 'salvage_' in str(subject.get('evidence_json')) or subject.get")],
    "absent-no-receipt": [("        if where == \"absent\":\n            return \"absent\"",
                           "        if where == \"absent\" and verified_empty and subject.get(\"state\") in TERMINAL:\n"
                           "            return \"absent\"")],
    "no-rename-verify": [("            os.rename(name, tomb, src_dir_fd=parent, dst_dir_fd=parent)\n"
                          "            moved = True\n"
                          "            found = os.stat(tomb, dir_fd=parent, follow_symlinks=False)\n"
                          "            if not self._is(record, found):",
                          "            tomb = name\n"
                          "            found = os.stat(tomb, dir_fd=parent, follow_symlinks=False)\n"
                          "            if False:")],
    "age-from-marker": [("born = first[\"first_seen_at\"]",
                         "born = (self._marker(path, found) or {}).get(\"created_at\", first[\"first_seen_at\"])")],
    "age-from-birth-time": [("born = first[\"first_seen_at\"]",
                             "born = getattr(found, \"st_birthtime\", first[\"first_seen_at\"])")],
}

ORACLES = [
    ("reuse", "test_attempt_names_are_unique_even_after_path_sanitizing", ()),
    ("no-census", "test_cleanup_requires_terminal_empty_attempt", ("succeeded", False)),
    ("no-prefix", "test_sweep_requires_prefix_even_with_valid_creation_record", ()),
    ("follow-symlink", "test_sweep_never_follows_symlink", ()),
    ("salvage-hold", "test_salvage_evidence_never_holds_scratch", ("salvage_skipped",)),
    ("absent-no-receipt", "test_absent_folder_is_released_whatever_holds_it", ("quarantined", False)),
    ("no-rename-verify", "test_directory_renamed_in_after_the_check_is_refused_and_put_back", ("monkeypatch",)),
    ("age-from-marker", "test_unknown_folder_ages_from_the_store_never_its_marker_or_birth_time", ()),
    ("age-from-birth-time", "test_unknown_folder_ages_from_the_store_never_its_marker_or_birth_time", ()),
]


@pytest.mark.parametrize("mutation,test,args", ORACLES, ids=[row[0] for row in ORACLES])
def test_named_lifecycle_assertion_kills_mutation(tmp_path, monkeypatch, mutation, test, args):
    source = Path(attempt_tmp.__file__).read_text()
    for old, new in MUTATIONS[mutation]:
        assert old in source, "mutation must still hit its production decision"
        source = source.replace(old, new)
    mutant = ModuleType("subfleet._attempt_tmp_mutant")
    mutant.__package__ = "subfleet"
    mutant.__dict__["_follow_open"] = _follow_open
    exec(compile(source, attempt_tmp.__file__, "exec"), mutant.__dict__)
    monkeypatch.setattr(attempt_tmp, "AttemptTemp", mutant.AttemptTemp)
    check = getattr(oracle, test)
    with pytest.raises(AssertionError):
        check(tmp_path, *[monkeypatch if arg == "monkeypatch" else arg for arg in args])


@pytest.mark.parametrize("mutation,test,args", ORACLES, ids=[row[0] for row in ORACLES])
def test_named_lifecycle_assertion_passes_on_production(tmp_path, monkeypatch, mutation, test, args):
    """Each oracle passes unmutated, so a kill above is the mutation's doing."""
    getattr(oracle, test)(tmp_path, *[monkeypatch if arg == "monkeypatch" else arg for arg in args])
