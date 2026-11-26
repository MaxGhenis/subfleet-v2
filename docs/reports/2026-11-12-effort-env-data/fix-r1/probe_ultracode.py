"""Ultracode + effort pin probe (fix r1, finding 3). get_settings only unless --message.

Builds the turn argv with the worktree's own `claude_turn.argv`, swaps in the real
binary, and asks `get_settings`. Isolated mode uses a fresh HOME/CLAUDE_CONFIG_DIR
and strips every Claude/Anthropic variable, so no credential is present. Lane mode
keeps the inherited HOME and login (no CLAUDE_CONFIG_DIR is set in this job), strips
the session's own CLAUDE_CODE_* markers and effort variables, and never prints
credentials. Output: one JSON line per case with argv settings and `applied`.
"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

WORKTREE, SCRATCH, MODE = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
MESSAGE = "--message" in sys.argv
sys.path.insert(0, str(WORKTREE))
from subfleet.conversations import claude_turn  # noqa: E402
from subfleet.conversations.turn import TurnSpec  # noqa: E402
import dataclasses  # noqa: E402

BINARY = "/Users/maxghenis/.local/bin/claude"
MODEL = os.environ.get("PROBE_MODEL", "claude-opus-5-5")
EFFORT_ENV = "CLAUDE_CODE_EFFORT_LEVEL"


def spec(permission: str, effort: str | None) -> TurnSpec:
    fields = {f.name for f in dataclasses.fields(TurnSpec)}
    values = dict(provider="claude", message_id="m-1", text="Reply with OK.", model_id=MODEL, effort=effort, permission=permission, fast=False,
                  native_session_id=None, new_session_id=str(uuid.uuid4()))
    required = {f.name for f in dataclasses.fields(TurnSpec)
                if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}
    missing = required - set(values)
    if missing:
        raise SystemExit(f"TurnSpec needs {sorted(missing)}")
    return TurnSpec(**{k: v for k, v in values.items() if k in fields})


def run(name: str, argv: list[str], user_env: dict | None) -> dict:
    case_dir = SCRATCH / "probe" / MODE / name
    home, config, work = (case_dir / p for p in ("home", "config", "work"))
    for p in (home, config, work):
        p.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_CODE_", "ANTHROPIC_", "CLAUDE_"))
           and k not in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")
           and not (MODE == "lane" and k.startswith("SUBFLEET_"))}
    env.update(TMPDIR=str(case_dir), DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1",
               DISABLE_ERROR_REPORTING="1")
    if MODE == "isolated":
        env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(config))
        (config / "settings.json").write_text(json.dumps({"env": user_env or {}}))
    argv = [BINARY, *argv[1:]]
    if MODE == "lane":
        argv.insert(argv.index("--settings"), "--no-session-persistence")
    lines = []
    if MESSAGE:
        lines.append({"type": "user", "message": {"role": "user", "content": "Reply with OK."}})
    lines.append({"type": "control_request", "request_id": f"sf-eff-f1-{name}",
                  "request": {"subtype": "get_settings"}})
    done = subprocess.run(argv, input="".join(json.dumps(x) + "\n" for x in lines), cwd=work,
                          env=env, capture_output=True, text=True, timeout=300)
    rows = [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]
    answer = [r["response"] for r in rows if r.get("type") == "control_response"
              and r.get("response", {}).get("request_id") == f"sf-eff-f1-{name}"]
    applied = answer[0].get("response", {}).get("applied") if answer and answer[0].get("subtype") == "success" else None
    result = [r for r in rows if r.get("type") == "result"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    return {"case": name, "mode": MODE, "model": MODEL, "rc": done.returncode,
            "settings": settings, "effort_flag": argv[argv.index("--effort") + 1] if "--effort" in argv else None,
            "applied": applied, "answer_subtype": answer[0].get("subtype") if answer else None,
            "result_subtype": result[-1].get("subtype") if result else None,
            "result_is_error": result[-1].get("is_error") if result else None,
            "stderr_tail": done.stderr[-600:]}


head = claude_turn.argv(spec("bypass", "ultracode"))
index = head.index("--settings")
payload = json.loads(head[index + 1])
base_payload = json.loads(json.dumps(payload))
del base_payload["env"][EFFORT_ENV]
base = head[:index + 1] + [json.dumps(base_payload, separators=(",", ":"))] + head[index + 2:]
high_payload = json.loads(json.dumps(payload))
high_payload["env"][EFFORT_ENV] = "high"
pinned_high = head[:index + 1] + [json.dumps(high_payload, separators=(",", ":"))] + head[index + 2:]
if MODE == "isolated":
    cases = [("u0-base-no-pin", base, None), ("u1-head-pin", head, None),
             ("u2-head-pin-user-low", head, {EFFORT_ENV: "low"}),
             ("u3-control-pin-high", pinned_high, None),
             ("u4-control-base-user-low", base, {EFFORT_ENV: "low"})]
    if "--only-controls" in sys.argv:
        cases = cases[3:]
else:
    cases = [("u1-head-pin", head, None)]
for name, argv, user_env in cases:
    print(json.dumps(run(name, argv, user_env), sort_keys=True), flush=True)
