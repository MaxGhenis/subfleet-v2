"""D-TMP1 mutation witnesses reuse the lifecycle and leak suites' named assertions.

An oracle that renames a folder whose mode it set runs once per directory rename
rule (`tests/rename_rules.py`), and its id names the rule (D-TMP1-P3 r2).
"""

import os
from pathlib import Path
from types import ModuleType

import pytest

from subfleet import attempt_tmp
from tests.rename_rules import HOST, MACOS15, RULES
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
    # The mode change of a crash-left tombstone, which no rename precedes.
    "no-top-chmod": [("                if not self._own(record, tomb, parent):\n"
                      "                    self.log.warning(\"TMPDIR removal refused for %s: its folder was swapped\", record.get(\"attempt_id\"))\n"
                      "                    return False\n",
                      "                pass\n")],
    # D-TMP1-P3 r2: the mode fixed before the rename, and the check before that.
    "no-pre-rename-chmod": [("                    if not self._own(record, name, parent):\n"
                             "                        self.log.warning(\"TMPDIR removal refused for %s: its folder was swapped\", record.get(\"attempt_id\"))\n"
                             "                        return False\n",
                             "                    pass\n")],
    "no-pre-rename-identity-check": [("                if not self._is(record, source):\n",
                                      "                if False:\n")],
    "no-rmdir": [("                os.rmdir(path)\n            except OSError as exc:",
                  "                pass\n            except OSError as exc:")],
    "no-depth-check": [("            if len(stack) >= rfs.MAX_DEPTH:\n                raise rfs.TreeError(\"too-deep\", name)\n",
                        "")],
    # The four removal guards #246 had (C-12.10): the identity check of what the
    # rename moved (r2's post-rename re-check), symlinks inside never followed, no
    # other device entered, and a put-back never over a name that reappeared.
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
    # r3 (review r1b2 P2-1): r2's `mode | 0o700` by name, restored at each site.
    "or-mode-before-rename": [("                    if not self._own(record, name, parent):\n",
                               "                    os.chmod(name, stat.S_IMODE(source.st_mode) | 0o700, dir_fd=parent, "
                               "follow_symlinks=False)\n                    if False:\n")],
    "or-mode-tombstone": [("                if not self._own(record, tomb, parent):\n",
                           "                os.chmod(tomb, stat.S_IMODE(found.st_mode) | 0o700, dir_fd=parent, "
                           "follow_symlinks=False)\n                if False:\n")],
    "or-mode-nested": [("                os.chmod(name, stat.S_IRWXU, dir_fd=fd, follow_symlinks=False)\n",
                        "                os.chmod(name, stat.S_IMODE(found.st_mode) | 0o700, dir_fd=fd, follow_symlinks=False)\n")],
    # ... and through a verified descriptor, which is safe but puts a refused folder back wider than 0700.
    "or-mode-descriptor": [("            os.fchmod(fd, stat.S_IRWXU)\n",
                            "            os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) | 0o700)\n")],
    # The descriptor path dropped: always by name.
    "by-name-own": [("        try:\n            fd = os.open(name, rfs.O_DIR, dir_fd=parent)\n"
                     "        except PermissionError as exc:\n            if exc.errno != errno.EACCES:\n"
                     "                raise\n",
                     "        if True:\n")],
    "by-name-nested": [("            try:\n                child = rfs.open_dir(name, dir_fd=fd, expect=found)\n"
                        "            except PermissionError as exc:\n                if exc.errno != errno.EACCES:\n"
                        "                    raise\n",
                        "            if True:\n")],
    # The journal folder's parent never fsynced when the folder is made.
    "no-journal-fsync": [("                os.fsync(parent)\n", "                pass\n")],
    # Review r1b2's P3-1 and P3-2: what C-12.10 states and no PR test pinned.
    "any-tombstone": [("        if isinstance(name, str) and name.startswith(TOMBSTONE) and \"/\" not in name and len(name) <= 128:",
                       "        if isinstance(name, str):")],
    "create-skips-settle": [("                record = self._mark(name, attempt_id)\n                self._settle(name)\n",
                             "                record = self._mark(name, attempt_id)\n")],
    "sweep-no-ownership-check": [("                if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid():\n"
                                  "                    continue                 # not this store's to judge; nothing is recorded\n",
                                  "")],
}

def _ruled(mutation, test, args, rules=RULES):
    """One oracle row per rename rule, the rule last among its arguments and in its id."""
    return [(f"{mutation}-{rule}", mutation, test, (*args, rule)) for rule in rules]


