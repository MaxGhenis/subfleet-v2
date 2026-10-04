#!/usr/bin/env python3
"""Full-scope login drive: which accounts are logged in under ~/.subfleet/logins/<email>/,
and the slack table from the usage endpoint for those that are (C-9.9, C-11.7).

    uv run python tools/login_table.py [--logins ~/.subfleet/logins] [--cap-ratio 2.0]

Reads `claude auth status --json` per config directory (local), then one paced usage GET
per logged-in account with that directory's own credential, never printing a token.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from subfleet.adapters.claude import OAUTH_USAGE_BETA, OAUTH_USAGE_URL, ClaudeAdapter


def status(config_dir: Path) -> dict:
    result = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin:/usr/local/bin:" + str(Path.home() / ".local/bin"),
                                 "HOME": str(Path.home()), "CLAUDE_CONFIG_DIR": str(config_dir)}, timeout=30)
    try:
        return json.loads(result.stdout or "{}")
    except ValueError:
        return {"loggedIn": False, "error": result.stderr.strip()[:120]}


def usage(token: str) -> tuple[str, dict | None]:
    request = urllib.request.Request(OAUTH_USAGE_URL, headers={"Authorization": f"Bearer {token}",
                                                               "anthropic-beta": OAUTH_USAGE_BETA})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return "ok", json.load(response)
    except urllib.error.HTTPError as error:
        retry = error.headers.get("Retry-After") if error.headers else None
        return f"HTTP {error.code}" + (f" retry-after {retry}" if retry else ""), None
    except OSError as error:
        return type(error).__name__, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--logins", default="~/.subfleet/logins")
    ap.add_argument("--cap-ratio", type=float, default=2.0)
    ap.add_argument("--spacing", type=float, default=4.0)
    args = ap.parse_args()
    root = Path(args.logins).expanduser()
    plan = json.loads((root / "plan.json").read_text())
    print(f"{'n':>2} {'account':28} {'login':10} {'5h%':>5} {'all%':>5} {'Fable%':>7} {'slack':>6}  note")
    for entry in plan:
        config_dir = Path(entry["config_dir"])
        state = status(config_dir)
        if not state.get("loggedIn"):
            print(f"{entry['n']:>2} {entry['email']:28} {'pending':10}")
            continue
        who = state.get("email") or state.get("account", {}).get("email") or "?"
        login = "ok" if who == entry["email"] else f"WRONG:{who}"
        token = ClaudeAdapter._bearer({"CLAUDE_CONFIG_DIR": str(config_dir)})
        if not token:
            print(f"{entry['n']:>2} {entry['email']:28} {login:10} {'':>5} {'':>5} {'':>7} {'':>6}  credential not in {config_dir}/.credentials.json")
            continue
        code, payload = usage(token)
        time.sleep(args.spacing)
        if payload is None:
            print(f"{entry['n']:>2} {entry['email']:28} {login:10} {'':>5} {'':>5} {'':>7} {'':>6}  {code}")
            continue
        five = (payload.get("five_hour") or {}).get("utilization")
        shared = (payload.get("seven_day") or {}).get("utilization")
        fable = next((l.get("percent") for l in payload.get("limits") or [] if l.get("kind") == "weekly_scoped"
                      and ((l.get("scope") or {}).get("model") or {}).get("display_name") == "Fable"), None)
        slack = None if shared is None or fable is None else round((100 - shared) - args.cap_ratio * (100 - fable), 1)
        note = "" if slack is None else ("Opus may spend the slack" if slack > 0 else "FABLE-ONLY")
        fmt = lambda v: "?" if v is None else f"{v:.0f}"
        print(f"{entry['n']:>2} {entry['email']:28} {login:10} {fmt(five):>5} {fmt(shared):>5} {fmt(fable):>7} {fmt(slack) if slack is not None else '?':>6}  {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
