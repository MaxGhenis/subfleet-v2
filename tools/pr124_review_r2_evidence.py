"""Foreground regression and isolated mutation proof for PR 124 review round two."""
from io import BytesIO
import shutil
import subprocess
import sys
import tarfile

from tools.pr124_verify import EVIDENCE, ROOT, run

HEAD = "b11f766b7709353de8d8d12db0ba965f710738ea"
BASE = "f832e6b8"
GIT = ["git", "--git-dir=" + str(ROOT / ".git-local")]
PYTHON = str(ROOT / ".venv/bin/python")
UI = "tests/frontend/test_pr124_review_r2_fixes_ui.py"
UNIT = "tests/unit/test_pr124_review_r2_fixes.py"
OVERLAY = [UI, UNIT, "tests/frontend/ReviewCutoverProbe.swift", "tests/frontend/test_pr124_fixes_ui.py",
           "tools/app_cutover_pytest.py"]


def archived(revision, label, overlay=True):
    folder = EVIDENCE / label
    folder.mkdir(parents=True, exist_ok=True)
    data = subprocess.run([*GIT, "archive", revision], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=BytesIO(data)) as archive:
        archive.extractall(folder, filter="data")
    if overlay:
        for name in OVERLAY:
            shutil.copyfile(ROOT / name, folder / name)
    return folder


def current_copy():
    folder = EVIDENCE / "r2-mutations"
    names = subprocess.run([*GIT, "ls-files", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, text=True, check=True).stdout.splitlines()
    for name in names:
        if name.endswith(".bundle"):
            continue
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return folder


def pytest(folder, name, nodes, mutation=None, expect_failure=False):
    command = [PYTHON, "-m", "pytest", "-p", "tools.app_cutover_pytest", "-q", *nodes]
    if mutation:
        command = ["/usr/bin/env", "PR124_R2_MUTATION=" + mutation, *command]
    result = run("r2-" + name, 580, command, cwd=folder)
    log = (EVIDENCE / ("r2-" + name + ".log")).read_text()
    assert not result["timeout"], result
    if expect_failure:
        assert result["exit_code"] == 1 and "FAILED " in log and "ERROR " not in log, log[-8000:]
    else:
        assert result["exit_code"] == 0, log[-8000:]
    return result


def replace(folder, name, before, after):
    path = folder / name
    original = path.read_text()
    assert original.count(before) == 1, (name, before)
    path.write_text(original.replace(before, after))
    return original


def ui_mutations(folder):
    flag = lambda name: '(ProcessInfo.processInfo.environment["PR124_R2_MUTATION"] == "' + name + '")'
    model = "app/Sources/UIModel.swift"
    replace(folder, model, 'guard let capabilities = state.availability.capabilities else {',
            'if state.availability.capabilities == nil, ' + flag("preview-unready") + ' {\n'
            '            if newDraft.resolvedWorkspace == nil { newDraft.scratchWorkspace = scratchWorkspace(support: paths.support).path }\n'
            '            newDraft.applyWorkspaceCheck(WorkspaceCheckResult(ok: true), workspace: newDraft.resolvedWorkspace!, provider: provider, permission: permission)\n'
            '            return\n        }\n        guard let capabilities = state.availability.capabilities else {')
    replace(folder, model, 'guard state.availability.isReady else { validateNewDraftWorkspace(); return }',
            'guard state.availability.isReady || ' + flag("start-unready") + ' else { validateNewDraftWorkspace(); return }')
    replace(folder, model, 'let checkSupported = state.availability.capabilities.map { $0.has("workspace.check.v1") } ?? true',
            'let checkSupported = ' + flag("start-unready") + ' ? (state.availability.capabilities?.has("workspace.check.v1") == true) : (state.availability.capabilities.map { $0.has("workspace.check.v1") } ?? true)')
    replace(folder, "app/Sources/NewConversationDraft.swift", '(models.isEmpty ? settings.model : "")',
            '(' + flag("saved-model") + ' ? "" : (models.isEmpty ? settings.model : ""))')
    replace(folder, model, 'composerBeforeRetry?.scratchWorkspace = nil',
            'if ' + flag("scratch-owner") + ' { newDraft.scratchWorkspace = nil; validateNewDraftWorkspace() } else { composerBeforeRetry?.scratchWorkspace = nil }')
    for mutation, node in [
        ("preview-unready", "test_unready_daemon_disables_start_with_reason[False-down]"),
        ("start-unready", "test_start_rejects_stale_acceptance_after_losing_readiness[down]"),
        ("saved-model", "test_saved_model_survives_opening_before_catalog_load"),
        ("scratch-owner", "test_scratch_start_resets_its_composer_when_retry_opens_during_check"),
    ]:
        pytest(folder, "mutation-" + mutation, [UI + "::" + node], mutation, expect_failure=True)


def unit_mutations(folder):
    specs = [
        ("astra-literal", "subfleet/conversations/service.py", 'fallback = offered',
         'fallback = [m for m in offered if name != "codex" or m["id"] != "gpt-6-astra"]',
         "test_active_astra_only_catalog_publishes_fallback"),
        ("ignore-retired", "subfleet/conversations/service.py", ' and not m["retired"]', '',
         "test_policy_retirement_excludes_astra_from_fallback"),
        ("design-stale", "docs/desktop/design.md", '(shipped policy: `gpt-6-astra`)',
         '(shipped policy: `gpt-6.1-sol`). Retired `gpt-6-astra` is never a default',
         "test_design_describes_the_shipped_policy_without_claiming_astra_retired"),
        ("report-stale", "docs/reports/2026-10-03-app-cutover.md", 'The shipped routing policy is unchanged from `f832e6b8`.',
         'The shipped routing policy now defines `sol = gpt-6.1-sol`.',
         "test_pr_report_does_not_claim_the_reverted_policy_is_still_shipped"),
    ]
    for mutation, file, before, after, node in specs:
        original = replace(folder, file, before, after)
        try:
            pytest(folder, "mutation-" + mutation, [UNIT + "::" + node], expect_failure=True)
        finally:
            (folder / file).write_text(original)


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "baseline":
        folder = archived(HEAD, "r2-baseline")
        pytest(folder, "baseline-unit", [UNIT], expect_failure=True)
        pytest(folder, "baseline-ui", [UI], expect_failure=True)
    elif mode == "mutations":
        folder = current_copy()
        unit_mutations(folder)
        ui_mutations(folder)
    elif mode == "focused":
        pytest(ROOT, "focused-unit", [UNIT, "tests/unit/test_pr124_fixes.py"])
        pytest(ROOT, "focused-ui", [UI, "tests/frontend/test_pr124_fixes_ui.py"])
    elif mode == "policy":
        pytest(ROOT, "suite-policy", ["tests/unit/test_policy.py", "tests/unit/test_policy_support.py", UNIT])
    elif mode == "property":
        result = run("r2-property-seed9001", 580,
                     ["/usr/bin/env", "PR124_PROPERTY_SEED=9001", PYTHON, "-m", "pytest",
                      "-p", "tools.app_cutover_pytest", "-q", "-s", "--hypothesis-show-statistics",
                      "tests/frontend/test_pr124_recovery_properties.py"])
        assert result["exit_code"] == 0 and not result["timeout"], result
    elif mode == "base-failures":
        folder = archived(BASE, "r2-pr-base", overlay=False)
        for index, node in enumerate(sys.argv[2:]):
            pytest(folder, "base-failure-" + str(index), [node], expect_failure=True)
    else:
        raise SystemExit("expected baseline, focused, mutations, policy, property, or base-failures")