#: The swap after removal's last look at the allocation's name: only the check of
#: what the rename moved can refuse it (r2: the check before the rename refuses
#: the lifecycle suite's swap, which comes earlier).
LAST_LOOK = "leaks:test_a_directory_swapped_in_after_removals_last_look_is_refused_and_put_back"
#: r3: an entry swapped in before a mode change never gains group or other bits.
SWAP = "leaks:test_an_entry_swapped_in_before_a_mode_change_never_gains_group_or_other_bits"
#: r3: a folder an open admits gets 0700 through its descriptor, never by name.
DESCRIPTOR = "leaks:test_a_folder_the_owner_can_read_gets_0700_through_its_descriptor_never_by_name"

ORACLES = [
    ("reuse", "reuse", "lifecycle:test_attempt_names_are_unique_even_after_path_sanitizing", ()),
    ("no-census", "no-census", "lifecycle:test_cleanup_requires_terminal_empty_attempt", ("succeeded", False)),
    ("no-prefix", "no-prefix", "lifecycle:test_sweep_requires_prefix_even_with_valid_creation_record", ()),
    ("follow-symlink", "follow-symlink", "lifecycle:test_sweep_never_follows_symlink", ()),
    ("salvage-hold", "salvage-hold", "lifecycle:test_salvage_evidence_never_holds_scratch", ("salvage_skipped",)),
    ("absent-no-receipt", "absent-no-receipt", "lifecycle:test_absent_folder_is_released_whatever_holds_it",
     ("quarantined", False)),
    *_ruled("no-rename-verify", LAST_LOOK, ("monkeypatch", "caplog")),
    ("age-from-marker", "age-from-marker",
     "lifecycle:test_unknown_folder_ages_from_the_store_never_its_marker_or_birth_time", ()),
    ("age-from-birth-time", "age-from-birth-time",
     "lifecycle:test_unknown_folder_ages_from_the_store_never_its_marker_or_birth_time", ()),
    ("no-tombstone-record", "no-tombstone-record",
     "leaks:test_p3_1_tombstone_a_crash_left_is_finished_never_released_as_absent", ("monkeypatch",)),
    *_ruled("no-top-chmod", "leaks:test_p3_2_a_locked_tombstone_a_crash_left_is_finished", ("monkeypatch", "caplog")),
    # Caught under macOS 15's rule only: on this host's, the tombstone's mode change
    # covers for it (`test_the_pre_rename_chmod_mutant_is_caught_by_this_hosts_rule_only_if_it_refuses`).
    *_ruled("no-pre-rename-chmod", "leaks:test_p3_2_owner_unreadable_top_folder_is_removed",
            ("monkeypatch", "caplog"), rules=(MACOS15,)),
    # A 0000 directory (r3): for one an open admits, `_own`'s descriptor check refuses it too.
    *_ruled("no-pre-rename-identity-check",
            "leaks:test_a_locked_directory_swapped_in_before_removal_is_neither_changed_nor_moved",
            ("monkeypatch", "caplog", 0o000)),
    ("no-rmdir", "no-rmdir", "leaks:test_p3_3_failed_marker_publish_leaves_no_folder", ("monkeypatch",)),
    *_ruled("no-depth-check", "leaks:test_t4_a_chain_past_max_depth_is_refused_in_bounded_descriptors",
            ("monkeypatch", "caplog")),
    *_ruled("no-identity-check", LAST_LOOK, ("monkeypatch", "caplog")),
    ("follow-inner-symlink", "follow-inner-symlink",
     "lifecycle:test_cleanup_does_not_follow_nested_symlinks_or_count_their_targets", ()),
    ("cross-device", "cross-device", "leaks:test_removal_never_enters_another_device", ("monkeypatch", "caplog")),
    ("put-back-over", "put-back-over", "leaks:test_put_back_never_replaces_a_name_that_reappeared",
     ("monkeypatch", "caplog")),
    *_ruled("or-mode-before-rename", SWAP, ("monkeypatch", "before-rename", 0o077, "hard-link", "at-mode-change")),
    *_ruled("or-mode-tombstone", SWAP, ("monkeypatch", "crash-left-tombstone", 0o077, "hard-link", "at-mode-change")),
    *_ruled("or-mode-nested", SWAP, ("monkeypatch", "nested", 0o077, "hard-link", "at-mode-change")),
    *_ruled("or-mode-descriptor", "leaks:test_t3_a_deletion_no_mode_change_fixes_defers_logs_and_keeps_the_record",
            ("monkeypatch", "caplog", 0o577)),
    *_ruled("by-name-own", DESCRIPTOR, ("monkeypatch", "before-rename", 0o500)),
    *[(f"by-name-own-tombstone-{rule}", "by-name-own", DESCRIPTOR,
       ("monkeypatch", "crash-left-tombstone", 0o500, rule)) for rule in RULES],
    *_ruled("by-name-nested", DESCRIPTOR, ("monkeypatch", "nested", 0o500)),
    ("no-journal-fsync", "no-journal-fsync", "leaks:test_the_journal_folders_own_entry_is_fsynced_once_when_it_is_made",
     ("monkeypatch",)),
    ("any-tombstone", "any-tombstone", "leaks:test_a_tombstone_value_that_is_not_a_tombstone_name_refuses_removal",
     ("caplog", "../escape-r1b")),
    ("create-skips-settle", "create-skips-settle", "leaks:test_a_successful_create_leaves_no_journal_entry",
     ("monkeypatch",)),
    ("sweep-no-ownership-check", "sweep-no-ownership-check",
     "leaks:test_a_journal_entry_naming_a_symlink_or_a_file_is_never_recorded_or_removed", ()),
]

