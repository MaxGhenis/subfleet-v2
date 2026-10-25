"""D-TMP1 mutation witnesses reuse the lifecycle and leak suites' named assertions."""

import os
from pathlib import Path
from types import ModuleType

import pytest

from subfleet import attempt_tmp
from tests.unit import test_attempt_tmpdir_leaks as leaks
from tests.unit import test_attempt_tmpdir_lifecycle as lifecycle


def _follow_open(name, *, dir_fd=None, expect=None):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)


MUTATIONS = {
    "reuse": [("name = PREFIX + safe_id + \"-\" + \"\".join(secrets.choice(_SUFFIX) for _ in range(8))",
               "name = PREFIX + 'shared'"),
              ("os.mkdir(self.root / name, 0o700)", "os.makedirs(self.root / name, 0o700, exist_ok=True)")],
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
    "no-rename-verify": [("                os.rename(name, tomb, src_dir_fd=parent, dst_dir_fd=parent)\n"
                          "                moved = True\n"
                          "                found = os.stat(tomb, dir_fd=parent, follow_symlinks=False)",
                          "                tomb = name\n"
                          "                found = os.stat(tomb, dir_fd=parent, follow_symlinks=False)"),
                         ("            if not self._is(record, found):\n"
                          "                self.log.warning(\"TMPDIR removal refused for %s: its folder was swapped\"",
                          "            if False:\n"
                          "                self.log.warning(\"TMPDIR removal refused for %s: its folder was swapped\"")],
    "age-from-marker": [("born = first[\"first_seen_at\"]",
                         "born = (self._marker(path, found) or {}).get(\"created_at\", first[\"first_seen_at\"])")],
    "age-from-birth-time": [("born = first[\"first_seen_at\"]",
                             "born = getattr(found, \"st_birthtime\", first[\"first_seen_at\"])")],
    # D-TMP1-P3: each of its four decisions, dropped one at a time.
    "no-tombstone-record": [("        name = record.get(\"tombstone\")\n        if name is None:",
                             "        name = None\n        if True:\n            return new_tombstone()")],
    "no-top-chmod": [("                os.chmod(tomb, mode | 0o700, dir_fd=parent, follow_symlinks=False)",
                      "                pass")],
    "no-rmdir": [("                os.rmdir(path)\n            except OSError as exc:",
                  "                pass\n            except OSError as exc:")],
    "no-depth-check": [("            if len(stack) >= rfs.MAX_DEPTH:\n                raise rfs.TreeError(\"too-deep\", name)\n",
                        "")],
    # The four removal guards #246 had (C-12.10): the identity check of what the
    # rename moved, symlinks inside never followed, no other device entered, and
    # a put-back never over a name that reappeared.
    "no-identity-check": [("            if not self._is(record, found):\n"
                           "                self.log.warning(\"TMPDIR removal refused for %s: its folder was swapped\"",
                           "            if False:\n"
                           "                self.log.warning(\"TMPDIR removal refused for %s: its folder was swapped\"")],
    "follow-inner-symlink": [("            found = os.stat(name, dir_fd=fd, follow_symlinks=False)\n"
                              "            if found.st_dev != dev:",
                              "            found = os.stat(name, dir_fd=fd)\n"
                              "            if found.st_dev != dev:")],
    "cross-device": [("            if found.st_dev != dev:\n                raise rfs.TreeError(\"mount-point\", name)\n",
                      "")],
    "put-back-over": [("        os.stat(name, dir_fd=parent, follow_symlinks=False)\n"
                       "        log.warning(\"TMPDIR tombstone %s kept: %s reappeared\", tomb, name)\n        return\n",
                       "        raise FileNotFoundError(name)\n")],
}

ORACLES = [
    ("reuse", "lifecycle:test_attempt_names_are_unique_even_after_path_sanitizing", ()),
    ("no-census", "lifecycle:test_cleanup_requires_terminal_empty_attempt", ("succeeded", False)),
    ("no-prefix", "lifecycle:test_sweep_requires_prefix_even_with_valid_creation_record", ()),
    ("follow-symlink", "lifecycle:test_sweep_never_follows_symlink", ()),
    ("salvage-hold", "lifecycle:test_salvage_evidence_never_holds_scratch", ("salvage_skipped",)),
    ("absent-no-receipt", "lifecycle:test_absent_folder_is_released_whatever_holds_it", ("quarantined", False)),
    ("no-rename-verify", "lifecycle:test_directory_renamed_in_after_the_check_is_refused_and_put_back",
     ("monkeypatch",)),
    ("age-from-marker", "lifecycle:test_unknown_folder_ages_from_the_store_never_its_marker_or_birth_time", ()),
    ("age-from-birth-time", "lifecycle:test_unknown_folder_ages_from_the_store_never_its_marker_or_birth_time", ()),
    ("no-tombstone-record", "leaks:test_p3_1_tombstone_a_crash_left_is_finished_never_released_as_absent",
     ("monkeypatch",)),
    ("no-top-chmod", "leaks:test_p3_2_owner_unreadable_top_folder_is_removed", ("caplog",)),
    ("no-rmdir", "leaks:test_p3_3_failed_marker_publish_leaves_no_folder", ("monkeypatch",)),
    ("no-depth-check", "leaks:test_t4_a_chain_past_max_depth_is_refused_in_bounded_descriptors",
     ("monkeypatch", "caplog")),
    ("no-identity-check", "lifecycle:test_directory_renamed_in_after_the_check_is_refused_and_put_back",
     ("monkeypatch",)),
    ("follow-inner-symlink", "lifecycle:test_cleanup_does_not_follow_nested_symlinks_or_count_their_targets", ()),
    ("cross-device", "leaks:test_removal_never_enters_another_device", ("monkeypatch", "caplog")),
    ("put-back-over", "leaks:test_put_back_never_replaces_a_name_that_reappeared", ("monkeypatch", "caplog")),
]

SUITES = {"lifecycle": lifecycle, "leaks": leaks}


def _oracle(name):
    suite, test = name.split(":")
    return getattr(SUITES[suite], test)


def _arguments(args, monkeypatch, caplog):
    return [monkeypatch if arg == "monkeypatch" else caplog if arg == "caplog" else arg for arg in args]


@pytest.mark.parametrize("mutation,test,args", ORACLES, ids=[row[0] for row in ORACLES])
def test_named_lifecycle_assertion_kills_mutation(tmp_path, monkeypatch, caplog, mutation, test, args):
    source = Path(attempt_tmp.__file__).read_text()
    for old, new in MUTATIONS[mutation]:
        assert source.count(old) >= 1, "mutation must still hit its production decision"
        source = source.replace(old, new)
    mutant = ModuleType("subfleet._attempt_tmp_mutant")
    mutant.__package__ = "subfleet"
    mutant.__dict__["_follow_open"] = _follow_open
    exec(compile(source, attempt_tmp.__file__, "exec"), mutant.__dict__)
    monkeypatch.setattr(attempt_tmp, "AttemptTemp", mutant.AttemptTemp)
    try:
        with pytest.raises(AssertionError):
            _oracle(test)(tmp_path, *_arguments(args, monkeypatch, caplog))
    finally:
        leaks.unlock(tmp_path)


@pytest.mark.parametrize("mutation,test,args", ORACLES, ids=[row[0] for row in ORACLES])
def test_named_lifecycle_assertion_passes_on_production(tmp_path, monkeypatch, caplog, mutation, test, args):
    """Each oracle passes unmutated, so a kill above is the mutation's doing."""
    _oracle(test)(tmp_path, *_arguments(args, monkeypatch, caplog))
