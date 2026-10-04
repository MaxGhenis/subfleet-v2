"""Bounded foreground verification for PR #128; mutations live in isolated copies.

Run one named case per call. Results and exact-child records are supplied by
pr124_verify; no app is installed and no live state is used.
"""
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

from tools.pr124_verify import ROOT, run

REPORT = ROOT / "docs/reports/2026-10-04-visual-review-fixes"
PYTHON = str(ROOT / ".venv/bin/python")
TEST = "tests/frontend/test_visual_review_fixes.py"
SNAPSHOT = "tests/frontend/test_visual_snapshots.py"


def prepare_compiler():
    """Create foreground compiler wrappers in this workspace on a fresh replay."""
    directory = ROOT / "build/pr124-evidence/bin"
    directory.mkdir(parents=True, exist_ok=True)
    frontend = directory / "swift-frontend"
    scripts = {
        frontend: "#!/bin/sh\nexec " + shlex.quote(PYTHON) + " "
            + shlex.quote(str(ROOT / "tools/foreground_swift_frontend.py")) + ' "$@"\n',
        directory / "xcrun": '#!/bin/sh\nif [ "$1" = swiftc ]; then\n  shift\n'
            + "  exec /usr/bin/xcrun swiftc -disable-sandbox -j 4 -driver-use-frontend-path "
            + shlex.quote(str(frontend)) + ' "$@"\nfi\nexec /usr/bin/xcrun "$@"\n',
    }
    for path, script in scripts.items():
        if not path.exists():
            path.write_text(script)
            path.chmod(0o755)


def replace(folder, file, before, after):
    path = folder / file
    content = path.read_text()
    assert before in content, (file, before)
    path.write_text(content.replace(before, after))


def test(name, folder, nodes):
    prepare_compiler()
    result = run(name, 580, [PYTHON, "-m", "pytest", "-p", "tools.app_cutover_pytest", "-q", *nodes,
                           f"--junitxml={REPORT / (name + '.xml')}"], cwd=folder)
    # These paths were created by this foreground slice. Retain the binary
    # content cache and reports, releasing completed temporary raster/object
    # files so verification also works on hosts with little free disk space.
    command = result["command"]
    temporary = Path(command[command.index("--basetemp") + 1])
    assert temporary.name.startswith("pr124-")
    shutil.rmtree(temporary)
    objects = ROOT / "build/app-cutover-evidence/objects"
    if objects.exists():
        shutil.rmtree(objects)
    return result


def baseline(name):
    folder = ROOT / "build/pr128-baseline"
    assert folder.is_dir(), "Extract git archive b6c5e9af into build/pr128-baseline first"
    for file in ("ReviewFixProbe.swift", "ReviewFixViewProbe.swift", "test_visual_review_fixes.py", "test_visual_snapshots.py"):
        shutil.copyfile(ROOT / "tests/frontend" / file, folder / "tests/frontend" / file)
    shutil.copyfile(ROOT / "tests/fixtures/visual/approvals.json", folder / "tests/fixtures/visual/approvals.json")
    nodes = {"models": [TEST, "-k", "not visible_on_card and not serving_facts and not sidebar and not filter and not disabled and not amber and not real_settled"],
             "views": [TEST, "-k", "visible_on_card or serving_facts or sidebar or filter or disabled or amber or real_settled"],
             "snapshots": [SNAPSHOT, "-k", "not rerender"]}[name]
    return test("baseline-" + name, folder, nodes)


def frontend():
    collected = subprocess.run([PYTHON, "-m", "pytest", "tests/frontend", "--collect-only", "-q"],
        cwd=ROOT, check=True, capture_output=True, text=True)
    nodes = [line for line in collected.stdout.splitlines() if line.startswith("tests/frontend/") and "::" in line]
    groups = {}
    view_names = ("visible_on_card", "serving_facts", "real_settled", "sidebar", "provider_filter", "disabled_quiet", "permission_amber")
    for node in nodes:
        group = node.split("::")[0]
        if "test_core_steer_properties.py::" in node:
            group = node
        elif "test_app_cutover_start.py" in node and "retry_preserves_ids" in node:
            group += "-core"
        elif "test_visual_review_fixes.py" in node:
            group += "-views" if any(name in node for name in view_names) else "-models"
        groups.setdefault(group, []).append(node)
    results = []
    for index, selected in enumerate(groups.values(), 1):
        results.append(test(f"frontend-{index:02d}", ROOT, selected))
    import json
    (REPORT / "frontend-suite.json").write_text(json.dumps({"collected": len(nodes), "slices": results}, indent=2) + "\n")
    assert all(r["exit_code"] == 0 and not r["timeout"] for r in results), "Inspect failed foreground slices"


