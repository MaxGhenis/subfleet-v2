"""D-TMP1 mutation witnesses reuse the lifecycle suite's named assertions."""

from pathlib import Path
from types import ModuleType

import pytest

from subfleet import attempt_tmp
from tests.unit import test_attempt_tmpdir_lifecycle as oracle


@pytest.mark.parametrize("mutation,test", [
    ("reuse", "test_attempt_names_are_unique_even_after_path_sanitizing"),
    ("no-census", "test_cleanup_requires_terminal_empty_unretained_attempt"),
    ("no-prefix", "test_sweep_requires_prefix_even_with_valid_creation_record"),
    ("follow-symlink", "test_sweep_never_follows_symlink"),
])
def test_named_lifecycle_assertion_kills_mutation(tmp_path, monkeypatch, mutation, test):
    source = Path(attempt_tmp.__file__).read_text()
    changes = {
        "reuse": [("path = Path(tempfile.mkdtemp(prefix=PREFIX + safe_id + \"-\", dir=self.root))",
                   "path = self.root / (PREFIX + 'shared'); path.mkdir(exist_ok=True)")],
        "no-census": [("not verified_empty or subject.get", "subject.get")],
        "no-prefix": [(" or not path.name.startswith(PREFIX)", "")],
        "follow-symlink": [("found = path.lstat()", "found = path.stat()"),
                           ("fd = os.open(path, rfs.O_DIR)", "fd = os.open(path, rfs.O_DIR & ~os.O_NOFOLLOW)"),
                           ("fd = rfs.open_dir(path)", "fd = os.open(path, rfs.O_DIR & ~os.O_NOFOLLOW)")],
    }
    for old, new in changes[mutation]:
        assert old in source, "mutation must still hit its production decision"
        source = source.replace(old, new)
    mutant = ModuleType("subfleet._attempt_tmp_mutant")
    mutant.__package__ = "subfleet"
    exec(compile(source, attempt_tmp.__file__, "exec"), mutant.__dict__)
    monkeypatch.setattr(attempt_tmp, "AttemptTemp", mutant.AttemptTemp)
    check = getattr(oracle, test)
    with pytest.raises(AssertionError):
        if mutation == "no-census":
            check(tmp_path, "succeeded", False, False)
        elif mutation == "follow-symlink":
            check(tmp_path, monkeypatch)
        else:
            check(tmp_path)
