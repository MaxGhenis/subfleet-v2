"""Apply each requested mutant alone, test it, restore and verify pristine bytes."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

DATA = Path(__file__).resolve().parent
REPO = DATA.parents[2]
SCRATCH = Path('/private/tmp/sf-eff-b1') / 'pristine'
SCRATCH.mkdir(exist_ok=True)
JOB = REPO / 'subfleet/adapters/claude.py'
TURN = REPO / 'subfleet/conversations/claude_turn.py'
pristine = {path: path.read_bytes() for path in (JOB, TURN)}
for path in pristine:
    shutil.copy2(path, SCRATCH / path.name)
job_pin = 'settings=lane_settings(effort=effort))  # C-12.4, D-EFFORT-ENV1'
job_test = 'test_property_job_effort_pins_settings_and_preserves_argv'
turn_test = 'test_property_turn_effort_pins_settings_and_preserves_argv'


def replace_once(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)


def env_add_mutant(text):
    text = replace_once(text, job_pin, 'settings=lane_settings())  # C-12.4, D-EFFORT-ENV1')
    start, end = text.index('    def build_launch('), text.index('    def resume_launch(')
    section = replace_once(text[start:end], 'env_add=dict(credential_env),',
                           'env_add={**credential_env, **({"CLAUDE_CODE_EFFORT_LEVEL": effort} if effort else {})},')
    return text[:start] + section + text[end:]


mutants = [
    ('1-drop-job-pin', JOB, lambda text: replace_once(text, job_pin, 'settings=lane_settings())  # C-12.4, D-EFFORT-ENV1'), job_test),
    ('2-drop-turn-pin', TURN, lambda text: replace_once(text, 'lane_settings_args(settings, effort=effort)', 'lane_settings_args(settings)'), turn_test),
    ('3-job-process-env', JOB, env_add_mutant, job_test),
    ('4-ultracode-string', TURN, lambda text: replace_once(text, 'lane_settings_args(settings, effort=effort)', 'lane_settings_args(settings, effort=spec.effort)'), turn_test),
    ('5-pin-unnamed-job', JOB, lambda text: replace_once(text, job_pin, 'settings=lane_settings(effort=effort or "high"))  # C-12.4, D-EFFORT-ENV1'), job_test),
    ('6-second-settings', JOB, lambda text: replace_once(text, job_pin, job_pin + '\n        argv += ["--settings", "{}"]'), job_test),
    ('7-lose-watchdog', JOB, lambda text: replace_once(text, 'merged["env"] = {**dict(merged.get("env") or {}), **LANE_SETTINGS_ENV}', 'merged["env"] = {**dict(merged.get("env") or {})}'), job_test),
]
results = []
try:
    for label, path, change, test in mutants:
        assert all(p.read_bytes() == content for p, content in pristine.items())
        path.write_text(change(pristine[path].decode()))
        try:
            done = subprocess.run([sys.executable, str(DATA / 'run-tests.py'), 'mutant-' + label,
                                   f'tests/unit/test_effort_env.py::{test}', '-q', '-x', '--tb=short'], cwd=REPO)
            log = (DATA / f'mutant-{label}.log').read_text()
            killed = done.returncode == 1 and f'FAILED tests/unit/test_effort_env.py::{test}' in log
            results.append({'mutant': label, 'test': test, 'rc': done.returncode, 'killed': killed})
            assert killed, results[-1]
        finally:
            path.write_bytes((SCRATCH / path.name).read_bytes())
            assert path.read_bytes() == pristine[path]
        print(f'Restored pristine bytes after {label}', flush=True)
finally:
    for path, content in pristine.items():
        path.write_bytes(content)
        assert path.read_bytes() == content
    (DATA / 'mutants.json').write_text(json.dumps({'results': results, 'restored_sha256': {
        str(path.relative_to(REPO)): hashlib.sha256(content).hexdigest() for path, content in pristine.items()
    }}, indent=2) + '\n')
print(f'{len(results)} mutants killed; all production bytes restored', flush=True)
