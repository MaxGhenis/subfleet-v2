import json, os, pathlib, subprocess
root = pathlib.Path('/private/tmp/sf-eff-b1')
config = root / 'config'
config.mkdir(exist_ok=True)
(root / 'home').mkdir(exist_ok=True)
for name, user_env, setting, child in [
    ('user-low-cli-high', {'CLAUDE_CODE_EFFORT_LEVEL': 'low'}, None, None),
    ('settings-medium', {'CLAUDE_CODE_EFFORT_LEVEL': 'low'}, 'medium', None),
    ('user-without-effort', {}, None, None),
    ('child-env-max', {'CLAUDE_CODE_EFFORT_LEVEL': 'low'}, 'medium', 'max'),
]:
    (config / 'settings.json').write_text(json.dumps({'env': user_env}))
    remove = sorted({k for k in os.environ if k.startswith(('CLAUDE_CODE_', 'ANTHROPIC_'))} | {'CLAUDE_CODE_EFFORT_LEVEL', 'ANTHROPIC_API_KEY', 'CLAUDE_CONFIG_DIR', 'CLAUDE_CONFIG_DIR_READONLY', 'CLAUDE_CONFIG_JSON', 'CLAUDE_CREDENTIAL_FILE', 'CLAUDE_LANE_AGENT_SECRET', 'SUBFLEET_AGENT_SECRET'})
    command = ['env']
    for key in remove:
        command += ['-u', key]
    command += [f'HOME={root / "home"}', f'CLAUDE_CONFIG_DIR={config}', f'TMPDIR={root}', 'DISABLE_AUTOUPDATER=1', 'DISABLE_TELEMETRY=1', 'DISABLE_ERROR_REPORTING=1']
    if child:
        command += [f'CLAUDE_CODE_EFFORT_LEVEL={child}']
    command += ['/Users/maxghenis/.local/bin/claude', '-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--effort', 'high']
    if setting:
        command += ['--settings', json.dumps({'env': {'CLAUDE_CODE_EFFORT_LEVEL': setting}}, separators=(',', ':'))]
    request = {'type': 'control_request', 'request_id': 'sf-eff-b1-settings', 'request': {'subtype': 'get_settings'}}
    try:
        result = subprocess.run(command, input=json.dumps(request)+'\n', cwd=root, text=True, capture_output=True, timeout=45)
        record = {'case': name, 'argv': command, 'rc': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    except subprocess.TimeoutExpired as e:
        record = {'case': name, 'timeout': 45, 'stdout': str(e.stdout), 'stderr': str(e.stderr)}
    (root / f'{name}.json').write_text(json.dumps(record, indent=2)+'\n')
    print(json.dumps(record), flush=True)
