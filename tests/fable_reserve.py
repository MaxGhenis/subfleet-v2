"""A policy that still reserves Fable, for the rules the shipped policy no longer exercises.

Fable was retired from dispatch on 2026-09-27 (Max: "opus 5.5 is strictly better than
fable"). The shipped policy routes no task to it, lists `fable` and its ids only as
`retired` aliases of `opus`, and reserves no model. The rules Fable used to exercise
remain: the reserve rule (C-11.7), stranded-capacity preference across chains
(C-23.37), and model-scoped closures. Claude accounts also still report Fable's own
weekly bucket. So the cases for those rules keep their recorded evidence and run
against this explicit policy: the shipped one, with Fable restored as a model, its
older id renamed onto it, and its bucket reserved. It is the shipped file as it stood
before the retirement.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from typing import Any

from subfleet.policy import DEFAULT_POLICY_PATH, load_policy

#: The model entry the shipped policy carried until 2026-09-27.
FABLE = {"provider": "claude", "id": "claude-fable-5-1", "priority": 4, "scope": "fable"}
WRITING_TASKS = ("authored-prose", "strategy", "adjudication")


def fable_reserve_data(*, writing_chains: bool = False) -> dict[str, Any]:
    """The shipped policy's JSON with Fable restored and reserved (unvalidated)."""
    data = json.loads(DEFAULT_POLICY_PATH.read_text())
    data = copy.deepcopy(data)
    data["models"] = {"fable": dict(FABLE), **data["models"]}
    data["retired"] = {alias: target for alias, target in data["retired"].items()
                       if alias not in ("fable", "claude-fable-5", FABLE["id"])}
    data["retired"]["claude-fable-5"] = "fable"
    data["reserve"] = {**data.get("reserve", {}), "models": ["fable"]}
    if writing_chains:
        for task in WRITING_TASKS:
            data["chains"][task] = ["fable"] * len(data["tiers"])
    return data


def write_fable_reserve_policy(path: Path, **options: Any) -> Path:
    path = Path(path)
    path.write_text(json.dumps(fable_reserve_data(**options), indent=2) + "\n")
    return path


def load_fable_reserve_policy(**options: Any) -> dict[str, Any]:
    """Validated through `load_policy`, as the daemon would load it."""
    with tempfile.TemporaryDirectory() as directory:
        return load_policy(write_fable_reserve_policy(Path(directory) / "policy.json", **options))
