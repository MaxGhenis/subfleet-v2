"""Check an installed guard with inert data; never dispatch these command arrays."""

import json
import sitecustomize as guard


cases = (
    ('ps', '/bin/ps', ['/bin/ps', '-p', '99999999']),
    ('lsof', '/usr/sbin/lsof', ['/usr/sbin/lsof', '-p', '99999999']),
    ('security', '/nonexistent-sf-fix1/security', ['security', 'find-generic-password', '-s', 'nonexistent-sf-fix1-service']),
    ('agent-secret', '/nonexistent-sf-fix1/agent-secret', ['agent-secret', 'get', 'nonexistent-sf-fix1-service']),
    ('launchctl', '/bin/launchctl', ['launchctl', 'print', 'nonexistent-sf-fix1-domain']),
    ('git-push', '/usr/bin/git', ['git', 'push', 'nonexistent-sf-fix1-remote', 'nonexistent-sf-fix1-ref']),
    ('gh-pr-merge', '/nonexistent-sf-fix1/gh', ['gh', 'pr', 'merge', '99999999', '--repo', 'nonexistent-sf-fix1/absent']),
    ('gh-api-write', '/nonexistent-sf-fix1/gh', ['gh', 'api', '-X', 'POST', 'repos/nonexistent-sf-fix1/absent/issues']),
    ('gh-run-rerun', '/nonexistent-sf-fix1/gh', ['gh', 'run', 'rerun', '99999999', '--repo', 'nonexistent-sf-fix1/absent']),
)
outcomes = []
for label, executable, argv in cases:
    try:
        guard._check_exec(executable, argv)
    except PermissionError:
        outcomes.append({'check': label, 'result': 'refused as inert data'})
    else:
        raise AssertionError('guard accepted forbidden inert command data: ' + label)

assert guard._procs.Darwin.pids is guard._native_denied
try:
    guard._procs.kernel.pids()
except guard._procs.InspectionError:
    outcomes.append({'check': 'native-pid-list', 'result': 'replaced reader refused before native call'})
else:
    raise AssertionError('guard accepted native PID-list request')

guard._check_exec('/usr/bin/true', ['/usr/bin/true'])
outcomes.append({'check': 'permitted-command-data', 'result': 'accepted as inert data'})
if guard._GUARD_VERSION == 'sf-a2-fix1-strict-v2':
    nested = (
        ('taskpolicy-ps', ['ps', '-p', '99999999']),
        ('taskpolicy-security', ['security', 'find-generic-password', '-s', 'nonexistent-sf-fix1-service']),
        ('taskpolicy-git-push', ['/usr/bin/git', 'push', 'nonexistent-sf-fix1-remote']),
        ('taskpolicy-gh-write', ['gh', 'api', '-X', 'POST', 'repos/nonexistent-sf-fix1/absent/issues']),
    )
    for label, inner in nested:
        try:
            guard._check_exec('/usr/sbin/taskpolicy', ['taskpolicy', '-c', 'utility', *inner])
        except PermissionError:
            outcomes.append({'check': label, 'result': 'nested forbidden inert data refused'})
        else:
            raise AssertionError('guard accepted nested forbidden data: ' + label)
    try:
        guard._check_exec('/usr/sbin/taskpolicy', ['taskpolicy', '-c', 'background', '/usr/bin/true'])
    except PermissionError:
        outcomes.append({'check': 'taskpolicy-other-prefix', 'result': 'unapproved inert prefix refused'})
    else:
        raise AssertionError('guard accepted an unapproved taskpolicy prefix')
    guard._check_exec('/usr/sbin/taskpolicy', ['taskpolicy', '-c', 'utility', '/usr/bin/git',
                                           '-C', '/nonexistent-sf-fix1-repo', 'status'])
    outcomes.append({'check': 'taskpolicy-safe-git-data', 'result': 'accepted as inert data'})
    assert not guard._approved_frontend(guard._ROOT / 'nonexistent-sf-fix1-native-probe')
    outcomes.append({'check': 'unapproved-native-probe', 'result': 'unapproved nonexistent path refused'})
print(json.dumps({'guard_version': guard._GUARD_VERSION, 'proofs': len(outcomes),
                  'external_commands_executed': 0, 'outcomes': outcomes}))
