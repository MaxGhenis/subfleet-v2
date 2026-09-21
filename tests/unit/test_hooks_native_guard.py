"""The retained pre-bash hook protects native sessions without a v1 executable."""
import io
import json

import pytest

from subfleet import hooks


@pytest.mark.parametrize('command', [
    'codex exec "do work"', 'env CODEX_HOME=/isolated codex review',
    'cd /repo && /usr/local/bin/codex e "work"',
    'echo hello\nnohup subfleet codex -p prompt.md',
    '/home/bin/subfleet-claude -p prompt.md', 'claude-lane work',
    '(time subfleet claude work)', 'codex exec -d work',
    'echo SUBFLEET_ATTACHED_OK=1; codex exec work',
])
def test_attached_provider_launch_is_denied(command, capsys):
    payload = {'tool_name': 'Bash', 'tool_input': {'command': command}}
    assert hooks.run('pre-bash', stream=io.StringIO(json.dumps(payload))) == 0
    result = json.loads(capsys.readouterr().out)['hookSpecificOutput']
    assert result['hookEventName'] == 'PreToolUse' and result['permissionDecision'] == 'deny'
    assert 'subfleet run --task' in result['permissionDecisionReason']


@pytest.mark.parametrize('command', [
    'subfleet run --task research --tier standard -p prompt.md',
    'bash -n bin/subfleet-claude', 'echo "codex exec work"',
    "cat <<'PROMPT'\ncodex exec do something\nPROMPT\n",
    'SUBFLEET_ATTACHED_OK=1 codex exec work',
    'env SUBFLEET_ATTACHED_OK=1 /bin/codex review',
    'subfleet-codex -d -p prompt.md', 'codex --version',
])
def test_safe_mentions_and_explicit_override_remain_silent(command):
    output = io.StringIO()
    assert hooks.pre_tool_use({'tool_name': 'Bash', 'tool_input': {'command': command}}, stdout=output) == 0
    assert output.getvalue() == ''


def test_uninstall_removes_only_native_entries_with_backup(tmp_path):
    path = tmp_path / 'settings.json'
    old = {'theme': 'dark', 'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
        {'type': 'command', 'command': '/legacy/subfleet-hook pre-bash'},
        {'type': 'command', 'command': 'unrelated hook'},
    ]}]}}
    path.write_text(json.dumps(old))
    hooks.apply(path, command='/bin/sf hook')
    before = path.read_bytes()
    preview = hooks.plan(path, command='/bin/sf hook', remove=True)
    assert preview['changed_events'] and path.read_bytes() == before
    result = hooks.apply(path, command='/bin/sf hook', remove=True)
    assert result['written']
    after = json.loads(path.read_text())
    assert after['theme'] == 'dark' and after['hooks']['PreToolUse'] == old['hooks']['PreToolUse']
    assert not hooks.plan(path, command='/bin/sf hook', remove=True)['changed_events']
