"""D-CODEX-0169: move the installed guard TRUST's Codex pin from 0.159.0 to 0.169.1.

The hub's install kit for docs/reports/2026-11-17-codex-0169-pin.md. It changes the
installed TRUST only, never the installed hook. The installed hook (e869dd4c...) predates
D-SEARCH-REL1's (af5e7239..., #298), whose install is a separate decision. So the
hub installs the installed TRUST with this PR's two edits, the codex_version line and
the appended provenance sentence, not the repository's TRUST. `stage` derives it and
proves that it differs from the repository's TRUST only in hook_sha256.

Run it from a checkout of this commit with the installed runtime, which can import
subfleet.guard.preflight:

    PY=~/.local/share/subfleet/current/venv/bin/python
    $PY -E -P tools/guard_pin_0169.py stage --live ~/.subfleet/guard \\
        --stage ~/.subfleet/guard-stage-codex-0169 --codex PATH --lane-home HOME ...
    $PY -E -P tools/guard_pin_0169.py swap --live ~/.subfleet/guard --stage ~/.subfleet/guard-stage-codex-0169
    $PY -E -P tools/guard_pin_0169.py rollback --live ~/.subfleet/guard

`stage` refuses unless the live pair is the expected one (hook e869dd4c..., TRUST
36267abb...) and the derived TRUST is the pinned 0224cb3f.... It refuses a stage
folder that exists or sits inside `--live`, and with `--codex` it imports the
preflight before it writes anything. It runs the guard preflight once for each
`--lane-home` given, using the live hook, the staged TRUST and no cache. A lane whose
preflight raises counts as not verified. It writes the PREFLIGHT record, which lists
the lanes, only if every lane given verified, and it writes nothing under `--live`.
`swap` re-checks the live pair, re-derives the staged TRUST from the live one, and
checks the recorded digest, the pin and the PREFLIGHT record. It refuses if
TRUST.pre-0.169.1.bak exists. It writes the backup and then the new TRUST, each by a
temporary file and an atomic rename within the folder, so neither name ever holds
partial bytes.
`rollback` runs only while the live hook is still e869dd4c... and the live TRUST is the
pinned 0224cb3f... that `swap` installs, or still the pre-move TRUST after a `swap` that
stopped before its rename. It reads nothing from the checkout. It writes the backup's
bytes back by the same atomic rename, then renames the backup to
TRUST.pre-0.169.1.bak.rolled-back-<UTC time>, so a later `swap` can run from a fresh
stage. Once a later guard change replaces the hook or TRUST, `rollback` refuses.
None of the three touches the Codex CLI: the hub upgrades it right after `swap` and
downgrades it right after `rollback` (the report's steps).

Exit codes: 0 done, 1 a lane's preflight did not verify, 2 refused, 3 an OS error or a
runtime that cannot import subfleet.guard.preflight.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REPO_TRUST = ROOT / "private/guard/TRUST"
LIVE_HOOK_SHA256 = "e869dd4cac5402112c3a135531de861c84089290ee31779f52145474f678dfed"
LIVE_TRUST_SHA256 = "36267abbee6e414805defd0672138f0be9b8dd2fd8675f739c77df90332bafc0"
INSTALLED_TRUST_SHA256 = "0224cb3f9c1adb3ca2879bde1513d6b67cc4d0766bf19ba4cd8dd8770c283047"
OLD_VERSION_LINE = '"codex_version": "codex-cli 0.159.0"'
NEW_VERSION_LINE = '"codex_version": "codex-cli 0.169.1"'
SENTENCE_START = " 2026-11-17, D-CODEX-0169:"
TAIL = '"\n}\n'
BACKUP = "TRUST.pre-0.169.1.bak"
PREFLIGHT = "PREFLIGHT"


class Refusal(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def repo_trust_and_sentence(repo_trust: Path = REPO_TRUST) -> tuple[str, str]:
    try:
        text = repo_trust.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise Refusal(f"{repo_trust} is not UTF-8") from exc
    if text.count(SENTENCE_START) != 1 or not text.endswith(TAIL):
        raise Refusal(f"{repo_trust} does not carry exactly one D-CODEX-0169 sentence at its end")
    return text, text[text.index(SENTENCE_START):-len(TAIL)]


def derive(live_text: str, repo_text: str, sentence: str) -> str:
    """The live TRUST with this PR's two edits; equal to the repository's TRUST but for the hook hash."""
    if live_text.count(OLD_VERSION_LINE) != 1 or not live_text.endswith(TAIL) or SENTENCE_START in live_text:
        raise Refusal("the live TRUST is not the expected 0.159.0 pin")
    new = live_text.replace(OLD_VERSION_LINE, NEW_VERSION_LINE)[:-len(TAIL)] + sentence + TAIL
    key = '"hook_sha256": "'
    if repo_text.count(key) != 1:
        raise Refusal("the repository's TRUST does not carry exactly one hook_sha256")
    repo_hook = repo_text.split(key, 1)[1].split('"', 1)[0]
    if new.count(LIVE_HOOK_SHA256) != 1 or new.replace(LIVE_HOOK_SHA256, repo_hook) != repo_text:
        raise Refusal("the derived TRUST differs from the repository's in more than hook_sha256")
    return new


def derived_bytes(pre_move: bytes, repo_trust: Path = REPO_TRUST) -> bytes:
    try:
        text = pre_move.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refusal("the live TRUST is not UTF-8") from exc
    return derive(text, *repo_trust_and_sentence(repo_trust)).encode("utf-8")


def check_live(live: Path) -> tuple[bytes, bytes]:
    hook, trust = (live / "never-rules-hook.sh").read_bytes(), (live / "TRUST").read_bytes()
    if sha256(hook) != LIVE_HOOK_SHA256 or sha256(trust) != LIVE_TRUST_SHA256:
        raise Refusal(f"live pair is hook {sha256(hook)[:12]}..., TRUST {sha256(trust)[:12]}...; "
                      f"expected {LIVE_HOOK_SHA256[:12]}..., {LIVE_TRUST_SHA256[:12]}...")
    return hook, trust


def _inside(path: Path, folder: Path) -> bool:
    """Whether path is folder or under it, by file identity, so case and symlinks do not matter."""
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.exists() and os.path.samefile(candidate, folder):
            return True
    return False


def _preflight_function():
    from subfleet.guard.preflight import preflight
    return preflight


def stage(live: Path, stage_root: Path, *, codex: str | None, lane_homes: list[str],
          repo_trust: Path = REPO_TRUST, preflight_function=None) -> int:
    _hook, trust = check_live(live)
    new = derived_bytes(trust, repo_trust)
    if sha256(new) != INSTALLED_TRUST_SHA256:
        raise Refusal(f"the derived TRUST is {sha256(new)[:12]}..., not the pinned {INSTALLED_TRUST_SHA256[:12]}...")
    if stage_root.exists():
        raise Refusal(f"{stage_root} exists; stage into a fresh folder")
    if _inside(stage_root, live):
        raise Refusal(f"{stage_root} is inside {live}; stage outside the live guard")
    if codex and not lane_homes:
        raise Refusal("--codex needs at least one --lane-home")
    run = (preflight_function or _preflight_function()) if codex else None
    guard = stage_root / "guard"
    guard.mkdir(parents=True, mode=0o700)
    (guard / "TRUST").write_bytes(new)
    (guard / "TRUST").chmod(0o600)
    (stage_root / "TRUST.sha256").write_text(sha256(new) + "\n")
    print(f"staged TRUST {sha256(new)} ({len(new)} bytes) in {guard}")
    if run is None:
        print("no --codex: nothing preflighted, so `swap` refuses this stage")
        return 0
    verified, versions = [], set()
    for home in lane_homes:
        work = Path(tempfile.mkdtemp(prefix="work-", dir=stage_root))
        try:
            result = run(codex, home=home, workdir=work, hook_path=live / "never-rules-hook.sh",
                         trust_path=guard / "TRUST", use_cache=False, cache_directory=stage_root / "cache",
                         state_root=stage_root)
        except Exception as exc:   # noqa: BLE001 -- any failure leaves the lane unverified
            print(f"preflight {home}: raised {type(exc).__name__}: {exc}")
            continue
        print(f"preflight {home}: ok={result.ok} kind={result.kind} version={result.version} {result.message}")
        if result.ok:
            verified.append(home)
            versions.add(result.version)
    if len(verified) != len(lane_homes):
        print(f"{len(lane_homes) - len(verified)} of {len(lane_homes)} lanes did not verify; no {PREFLIGHT} record")
        return 1
    record = {"trust_sha256": sha256(new), "codex": codex, "versions": sorted(versions), "lanes": verified}
    (stage_root / PREFLIGHT).write_text(json.dumps(record, indent=1) + "\n")
    return 0


def _replace(directory: Path, data: bytes, name: str = "TRUST") -> None:
    fd, tmp = tempfile.mkstemp(prefix=".TRUST-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, directory / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _preflight_record(stage_root: Path) -> dict | None:
    try:
        record = json.loads((stage_root / PREFLIGHT).read_text())
    except (FileNotFoundError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def swap(live: Path, stage_root: Path, *, repo_trust: Path = REPO_TRUST) -> int:
    _hook, trust = check_live(live)
    staged = (stage_root / "guard/TRUST").read_bytes()
    digest = sha256(staged)
    if digest != (stage_root / "TRUST.sha256").read_text().strip():
        raise Refusal("the staged TRUST changed after `stage`")
    if staged != derived_bytes(trust, repo_trust):
        raise Refusal("the staged TRUST is not this checkout's derivation from the live TRUST")
    if digest != INSTALLED_TRUST_SHA256:
        raise Refusal(f"the staged TRUST is {digest[:12]}..., not the pinned {INSTALLED_TRUST_SHA256[:12]}...")
    record = _preflight_record(stage_root)
    if not record or record.get("trust_sha256") != digest or not record.get("lanes"):
        raise Refusal(f"{stage_root} has no passing {PREFLIGHT} record; stage again with --codex and --lane-home")
    backup = live / BACKUP
    if backup.exists():
        raise Refusal(f"{backup} exists")
    _replace(live, trust, BACKUP)
    if sha256(backup.read_bytes()) != LIVE_TRUST_SHA256:
        raise Refusal(f"{backup} does not hold the live TRUST")
    _replace(live, staged)
    print(f"live TRUST {sha256((live / 'TRUST').read_bytes())}; previous kept at {backup}; "
          f"preflighted on {', '.join(record['lanes'])}")
    return 0


def rollback(live: Path, *, now: datetime | None = None) -> int:
    backup_path = live / BACKUP
    if not backup_path.exists():
        raise Refusal(f"{backup_path} does not exist; nothing to roll back")
    backup = backup_path.read_bytes()
    if sha256(backup) != LIVE_TRUST_SHA256:
        raise Refusal(f"{backup_path} is not the pre-move TRUST")
    hook = (live / "never-rules-hook.sh").read_bytes()
    if sha256(hook) != LIVE_HOOK_SHA256:
        raise Refusal(f"the live hook is {sha256(hook)[:12]}..., not {LIVE_HOOK_SHA256[:12]}...: "
                      "a later guard change owns the pair, so this rollback no longer applies")
    aside = live / f"{BACKUP}.rolled-back-{(now or datetime.now(UTC)).strftime('%Y%m%dT%H%M%SZ')}"
    if aside.exists():
        raise Refusal(f"{aside} exists; wait a second and run rollback again")
    current = (live / "TRUST").read_bytes()
    if current == backup:
        what = "the live TRUST already is the pre-move TRUST (a swap that stopped before its rename)"
    elif sha256(current) == INSTALLED_TRUST_SHA256:
        _replace(live, backup)
        what = f"live TRUST {sha256((live / 'TRUST').read_bytes())} (the 0.159.0 pin)"
    else:
        raise Refusal(f"the live TRUST is {sha256(current)[:12]}..., neither the pinned {INSTALLED_TRUST_SHA256[:12]}... "
                      "that `swap` installs nor the pre-move TRUST: a later guard change owns it, "
                      "so this rollback no longer applies")
    os.rename(backup_path, aside)
    print(f"{what}; backup moved to {aside}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("stage", "swap", "rollback"):
        command = sub.add_parser(name)
        command.add_argument("--live", type=Path, required=True)
        if name != "rollback":
            command.add_argument("--stage", type=Path, required=True)
        if name == "stage":
            command.add_argument("--codex")
            command.add_argument("--lane-home", action="append", default=[])
    args = parser.parse_args(argv)
    live = args.live.expanduser()
    try:
        if args.command == "stage":
            return stage(live, args.stage.expanduser(), codex=args.codex,
                         lane_homes=[str(Path(h).expanduser()) for h in args.lane_home])
        if args.command == "swap":
            return swap(live, args.stage.expanduser())
        return rollback(live)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except ImportError as exc:
        print(f"error: {exc} (run with the installed runtime, which imports subfleet.guard.preflight)",
              file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
