"""Keep this job's pytest temporary allocations and retained failures in its scratch."""
from pathlib import Path
import json
import os
import tempfile
import sys
import re
import pytest

SCRATCH = Path(os.environ['SF_AO_FIX1_SCRATCH'])
assert 'sf-ao-fix1' in str(SCRATCH.resolve())
_original_mkdtemp = tempfile.mkdtemp


def forbidden_command(words):
    """Inspect command data; never execute a forbidden command to test this guard."""
    executable = Path(words[0]).name if words else ''
    blocked = executable in ('security', 'agent-secret', 'launchctl')
    blocked = blocked or (executable == 'git' and any(word in ('push', 'stash') for word in words[1:]))
    if executable == 'gh':
        blocked = blocked or any(words[i:i+2] in (['pr', 'merge'], ['run', 'rerun'])
                                 for i in range(len(words)-1))
        methods = ('POST', 'PATCH', 'DELETE')
        blocked = blocked or ('api' in words and any(
            word in methods or any(word == prefix + method for prefix in ('-X', '-X=', '--method=')
                                   for method in methods) for word in words))
    if executable in ('sh', 'bash', 'zsh') and '-c' in words:
        shell = words[words.index('-c')+1]
        blocked = blocked or bool(re.search(
            r'\bgit\b[^;\n]*\b(?:push|stash)\b|\bgh\s+(?:pr\s+merge|run\s+rerun|api\b.*(?:POST|PATCH|DELETE))|\b(?:security|agent-secret|launchctl)\b', shell))
    return blocked


def _scoped_mkdtemp(suffix=None, prefix=None, dir=None):
    if dir is None or Path(dir).resolve() in (Path('/private/tmp'), Path('/tmp').resolve()):
        dir = str(SCRATCH)
    if 'sf-ao-fix1' not in str(Path(dir)):
        prefix = 'sf-ao-fix1-' + (prefix or '')
    path = _original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
    with (SCRATCH / 'created-dirs.jsonl').open('a') as stream:
        stream.write(json.dumps(path) + '\n')
    return path

@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    os.environ['TMPDIR'] = str(SCRATCH)
    tempfile.tempdir = str(SCRATCH)
    tempfile.mkdtemp = _scoped_mkdtemp
    from tests import failed_keep
    failed_keep.ROOT = SCRATCH / 'failed'


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    from tests.unit import retention_world

    def local_transfer(self, ref='HEAD:main'):
        source, _, destination = ref.partition(':')
        destination = destination or source
        if not destination.startswith('refs/'):
            destination = 'refs/heads/' + destination
        retention_world.git(self.remote, 'fetch', '--quiet', str(self.repo),
                            source + ':' + destination)
        retention_world.git(self.repo, 'fetch', '--quiet', str(self.remote),
                            '+refs/heads/*:refs/remotes/origin/*')

    retention_world.World.push = local_transfer

    def refuse_mutations(event, args):
        if event != 'subprocess.Popen':
            return
        command = args[1]
        if isinstance(command, (str, bytes)):
            command = [os.fsdecode(command)]
        words = [os.fsdecode(word) for word in command]
        if forbidden_command(words):
            pytest.skip('job brief forbids executing this mutation, including fixture executables')

    sys.addaudithook(refuse_mutations)
