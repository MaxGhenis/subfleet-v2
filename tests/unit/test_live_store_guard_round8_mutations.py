"""D-ST1 r8: each requested source mutation must kill a named invariant.

Controls and mutants run as ordinary Python children under the parent's single
pytest/lock hold. No mutation launches a reader or accesses a personal store.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.unit.test_live_store_guard_round7_mutations import shadow

MUTANTS = {
    "fold": ("subfleet/credentials.py",
             'folded = unicodedata.normalize("NFD", name).casefold()',
             'folded = unicodedata.normalize("NFD", name)', '''
from subfleet import credentials
try:
    sys.audit('subprocess.Popen', 'Security', ['Security'], None, {'PATH': str(scratch)})
except credentials.KeychainExecRefused:
    pass
else:
    raise AssertionError('folded bare reader approved')
'''),
    "family-taskpolicy": ("subfleet/credentials.py", '"nohup", "taskpolicy"', '"nohup"', '''
from subfleet import credentials
try:
    sys.audit('subprocess.Popen', '/usr/bin/taskpolicy', ['taskpolicy', 'fixture'], None,
              {'PATH': '/usr/bin:/bin'})
except credentials.KeychainExecRefused:
    pass
else:
    raise AssertionError('wrapper reset PATH approved')
'''),
    "uv-cache": ("tests/conftest.py",
                 '    os.environ.setdefault("UV_CACHE_DIR", str(original_home / ".cache" / "uv"))\n', '', '''
from types import SimpleNamespace
from tests import conftest
os.environ['HOME'] = str(scratch / 'original-home')
os.environ.pop('UV_CACHE_DIR', None)
config = SimpleNamespace()
try:
    conftest.pytest_configure(config)
    assert os.environ.get('UV_CACHE_DIR') == str(scratch / 'original-home/.cache/uv')
finally:
    conftest.pytest_unconfigure(config)
'''),
    "chdir": ("tests/live_store_trap.py", '"chdir", "stat"', '"stat"', '''
import pytest
from tests.live_store_trap import untouchable
root = scratch / 'live'
root.mkdir()
before = os.getcwd()
try:
    with pytest.MonkeyPatch.context() as patch, untouchable(patch, root):
        with pytest.raises(pytest.fail.Exception, match='touched'):
            os.chdir(root)
finally:
    os.chdir(before)
'''),
}


@pytest.mark.parametrize("name", MUTANTS)
def test_round8_mutant_is_killed(tmp_path, shadow, name):
    relative, original, replacement, probe = MUTANTS[name]
    source = shadow / relative
    saved = source.read_text()
    assert saved.count(original) == 1, (name, "stale mutation")
    env = {**os.environ, "PYTHONPATH": str(shadow), "PYTHONDONTWRITEBYTECODE": "1"}
    code = "import os,sys\nfrom pathlib import Path\nscratch=Path(sys.argv[1]); scratch.mkdir()\n" + probe
    def run(label):
        return subprocess.run([sys.executable, "-c", code, str(tmp_path / label)],
                              cwd=shadow, env=env, capture_output=True, text=True, timeout=30)
    control = run("control")
    assert control.returncode == 0, control.stdout + control.stderr
    try:
        source.write_text(saved.replace(original, replacement))
        mutant = run("mutant")
        assert mutant.returncode != 0, f"survived: {name}"
        assert "AssertionError" in mutant.stderr or "Failed: DID NOT RAISE" in mutant.stderr, mutant.stderr
    finally:
        source.write_text(saved)