def mutate(name):
    cache = ROOT / "build/pr124-evidence/probes"
    existing_binaries = set(cache.iterdir())
    folder = ROOT / "build/pr128-mutants" / name
    assert not folder.exists(), f"Use a fresh mutation name: {folder}"
    folder.mkdir(parents=True)
    for directory in ("app", "tests", "tools", "subfleet"):
        shutil.copytree(ROOT / directory, folder / directory, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(ROOT / "pyproject.toml", folder / "pyproject.toml")
    nodes = [TEST]
    if name == "command":
        replace(folder, "app/Sources/ApprovalPresentation.swift", 'request["params"]?["command"]?.string ?? ', "")
        nodes += ["-k", "loaded_approval"]
    elif name == "grants":
        replace(folder, "app/Sources/UIApprovals.swift", "ApprovalGrantView(card: card, request: detail?.request)", "EmptyView()")
        replace(folder, "app/Sources/UIApprovals.swift", "ApprovalGrantView(card: card, request: detail.request)", "EmptyView()")
        nodes += ["-k", "visible_on_card"]
    elif name == "outcomes":
        replace(folder, "app/Sources/WorkPresentation.swift", "(messageState.map(MessageState.terminal.contains) ?? false) || ", "")
        nodes += ["-k", "settled_turn_keeps"]
    elif name == "work-outcome":
        replace(folder, "app/Sources/WorkPresentation.swift", 'outcome: turn.state', 'outcome: nil')
        nodes += ["-k", "failed_work_group"]
    elif name == "tool-labels":
        original = (ROOT / "build/pr128-baseline/app/Sources/WorkPresentation.swift").read_text()
        current = (folder / "app/Sources/WorkPresentation.swift").read_text()
        # Reintroduce only the old label and grouping code, retaining status fixes.
        start = original.index("extension ToolActivity")
        end = original.index("enum ProgressRow")
        a = current.index("extension ToolActivity")
        b = current.index("enum ProgressRow")
        (folder / "app/Sources/WorkPresentation.swift").write_text(current[:a] + original[start:end] + current[b:].replace(', outcome: turn.state', ''))
        nodes += ["-k", "tool_labels or grouped_as_commands"]
    elif name == "serving":
        replace(folder, "app/Sources/UIWindow.swift", "ServedChipView(chip: chip)", "EmptyView()")
        nodes += ["-k", "serving_facts"]
    elif name == "sidebar":
        original = (ROOT / "build/pr128-baseline/app/Sources/UIWindow.swift").read_text()
        current = (folder / "app/Sources/UIWindow.swift").read_text()
        a, b = current.index("struct SidebarView"), current.index("struct SidebarRow")
        old_a, old_b = original.index("struct SidebarView"), original.index("struct SidebarRow")
        (folder / "app/Sources/UIWindow.swift").write_text(current[:a] + original[old_a:old_b] + current[b:])
        nodes += ["-k", "sidebar or filter"]
    elif name == "badge-control":
        # Keep native selection and remove only the hand's independent action.
        # A static marker inside a selectable row must not pass the control test.
        replace(folder, "app/Sources/UIWindow.swift", "Button(action: showApprovals) {", "Group {")
        nodes += ["-k", "sidebar"]
    elif name == "disabled":
        replace(folder, "app/Sources/Theme.swift", ".opacity(enabled ? 1 : 0.4)", ".opacity(1)")
        nodes += ["-k", "disabled_quiet"]
    elif name == "amber":
        replace(folder, "app/Sources/Theme.swift", "static let attention = text.attention.color", "static let attention = Color(nsColor: .systemOrange)")
        nodes += ["-k", "permission_amber"]
    elif name == "snapshot-scale":
        source = (folder / "tests/frontend/SnapshotProbe.swift").read_text()
        start = source.index("    let rep = NSBitmapImageRep(bitmapDataPlanes:")
        end = source.index("    host.cacheDisplay", start)
        (folder / "tests/frontend/SnapshotProbe.swift").write_text(source[:start] + "    let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds)!\n" + source[end:])
        nodes = [SNAPSHOT, "-k", "pixel_size"]
    elif name == "snapshot-capability":
        replace(folder, "tests/frontend/SnapshotProbe.swift", ', "workspace.check.v1"', "")
        nodes = [SNAPSHOT, "-k", "refused_folder"]
    else:
        raise SystemExit("Unknown mutation " + name)
    result = test("mutation-" + name, folder, nodes)
    # Mutant binaries are never reused by another case; keep the mutated
    # sources and failure evidence instead of accumulating executables.
    for binary in set(cache.iterdir()) - existing_binaries:
        binary.unlink()
    return result


if __name__ == "__main__":
    mode, name = sys.argv[1:3]
    if mode == "mutation":
        result = mutate(name)
        # A compilation error is never a valid mutation kill.
        log = ROOT / "build/pr124-evidence" / ("mutation-" + name + ".log")
        assert result["exit_code"] == 1 and not result["timeout"], result
        assert not re.search(r"\.swift:\d+:\d+: error:", log.read_text()) and "ERROR at setup" not in log.read_text(), log
    elif mode == "fixed":
        nodes = {"models": [TEST, "-k", "not visible_on_card and not serving_facts and not sidebar and not filter and not disabled and not amber and not real_settled"],
                 "views": [TEST, "-k", "visible_on_card or serving_facts or sidebar or filter or disabled or amber or real_settled"],
                 "snapshots": [SNAPSHOT], "progress": ["tests/frontend/test_visual_progress.py"],
                 "protocol": ["tests/unit/test_app_cutover_daemon.py", "tests/unit/test_pr124_fixes.py", "tests/unit/test_approval_commit.py"]}[name]
        result = test("fixed-" + name, ROOT, nodes)
        assert result["exit_code"] == 0 and not result["timeout"], result
    elif mode == "baseline":
        result = baseline(name)
        assert result["exit_code"] == 1 and not result["timeout"], result
        log = ROOT / "build/pr124-evidence" / ("baseline-" + name + ".log")
        assert not re.search(r"\.swift:\d+:\d+: error:", log.read_text()) and "ERROR at setup" not in log.read_text(), log
    elif mode == "suite" and name == "frontend":
        frontend()
    else:
        raise SystemExit("Expected mutation, fixed, baseline or suite frontend")
