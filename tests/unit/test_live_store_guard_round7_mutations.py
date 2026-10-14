"""Mutation witnesses for D-ST1. Each child is an isolated Python process, not pytest.

Run controls and mutants inside one foreground pytest/lock hold. All executable
and filesystem subjects are temporary stand-ins; no mutant starts a native reader.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[2]
CREDENTIALS = 'subfleet/credentials.py'
HOMES = 'subfleet/home_paths.py'
FIXTURES = 'tests/conftest.py'
TRAP = 'tests/live_store_trap.py'

# (file, exact original, replacement, invariant probe). Exact replacements keep
# a stale mutation from silently becoming a passing no-op.
MUTANTS = {
    'm01-fold': (CREDENTIALS, 'unicodedata.normalize("NFD", os.path.realpath(path)).casefold()',
                 'unicodedata.normalize("NFD", os.path.realpath(path))', 'fold'),
    'm02-identity': (CREDENTIALS, 'return found.st_dev, found.st_ino', 'return None', 'identity'),
    'm03-session-pin': (CREDENTIALS,
        'return under_pytest() or any(session_alive(*pin) for pin in list(pins))',
        'return under_pytest()', 'pin'),
    'm04-child-cwd': (CREDENTIALS, 'base = "" if cwd is None else os.fsdecode(cwd)', 'base = ""', 'cwd'),
    'm05-child-path': (CREDENTIALS, 'os.get_exec_path(env), cwd', 'os.get_exec_path(), cwd', 'path'),
    'm06-configure-install': (FIXTURES, '    install_keychain_exec_guard()\n', '', 'configure'),
    'm07-import-install': (CREDENTIALS,
        'if PYTEST_MARKER in os.environ or "PYTEST_CURRENT_TEST" in os.environ:', 'if False:', 'import'),
    'm08-early-file-check': (CREDENTIALS,
        'target is not None and _is_keychain_binary(target, _native_keychain_binary())',
        'security_bin in ("security", "/usr/bin/security")', 'early'),
    'm09-executable-search': (CREDENTIALS,
        'os.path.isfile(candidate) and os.access(candidate, os.X_OK)', 'os.path.exists(candidate)', 'search'),
    'm10-install-once': (CREDENTIALS, 'pins = getattr(sys, _EXEC_GUARD, None)', 'pins = None', 'once'),
    'm11-posix-spawn': (CREDENTIALS,
        'frozenset({"subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn"})',
        'frozenset({"subprocess.Popen", "os.exec", "os.spawn"})', 'spawn'),
    'm12-adapter-error': (CREDENTIALS,
        'raise KeychainExecRefused(_REFUSAL, code=7, fix=_REFUSAL_FIX)',
        'raise PermissionError(_REFUSAL)', 'code'),
    'm13-home-root': (HOMES, 'return (home / ".subfleet").resolve()',
        'return (provider_home() / ".subfleet").resolve()', 'home-root'),
    'm14-account-refusal': (HOMES, 'if under_pytest() and is_account_home(home):', 'if False:', 'account'),
    'm15-account-identity': (HOMES, 'return home.samefile(personal)', 'return False', 'account-alias'),
    'm16-dev-env-removal': (HOMES,
        'SAFE_ENV_REMOVALS = ("CLAUDE_LANE_AGENT_SECRET", "SUBFLEET_DEV_APP_EXECUTABLE")',
        'SAFE_ENV_REMOVALS = ("CLAUDE_LANE_AGENT_SECRET",)', 'dev-env'),
    'r7-home-baseline': (FIXTURES, '    os.environ["HOME"] = str(base / "home")\n', '', 'home-baseline'),
    'r7-path-baseline': (FIXTURES,
        '    os.environ["PATH"] = recorder.search_path(os.environ.get("PATH", os.defpath))\n', '', 'path-baseline'),
    'r7-bare-name': (CREDENTIALS, 'target == "security" or _matches(target, _native)',
        '_matches(target, _native)', 'bare'),
    'r7-io-open': (TRAP,
        '        inner.setattr(io, "open", guarded(io.open, [(0, "file", "dir_fd")]))\n', '', 'read_text'),
    'r7-destination': (TRAP, '[(0, "src", "src_dir_fd"), (1, "dst", "dst_dir_fd")]', '[(0, "src", "src_dir_fd")]', 'destination'),
    'r7-realpath': (TRAP, 'if not getattr(resolving, "active", False):', 'if False:', 'alias'),
    'r7-dir-fd': (TRAP, 'check(path, kwargs.get(fd_keyword))', 'check(path)', 'dir-fd'),
}

PROBE = '''
import os, pwd, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
import pytest
kind, scratch = sys.argv[1], Path(sys.argv[2])
scratch.mkdir()
configure_cases = {'configure', 'dev-env', 'home-baseline', 'path-baseline'}
if kind in configure_cases:
    for name in ('SUBFLEET_UNDER_PYTEST', 'PYTEST_CURRENT_TEST', 'SUBFLEET_PYTEST_SESSION', 'SUBFLEET_PYTEST_SESSION_ID'):
        os.environ.pop(name, None)
from subfleet import credentials, home_paths
from subfleet.adapters.base import AdapterError

def refuse(event, program, cwd=None, env=None):
    details = {'subprocess.Popen': (program, [program], cwd, env),
               'os.posix_spawn': (program, [program], env or {})}
    try:
        sys.audit(event, *details[event])
    except Exception as error:
        assert isinstance(error, credentials.KeychainExecRefused), type(error)
        assert error.code == 7
        return
    raise AssertionError('launch was approved')

if kind == 'fold':
    native = credentials._native_keychain_binary()
    assert credentials._is_keychain_binary('/USR/BIN/SECURITY', native, _identity=lambda path: None)
elif kind == 'identity':
    # Match by identity alone, with a recording fixture as the native identity.
    recorder = scratch / 'native'
    recorder.touch()
    alias = scratch / 'alias'
    alias.hardlink_to(recorder)
    native = (str(recorder), credentials._identity(recorder))
    assert credentials._is_keychain_binary(str(alias), native, _fold=lambda path: path)
elif kind == 'pin':
    for name in ('SUBFLEET_UNDER_PYTEST', 'PYTEST_CURRENT_TEST', 'SUBFLEET_PYTEST_SESSION', 'SUBFLEET_PYTEST_SESSION_ID'):
        os.environ.pop(name, None)
    refuse('subprocess.Popen', 'security')
elif kind == 'cwd':
    refuse('subprocess.Popen', './security', cwd='/usr/bin')
elif kind in ('path', 'bare', 'spawn', 'import', 'code'):
    # Explicit paths are the control; PATH-search preservation uses an alias
    # name so the stronger bare-security rule cannot mask a PATH-search mutant.
    from tests.keychain_standin import StandIn
    guarded = StandIn(scratch / 'guarded')
    safe = StandIn(scratch / 'safe')
    credentials._STANDIN_KEYCHAIN_BINARIES = (str(guarded.path),)
    if kind == 'path':
        (guarded.path.parent / 'reader').symlink_to(guarded.path)
        (safe.path.parent / 'reader').symlink_to(safe.path)
        os.environ['PATH'] = safe.search_path()
        refuse('subprocess.Popen', 'reader', env={'PATH': str(guarded.path.parent)})
    elif kind == 'bare':
        refuse('subprocess.Popen', 'security', env={'PATH': str(safe.path.parent)})
    else:
        refuse('os.posix_spawn' if kind == 'spawn' else 'subprocess.Popen', str(guarded.path))
elif kind == 'early':
    try:
        credentials.keychain_command('fixture-ref', '/usr/bin/./security')
    except AdapterError:
        pass
    else:
        raise AssertionError('early alias check was approved')
elif kind == 'search':
    (scratch / 'security').mkdir()
    assert credentials._find_program('security', [str(scratch)]) is None
elif kind == 'once':
    assert credentials.install_keychain_exec_guard() is False
    assert credentials.install_keychain_exec_guard() is False
elif kind == 'home-root':
    os.environ['HOME'] = str(scratch / 'home')
    os.environ['SUBFLEET_PROVIDER_HOME'] = str(scratch / 'providers')
    assert home_paths.production_state_root() == (scratch / 'home' / '.subfleet').resolve()
elif kind in ('account', 'account-alias'):
    from tests.live_store_trap import untouchable
    account = scratch / 'person'
    (account / '.subfleet').mkdir(parents=True)
    alias = scratch / 'alias'
    alias.symlink_to(account)
    original = pwd.getpwuid
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_dir=str(account)))
        patch.setenv('HOME', str(alias if kind == 'account-alias' else account))
        with untouchable(patch, account / '.subfleet'):
            try:
                home_paths.production_state_root()
            except RuntimeError as error:
                assert 'isolated test home' in str(error)
            else:
                raise AssertionError('account root was approved')
elif kind in configure_cases:
    from tests import conftest
    from tests.keychain_standin import StandIn
    outer = StandIn(scratch / 'outer')
    os.environ['PATH'] = outer.search_path()
    os.environ['SUBFLEET_DEV_APP_EXECUTABLE'] = str(scratch / 'dev-app')
    before_home = os.environ['HOME']
    config = SimpleNamespace()
    try:
        conftest.pytest_configure(config)
        if kind == 'configure':
            credentials._STANDIN_KEYCHAIN_BINARIES = (str(outer.path),)
            refuse('subprocess.Popen', str(outer.path))
        elif kind == 'dev-env':
            assert 'SUBFLEET_DEV_APP_EXECUTABLE' not in os.environ
        elif kind == 'home-baseline':
            assert os.environ['HOME'] == str(config._subfleet_safe_home_base / 'home')
        else:
            subprocess.run(['/bin/sh', '-c', 'security fixture-ref'], capture_output=True, check=True)
            assert outer.count() == 0 and config._subfleet_keychain_standin.count() == 1
    finally:
        conftest.pytest_unconfigure(config)
    assert os.environ['HOME'] == before_home
elif kind in ('read_text', 'destination', 'alias', 'dir-fd'):
    from tests.live_store_trap import untouchable
    root = scratch / 'live'
    root.mkdir()
    (root / 'data').write_text('fixture')
    alias = scratch / 'alias'
    alias.symlink_to(root)
    outside = scratch / 'outside'
    outside.touch()
    fd = os.open(root, os.O_RDONLY)
    try:
        with pytest.MonkeyPatch.context() as patch, untouchable(patch, root):
            caught = False
            try:
                if kind == 'read_text': (root / 'data').read_text()
                elif kind == 'destination': os.rename(outside, root / 'new')
                elif kind == 'alias': os.stat(alias / 'data')
                else: os.stat('data', dir_fd=fd)
            except pytest.fail.Exception:
                caught = True
            assert caught, 'trap allowed the access'
    finally:
        os.close(fd)
else:
    raise AssertionError(kind)
print('invariant passed:', kind)
'''


@pytest.fixture(scope="module")
def shadow(tmp_path_factory):
    code = tmp_path_factory.mktemp("guard-mutants")
    shutil.copytree(REPO / "subfleet", code / "subfleet", ignore=shutil.ignore_patterns("__pycache__"))
    (code / "tests").mkdir()
    for file in ("__init__.py", "conftest.py", "keychain_standin.py", "live_store_trap.py"):
        shutil.copyfile(REPO / "tests" / file, code / "tests" / file)
    return code


@pytest.mark.parametrize("name", MUTANTS)
def test_each_guard_mutant_is_killed_by_its_invariant(tmp_path, shadow, name):
    relative, original, replacement, kind = MUTANTS[name]
    # Each worker has its own module fixture; each child has fresh audit hooks.
    env = {**os.environ, "PYTHONPATH": str(shadow), "PYTHONDONTWRITEBYTECODE": "1"}
    def run(label):
        return subprocess.run([sys.executable, "-c", PROBE, kind, str(tmp_path / label)],
                              cwd=shadow, env=env, capture_output=True, text=True)
    control = run("control")
    assert control.returncode == 0, control.stdout + control.stderr
    path = shadow / relative
    source = path.read_text()
    assert source.count(original) == 1, (name, original)
    try:
        path.write_text(source.replace(original, replacement))
        mutant = run("mutant")
    finally:
        path.write_text(source)
    assert mutant.returncode != 0, f"{name} survived: {mutant.stdout}"
    assert "AssertionError" in mutant.stderr or "Failed: touched" in mutant.stderr, mutant.stderr
