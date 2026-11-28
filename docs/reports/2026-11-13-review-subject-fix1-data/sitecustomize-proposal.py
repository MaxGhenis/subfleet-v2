"""Job-local fail-closed guard for the remaining isolated tests.

This is a temporary test-lane helper, not a product change.
"""

import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import traceback
import unicodedata


_ROOT = Path('/private/tmp/sf-a2-fix1')
_PROJECT = Path('/Users/maxghenis/.subfleet/worktrees/20261115-180648-rv-a2-fix-r1b')
_GUARD_VERSION = 'sf-a2-fix1-strict-v2'
_NATIVE_TEMP = Path('/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261115-180648-rv-a2-fix-r1b-a1-3hqfxg5e/sf-a2-fix1')
_INTERPRETER = os.path.normpath(sys.executable)
_BOOTSTRAPPING = True
_BUSY = False
_DENIED_NAMES = frozenset({'ps', 'lsof', 'security', 'agent-secret', 'launchctl'})
_WRAPPERS = frozenset({'sh', 'bash', 'zsh', 'dash', 'ksh', 'fish', 'env', 'arch',
                       'xcrun', 'osascript', 'lockf', 'nice', 'nohup', 'setsid'})
_NATIVE_READS = frozenset({'proc_listpids', 'proc_pidinfo', 'proc_pidpath',
                           'proc_pidpath_audittoken', 'responsibility_get_pid_responsible_for_pid',
                           'sysctl'})
_NATIVE_STARTS = frozenset({'posix_spawn', 'posix_spawnp', 'execv', 'execve',
                            'execvp', 'execvpe', 'system', 'popen'})


def _name(value):
    return unicodedata.normalize('NFKC', os.path.basename(os.fsdecode(value))).casefold()


def _log(reason):
    # Never serialize argv, environment, locals, exception objects or values.
    # Code locations identify the responsible fixture without exposing data.
    global _BUSY
    if _BUSY:
        return
    _BUSY = True
    try:
        frames = []
        for frame in traceback.extract_stack(limit=18):
            if '/tests/' in frame.filename:
                frames.append({'file': frame.filename.split('/tests/', 1)[1],
                               'line': frame.lineno, 'function': frame.name})
        record = {'reason': reason, 'at': time.monotonic(), 'frames': frames}
        with (_ROOT / ('guard-denials-' + str(os.getpid()) + '.jsonl')).open('a') as stream:
            stream.write(json.dumps(record) + '\n')
    except BaseException:
        pass
    finally:
        _BUSY = False


def _deny(reason):
    _log(reason)
    raise PermissionError(errno.EPERM, 'sf-a2-fix1 safety guard: ' + reason)


