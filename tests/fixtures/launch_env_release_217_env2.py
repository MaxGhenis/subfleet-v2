"""`launch_env.build_environment` as release/217 had it before helper scopes
carried the daemon's withheld names (review r2 of #248, P3-2): I2's reference.

Captured verbatim from 4a4d41f47a59cdc60c28da7ade8ee6038c7bc195. It runs over
the current module's helpers (`_HELPER_PASS`, `_selected`, `_present`,
`valid_name`), which that change left as they were.
"""

from collections.abc import Iterable, Mapping
import os

from subfleet import launch_env

BASELINE_COMMIT = "4a4d41f47a59cdc60c28da7ade8ee6038c7bc195"

SOURCE = '''def build_environment(additions: Mapping[str, str] | None = None,
                      removals: Iterable[str] = (), *,
                      pass_names: Iterable[str] | None = None,
                      environ: Mapping[str, str] | None = None,
                      withheld: Iterable[str] = ()) -> dict[str, str]:
    """Select parent names and explicit authority, then mandatory removals.

    Callers supply only their own additions; never pass an ambient environment
    as additions. Explicit policy names authorize inheritance, not values or
    patterns. `withheld` names are never taken from the parent, even when
    allowlisted or passed: lane credential references, which reach their own
    lane only as an addition. Reject malformed programmatic callers as well as
    policy files.
    """
    passed = frozenset(_HELPER_PASS.get() if pass_names is None else pass_names)
    if any(not valid_name(name) for name in passed):
        raise ValueError("environment pass-through requires exact variable names")
    withheld = frozenset(withheld)
    parent = os.environ if environ is None else environ
    selected = _selected(parent)
    result = _present(parent, (name for name in parent
                               if (selected(name) or name in passed) and name not in withheld))
    result.update(additions or {})
    for name in removals:
        result.pop(name, None)
    return result
'''


def release_build_environment():
    """Release's function, compiled over today's helpers."""
    namespace = {"Iterable": Iterable, "Mapping": Mapping, "os": os,
                 "_HELPER_PASS": launch_env._HELPER_PASS, "_selected": launch_env._selected,
                 "_present": launch_env._present, "valid_name": launch_env.valid_name}
    exec(compile(SOURCE, __file__, "exec"), namespace)
    return namespace["build_environment"]
