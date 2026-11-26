# Exact helper definitions from a093fb4b6, before PR 284.

def releases_after(result, at):
    """Each trace entry from `at` on that turns suspension off."""
    return [e for e in result["trace"] if e["at"] >= at and "suspended" in e and not e["suspended"]
            and e["event"] != "move"]

def assert_replayed_shrink_order(result, steps):
    """The CI order, each step as presented: the order the rule is checked against. The reveal is
    still outstanding at the shrink (the view's own trace; the readiness `reveal_pending` is the
    search palette's, false throughout this mode)."""
    diagnostic = json.dumps(result, sort_keys=True)
    assert steps["early_completion"]["position"]["from_end"] <= END, diagnostic
    assert steps["scrolled"]["position"]["from_end"] > DISTANT, diagnostic
    assert steps["shrinking"]["reveal"]["outstanding"], diagnostic
    assert steps["shrunk"]["reveal"]["outstanding"], diagnostic
    assert_measured_shrinks(result, steps)
    assert steps["shrunk"]["position"]["from_end"] <= END, diagnostic
    assert any(e["event"] == "marker_appeared" and steps["shrinking"]["at"] <= e["at"] <= steps["shrunk"]["at"]
               for e in result["trace"]), diagnostic
    assert steps["late_completion"]["position"]["from_end"] <= END, diagnostic
    assert steps["regrown"]["position"]["from_end"] > DISTANT, diagnostic
    assert settling_tick(result, steps) is not None, diagnostic

def assert_shrink_releases_nothing(result, steps):
    """Neither the marker nor the completion at the clamped end released suspension, and the
    settling tick never brought the view to the end."""
    diagnostic = json.dumps(result, sort_keys=True)
    assert not releases_after(result, steps["reveal"]["at"]), diagnostic
    assert not any(e["event"] == "reveal_end_visible" for e in result["trace"]), diagnostic
    tick = settling_tick(result, steps)
    assert tick["suspended"] and tick["move"] == "stay", diagnostic
    for g in result["geometry"]:
        if g["at"] >= steps["regrown"]["at"]:
            assert g["position"]["from_end"] > DISTANT, json.dumps(g) + ": " + diagnostic
    for key in ("after_tick", "caught_up"):
        assert steps[key]["position"]["from_end"] > DISTANT, diagnostic
