"""Run the tracked PATH shim against fake Codex and native CLI executables."""

import json
import os
from pathlib import Path
import sys

import pytest

from tests import waits


ROOT = Path(__file__).resolve().parents[2]
SHIM = ROOT / "bin" / "codex"


@pytest.fixture
def shim(tmp_path):
    home = tmp_path / "home"
    real = home / ".bun/bin/codex"
    real.parent.mkdir(parents=True)
    real.write_text(f"#!{sys.executable}\n" + """import json,os,sys
from pathlib import Path
Path(os.environ['CHILD_LOG']).write_text(json.dumps({'argv':sys.argv[1:],'home':os.environ.get('CODEX_HOME'),'stdin':sys.stdin.read(),'api_env':{key:os.environ[key] for key in ('CODEX_API_KEY','OPENAI_API_KEY') if key in os.environ}}))
print('provider-output')
raise SystemExit(int(os.environ.get('CHILD_RC','0')))
""")
    real.chmod(0o755)
    native = home / ".local/share/subfleet/current/venv/bin"
    native.mkdir(parents=True)
    (native / "python").symlink_to(sys.executable)
    (native / "subfleet").write_text("import json,os,sys\nfrom pathlib import Path\n"
        + f"sys.path.insert(0,{str(ROOT)!r})\n" + """
assert sys.flags.ignore_environment and sys.flags.safe_path
with Path(os.environ['CALL_LOG']).open('a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1]=='pick':
    print(os.environ.get('PICK_OUTPUT',''))
    raise SystemExit(int(os.environ.get('PICK_RC','0')))
from subfleet.cli import main
raise SystemExit(main(sys.argv[1:]))
""")
    picked = home / "lane with spaces"
    picked.mkdir()
    child_log, call_log = tmp_path / "child.json", tmp_path / "calls.jsonl"
    env = {key: value for key, value in os.environ.items()
           if key not in ("CODEX_HOME", "SUBFLEET_NO_AUTOPICK", "SUBFLEET_ALLOW_API_LANE",
                          "CODEX_API_KEY", "OPENAI_API_KEY")}
    env.update(HOME=str(home), PICK_OUTPUT=str(picked), CHILD_LOG=str(child_log), CALL_LOG=str(call_log),
               PATH="/usr/bin:/bin")
    def run(*args, **values):
        return waits.run(["/bin/bash", str(SHIM), *args], env={**env, **values},
                         input="prompt on stdin\n", capture_output=True, text=True, timeout=5)
    def calls():
        return [json.loads(line) for line in call_log.read_text().splitlines()] if call_log.exists() else []
    return run, picked, child_log, calls, native, real


@pytest.mark.parametrize("args,model", [
    (("exec", "-m", "gpt-6-astra", "do the task"), "gpt-6-astra"),
    (("e", "--model=gpt-5.6-terra", "-"), "gpt-5.6-terra"),
    (("--model", "gpt-6-astra", "exec", "-"), "gpt-6-astra"),
    (("exec", "-mgpt-6-astra", "-"), "gpt-6-astra"),
    (("review", "--base", "--model", "-"), None),
    (("exec", "--", "--model", "not-a-model"), None),
])
def test_shim_forwards_model_and_original_argv(shim, args, model):
    """C-11.2: the picker sees explicit scope; provider argv and stdin stay exact."""
    run, picked, child, calls, *_ = shim
    result = run(*args, CHILD_RC="13")
    assert result.returncode == 13 and result.stdout == "provider-output\n"
    assert calls()[0] == ["pick", "codex", *(["--model", model] if model else [])]
    assert calls()[1] == ["_api-lane-check", str(picked)]
    assert json.loads(child.read_text()) == {"argv": list(args), "home": str(picked),
                                           "stdin": "prompt on stdin\n", "api_env": {}}


@pytest.mark.parametrize("command", ["exec", "e", "review"])
@pytest.mark.parametrize("explicit", [False, True])
def test_noninteractive_subscription_launch_removes_api_environment(shim, command, explicit):
    """C-10.2: subscription authentication cannot inherit API billing credentials."""
    run, picked, child, calls, *_ = shim
    result = run(command, "-", CODEX_API_KEY="fake-codex-env-key", OPENAI_API_KEY="fake-openai-env-key",
                 **({"CODEX_HOME": str(picked)} if explicit else {}))
    assert result.returncode == 0
    assert json.loads(child.read_text())["api_env"] == {}
    assert calls()[-1] == ["_api-lane-check", str(picked)]
    assert "fake-codex-env-key" not in result.stdout + result.stderr
    assert "fake-openai-env-key" not in result.stdout + result.stderr


