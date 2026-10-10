"""Independent external-agent oracle and fake ruling files; no test decorators."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 10, 9, 16, 33, tzinfo=timezone.utc)


def stamp(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


LOWER, RAISE = "/fake/lower", "/fake/raise"


class Files:
    def __init__(self, lower=None, raised=None):
        self.files = {LOWER: lower, RAISE: raised}
        self.reads = []

    def __call__(self, path):
        self.reads.append(path)
        value = self.files[path]
        if value is None:
            raise FileNotFoundError(path)
        if isinstance(value, Exception):
            raise value
        return value


def file(value, written=0):
    return json.dumps(value).encode(), BASE.timestamp() + written


def lowering(floor=30, until=3600, ruling="Max", **extra):
    return {"floor_gb": floor, "until": stamp(until), "ruling": ruling, **extra}


def agent_rule(cfg, files, seconds):
    """Oracle copied independently from floor_gb, lower_override, pass_once.

    Return floor, margin, winning source and ignored reason codes. Only the
    agent's top-level non-object crash is totalized into an error here.
    """
    now = datetime.fromisoformat(stamp(seconds).replace("Z", "+00:00"))
    lo, raised, reasons = None, None, set()
    for name, path in (("lower", cfg["lower_path"]), ("override", cfg["raise_path"])):
        if path is None:
            continue
        try:
            data, mtime = files(path)
            ov = json.loads(data.decode("utf-8"))
            until = datetime.fromisoformat(str(ov["until"]).replace("Z", "+00:00"))
            if until.tzinfo is None:
                until = until.astimezone()
            floor = float(ov["floor_gb"])
            if name == "lower":
                ruling = str(ov.get("ruling") or "").strip()
                margin = max(0.0, float(ov.get("release_margin_gb", 0.0)))
                # Agent validates these, although the native rule ignores them.
                if ov.get("drop_gb") is not None:
                    float(ov["drop_gb"])
                float(ov.get("drop_window_min", 10.0))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            reasons.add(name + "_error")
            continue
        if until <= now or (name == "override" and floor != floor):
            reasons.add(name + "_expired")
            continue
        if name == "lower":
            written = datetime.fromtimestamp(mtime, timezone.utc)
            if ((until - min(now, written)).total_seconds() > cfg["max_lower_h"] * 3600
                    or not ruling or not floor >= cfg["min_floor_gb"]):
                reasons.add("lower_refused")
                continue
            lo = (floor, margin)
        else:
            raised = floor
    floor = cfg["floor_gb"] if raised is None else max(cfg["floor_gb"], raised)
    margin = cfg["resume_margin_gb"]
    source = "override" if raised is not None and raised > cfg["floor_gb"] else "policy"
    if lo is not None:
        if raised is not None and raised > lo[0]:
            floor, source = raised, "override"
        else:
            floor, margin = lo
            source = "lower"
    return floor, margin, source, reasons


def policy(**settings):
    return {"admission": {"disk": {"enabled": True, "lower_path": LOWER, "raise_path": RAISE, **settings}}}