def _python_allowed(executable):
    spelling = os.path.normpath(os.fsdecode(executable))
    if spelling == _INTERPRETER:
        return True
    return (os.path.dirname(spelling) == str(_PROJECT / '.venv' / 'bin')
            and re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?', _name(spelling)) is not None)


def _resolved_executable(executable, environment=None):
    target = os.fsdecode(executable)
    if not os.path.dirname(target):
        target = shutil.which(target, path=(environment or os.environ).get('PATH', os.defpath)) or ''
    return os.path.realpath(target)


def _approved_frontend(probe):
    if not (probe.is_relative_to(_ROOT) or probe.is_relative_to(_NATIVE_TEMP)):
        return False
    manifest = _ROOT / 'frontend-approved-binaries.jsonl'
    if not manifest.is_file() or not probe.is_file():
        return False
    expected = None
    for line in manifest.read_text().splitlines():
        entry = json.loads(line)
        if entry.get('path') == str(probe):
            expected = entry.get('sha256')
    if expected is None:
        return False
    payload = probe.read_bytes()
    return (payload[:4] in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe')
            and hashlib.sha256(payload).hexdigest() == expected)


def _check_exec(executable, argv, environment=None):
    words = [os.fsdecode(value) for value in argv] if isinstance(argv, (list, tuple)) else []
    name = _name(executable)
    argv_name = _name(words[0]) if words else name
    names = {name, argv_name}
    if names.intersection(_DENIED_NAMES):
        _deny('forbidden executable')
    if name == 'taskpolicy':
        if (_resolved_executable(executable, environment) == '/usr/sbin/taskpolicy'
                and len(words) > 3 and words[1:3] == ['-c', 'utility']):
            # Inspect the exact wrapped command too; QoS cannot hide a banned
            # reader/publication command or unapproved executable.
            _check_exec(words[3], words[3:], environment)
            return
        _deny('unapproved taskpolicy wrapper')
    if (name == 'xcrun' and _resolved_executable(executable, environment) == '/usr/bin/xcrun'
            and len(words) > 1 and words[1] == 'swiftc'):
        return
    if names.intersection(_WRAPPERS):
        _deny('opaque process wrapper')
    if 'gh' in names:
        # More conservative than the task: reject all external gh calls.
        # Guard tests may still pass command text to a parser as inert data.
        _deny('external gh command')
    if 'git' in names:
        if any(re.search(r'\bpush\b', word, re.IGNORECASE) for word in words[1:]):
            _deny('git push shaped command')
        # Refuse fixture executables named git that could delegate invisibly.
        # Only trusted installed Git binaries are allowed past this boundary.
        target = _resolved_executable(executable, environment)
        if not (target in {'/usr/bin/git', '/usr/local/bin/git', '/opt/homebrew/bin/git'}
                or target.startswith(('/opt/homebrew/Cellar/git/', '/usr/local/Cellar/git/',
                                      '/Library/Developer/CommandLineTools/usr/bin/git'))):
            _deny('unverified git executable')
        return
    if re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?', name):
        if not _python_allowed(executable):
            _deny('Python interpreter without verified guard installation')
        if any(word.startswith('-') and not word.startswith('--') and 'S' in word
               for word in words[1:]):
            _deny('Python site initialization bypass')
        return
    if os.fsdecode(executable) in {'/bin/sleep', '/usr/bin/sleep', '/bin/true', '/usr/bin/true',
                                  '/bin/false', '/usr/bin/false'}:
        return
    probe = Path(_resolved_executable(executable, environment))
    if _approved_frontend(probe):
        return
    _deny('external executable outside strict allowlist')


def _audit(event, details):
    if event == 'subprocess.Popen':
        executable, argv, _cwd, environment = details
        _check_exec(executable, argv, environment)
    elif event in ('os.exec', 'os.posix_spawn'):
        executable, argv, environment = details
        _check_exec(executable, argv, environment)
    elif event == 'os.spawn':
        _mode, executable, argv, environment = details
        _check_exec(executable, argv, environment)
    elif event == 'os.system':
        _deny('shell execution')
    elif event == 'ctypes.dlsym' and not _BOOTSTRAPPING:
        symbol = os.fsdecode(details[1])
        if symbol in _NATIVE_READS:
            _deny('native process reader binding')
        if symbol in _NATIVE_STARTS:
            _deny('native process execution binding')


sys.addaudithook(_audit)
try:
    # Source inspection verified that this import binds native symbols but does
    # not call them: no ps, sysctl, libproc enumeration or process reads occur.
    from subfleet import procs as _procs

    def _native_denied(*args, **kwargs):
        _log('native process inspection')
        raise _procs.InspectionError('sf-a2-fix1 safety guard: native process inspection unavailable')

    def _native_init(self):
        self._sysctl = self._listpids = self._pidinfo = self._responsible = _native_denied
        self.readable = self.responsibility = True
        self._argument_max = None

    _procs.Darwin.__init__ = _native_init
    for _method in ('pids', 'process', 'cwd', 'arguments', 'responsible'):
        setattr(_procs.Darwin, _method, _native_denied)
    for _binding in ('_sysctl', '_listpids', '_pidinfo', '_responsible'):
        setattr(_procs.kernel, _binding, _native_denied)
    # Native C posix_spawn has no CPython subprocess audit event. Force the
    # ordinary audited fallback, and make a direct private call fail closed.
    _procs._spawn.available = False
    if 'posix_spawn' in _procs._spawn.functions:
        _procs._spawn.functions['posix_spawn'] = lambda *args, **kwargs: _deny('native process execution')
    _BOOTSTRAPPING = False
except BaseException:
    _log('guard bootstrap failed')
    # sitecustomize errors otherwise print a warning and allow Python startup.
    # Exit before any test code if the native guard could not be installed.
    os._exit(97)
