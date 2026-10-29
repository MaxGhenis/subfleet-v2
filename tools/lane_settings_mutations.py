"""Reproduce D-WATCHDOG1's mutants (d1031), one pytest per mutant.

Each mutant edits one site, runs its killing tests from tests/unit/test_lane_settings.py
with -k, and puts the file back whatever happens. A mutant is killed only when every
failure is an assertion and nothing errors at collection or setup.

    TMPDIR=<scratch> python tools/lane_settings_mutations.py [--only NAME ...]

Logs and JUnit reports go to $TMPDIR/lane-settings-mutants, never into the tree.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ADAPTER = 'subfleet/adapters/claude.py'
CARDS = 'subfleet/claude_cards.py'
PUBLISH = 'subfleet/review_publish.py'
TURN = 'subfleet/conversations/claude_turn.py'
TESTS = 'tests/unit/test_lane_settings.py'

# (name, file, before, after, -k expression)
MUTANTS = (
    ('heal-without-override', CARDS,
     """'{"mcpServers":{}}', *lane_settings_args()]""", """'{"mcpServers":{}}']""",
     'i1 and heal'),
    ('read-only-fast-turn-second-settings', TURN,
     '    return command + lane_settings_args(settings)',
     '    if spec.permission == "read-only" and settings.pop("fastMode", False):\n'
     '        return command + lane_settings_args(settings) + ["--settings", \'{"fastMode":true}\']\n'
     '    return command + lane_settings_args(settings)',
     'i1 and turn and fast=True and permission=read-only'),
    ('value-one', ADAPTER,
     'MappingProxyType({"CLAUDE_CODE_RETRY_WATCHDOG": "0"})',
     'MappingProxyType({"CLAUDE_CODE_RETRY_WATCHDOG": "1"})',
     'i1'),
    ('override-drops-other-keys', ADAPTER,
     '    merged = dict(settings or {})\n', '    merged = {}\n',
     'i2 and turn and permission=ask'),
    ('read-only-jobs-skipped', PUBLISH,
     '        return ["--settings", json.dumps(settings, separators=(",", ":"))]',
     '        return [] if sandbox != Sandbox.WORKSPACE_WRITE.value else '
     '["--settings", json.dumps(settings, separators=(",", ":"))]',
     'i1 and job and sandbox=read-only'),
)


def edit(source: str, before: str, after: str) -> str:
    assert source.count(before) == 1, (before, source.count(before))
    return source.replace(before, after)


def run(name: str, expression: str, logs: Path) -> bool:
    report = logs / f'{name}.xml'
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', TESTS, '-k', expression,
                             f'--junitxml={report}'], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (logs / f'{name}.log').write_text(result.stdout)
    failures, errors = [], 0
    if report.exists():
        for case in ET.parse(report).getroot().iter('testcase'):
            failures += [f.get('message') or '' for f in case.findall('failure')]
            errors += len(case.findall('error'))
    killed = (result.returncode == 1 and errors == 0 and bool(failures)
              and all('assert' in message.lower() for message in failures))
    summary = [line for line in result.stdout.splitlines() if ' in ' in line and
               any(word in line for word in ('passed', 'failed', 'error', 'deselected'))]
    print(f'{name}: {"KILLED" if killed else "NOT KILLED"}; exit={result.returncode}; '
          f'failures={len(failures)} errors={errors}; {summary[-1:]!r}', flush=True)
    return killed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', nargs='+')
    args = parser.parse_args()
    logs = Path(os.environ.get('TMPDIR') or tempfile.gettempdir()) / 'lane-settings-mutants'
    logs.mkdir(parents=True, exist_ok=True)
    survivors = []
    for name, filename, before, after, expression in MUTANTS:
        if args.only and name not in args.only:
            continue
        path = Path(filename)
        original = path.read_bytes()
        try:
            changed = edit(original.decode(), before, after)
            compile(changed, filename, 'exec')
            path.write_text(changed)
            if not run(name, expression, logs):
                survivors.append(name)
        finally:
            path.write_bytes(original)
    return 1 if survivors else 0


if __name__ == '__main__':
    raise SystemExit(main())