SUITES = {"lifecycle": lifecycle, "leaks": leaks}


def _oracle(name):
    suite, test = name.split(":")
    return getattr(SUITES[suite], test)


def _arguments(args, monkeypatch, caplog):
    return [monkeypatch if arg == "monkeypatch" else caplog if arg == "caplog" else arg for arg in args]


def _mutant(mutation):
    source = Path(attempt_tmp.__file__).read_text()
    for old, new in MUTATIONS[mutation]:
        assert source.count(old) >= 1, "mutation must still hit its production decision"
        source = source.replace(old, new)
    mutant = ModuleType("subfleet._attempt_tmp_mutant")
    mutant.__package__ = "subfleet"
    mutant.__dict__["_follow_open"] = _follow_open
    exec(compile(source, attempt_tmp.__file__, "exec"), mutant.__dict__)
    return mutant


@pytest.mark.parametrize("mutation,test,args", [row[1:] for row in ORACLES], ids=[row[0] for row in ORACLES])
def test_named_lifecycle_assertion_kills_mutation(tmp_path, monkeypatch, caplog, mutation, test, args):
    monkeypatch.setattr(attempt_tmp, "AttemptTemp", _mutant(mutation).AttemptTemp)
    try:
        with pytest.raises(AssertionError):
            _oracle(test)(tmp_path, *_arguments(args, monkeypatch, caplog))
    finally:
        leaks.unlock(tmp_path)


@pytest.mark.parametrize("mutation,test,args", [row[1:] for row in ORACLES], ids=[row[0] for row in ORACLES])
def test_named_lifecycle_assertion_passes_on_production(tmp_path, monkeypatch, caplog, mutation, test, args):
    """Each oracle passes unmutated, so a kill above is the mutation's doing."""
    _oracle(test)(tmp_path, *_arguments(args, monkeypatch, caplog))


def _host_renames_a_locked_directory(folder: Path) -> bool:
    probe = folder / "probe"
    probe.mkdir(0o700)
    os.chmod(probe, 0o500)
    try:
        os.rename(probe, folder / "probe-moved")
    except PermissionError:
        os.chmod(probe, 0o700)
        probe.rmdir()
        return False
    os.chmod(folder / "probe-moved", 0o700)
    (folder / "probe-moved").rmdir()
    return True


def test_the_pre_rename_chmod_mutant_is_caught_by_this_hosts_rule_only_if_it_refuses(tmp_path, monkeypatch, caplog):
    """Why the macOS 15 rule exists. With the mode change before the rename dropped,
    a host that renames a locked directory (macOS 26) still removes it, because
    the tombstone's mode change runs after; only a host that refuses (macOS 15),
    or its emulation, shows the defect. So this host's rule kills the mutant
    exactly when this host refuses such a rename."""
    (tmp_path / "probe-root").mkdir()
    refuses = not _host_renames_a_locked_directory(tmp_path / "probe-root")
    monkeypatch.setattr(attempt_tmp, "AttemptTemp", _mutant("no-pre-rename-chmod").AttemptTemp)
    oracle = _oracle("leaks:test_p3_2_owner_unreadable_top_folder_is_removed")
    try:
        if refuses:
            with pytest.raises(AssertionError):
                oracle(tmp_path, monkeypatch, caplog, HOST)
        else:
            oracle(tmp_path, monkeypatch, caplog, HOST)
    finally:
        leaks.unlock(tmp_path)