@pytest.mark.parametrize("values,code", [({"PICK_RC": "1"}, 1), ({"PICK_RC": "69"}, 69),
    ({"PICK_OUTPUT": ""}, 7), ({"PICK_OUTPUT": "relative/home"}, 7),
    ({"PICK_OUTPUT": "/does-not-exist"}, 7), ({"PICK_OUTPUT": "/first\n/second"}, 7)])
def test_failed_or_invalid_pick_never_starts_child(shim, values, code):
    """C-10.4: picker failure cannot fall through to the desktop default home."""
    run, _, child, calls, *_ = shim
    result = run("exec", "-", **values)
    assert result.returncode == code and not child.exists() and not result.stdout
    assert len(calls()) == 1


@pytest.mark.parametrize("explicit", [False, True])
def test_known_api_home_refused_even_with_legacy_override(shim, explicit):
    """C-10.2: the actual native API checker guards picked and explicit homes."""
    run, picked, child, calls, *_ = shim
    (picked / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "fake-test-key"}))
    result = run("exec", "-", SUBFLEET_ALLOW_API_LANE="1",
                 **({"CODEX_HOME": str(picked)} if explicit else {}))
    assert result.returncode == 7 and not child.exists() and not result.stdout
    assert "API-key" in result.stderr and "fake-test-key" not in result.stderr
    assert calls()[-1] == ["_api-lane-check", str(picked)]
    if explicit:
        assert len(calls()) == 1


@pytest.mark.parametrize("args", [(), ("login",), ("resume", "session"),
    ("--model", "gpt-6-astra", "interactive prompt"), ("exec", "--help")])
def test_interactive_and_help_passthrough_need_no_subfleet(shim, args):
    """Permanent PATH contract: interactive commands are outside automatic picking."""
    run, _, child, calls, native, _ = shim
    (native / "subfleet").unlink()
    result = run(*args, CODEX_API_KEY="fake-interactive-codex-key", OPENAI_API_KEY="fake-interactive-openai-key")
    assert result.returncode == 0 and child.exists() and calls() == []
    assert json.loads(child.read_text())["argv"] == list(args)
    assert json.loads(child.read_text())["api_env"] == {
        "CODEX_API_KEY": "fake-interactive-codex-key", "OPENAI_API_KEY": "fake-interactive-openai-key"}


def test_explicit_home_skips_pick_but_keeps_guard(shim):
    """C-10.2: an identity pin cannot rotate or disable subscription validation."""
    run, picked, child, calls, *_ = shim
    result = run("exec", "resume", "session", CODEX_HOME=str(picked), SUBFLEET_NO_AUTOPICK="1")
    assert result.returncode == 0 and child.exists()
    assert calls() == [["_api-lane-check", str(picked)]]


@pytest.mark.parametrize("args,values", [(("exec", "resume", "session"), {}),
    (("exec", "fork", "session"), {}), (("exec", "-"), {"SUBFLEET_NO_AUTOPICK": "1"})])
def test_unpinned_continuation_and_optout_refuse(shim, args, values):
    """C-12: raw continuation requires its source home; opt-out cannot choose desktop."""
    run, _, child, calls, *_ = shim
    result = run(*args, **values)
    assert result.returncode == 7 and not child.exists() and not calls()


def test_missing_native_install_refuses_noninteractive_work(shim):
    """Completed cutover: no v1 CLI or unchecked provider fallback is reachable."""
    run, _, child, calls, native, _ = shim
    (native / "subfleet").unlink()
    assert run("exec", "-").returncode == 69
    assert not child.exists() and not calls()


def test_shim_does_not_resolve_itself_as_real_codex(shim):
    """C-5: self-referential PATH installation cannot recurse indefinitely."""
    run, _, child, calls, _, real = shim
    real.unlink()
    real.symlink_to(SHIM)
    assert run("exec", "-").returncode == 127
    assert not child.exists() and not calls()
