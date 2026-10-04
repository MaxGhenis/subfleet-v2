"""Isolated baseline and individually activated mutation checks for PR 124.

All commands run sequentially in foreground slices. The UI mutant copy has
independently selected faults so one compilation can test each fault alone;
PR124_MUTATION is used only in this generated copy, never production sources.
"""
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

from tools.pr124_verify import EVIDENCE, ROOT, run

GIT = ["git", "--git-dir=" + str(ROOT / ".git-local")]
PYTHON = str(ROOT / ".venv/bin/python")
BASE = "75972d4a9506132ac066c2e7474d5a1b9ac20f10"


def baseline_copy():
    folder = EVIDENCE / "baseline"
    folder.mkdir(parents=True, exist_ok=True)
    data = subprocess.run([*GIT, "archive", BASE], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=BytesIO(data)) as archive:
        archive.extractall(folder, filter="data")
    for name in ("tests/frontend/ReviewCutoverProbe.swift", "tests/frontend/test_pr124_fixes_ui.py",
                 "tests/frontend/test_pr124_recovery_properties.py", "tests/unit/test_pr124_fixes.py",
                 "tools/app_cutover_pytest.py"):
        shutil.copyfile(ROOT / name, folder / name)
    return folder


def mutation_copy():
    folder = EVIDENCE / "mutations"
    names = subprocess.run([*GIT, "ls-files", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, text=True, check=True).stdout.splitlines()
    for name in names:
        if name.endswith(".bundle"):
            continue
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return folder


def tests(folder, name, nodes, mutation=None):
    command = [PYTHON, "-m", "pytest", "-p", "tools.app_cutover_pytest", "-q", *nodes]
    if mutation:
        command = ["/usr/bin/env", "PR124_MUTATION=" + mutation, *command]
    result = run(name, 580, command, cwd=folder)
    log = (EVIDENCE / (name + ".log")).read_text()
    assert result["exit_code"] == 1 and not result["timeout"], result
    assert "FAILED " in log and "ERROR " not in log, log[-4000:]
    return result


def replace(folder, file, before, after):
    path = folder / file
    original = path.read_text()
    assert original.count(before) == 1, (file, before)
    path.write_text(original.replace(before, after))
    return original


def daemon_mutations(folder):
    specs = [
        ("policy", "subfleet/default_policy.json", '"id": "gpt-6.1-sol"', '"id": "gpt-6-astra"',
         "test_default_codex_policy_routes_hard_to_sol_and_retires_astra"),
        ("hard-tier", "subfleet/conversations/service.py", 'else hard_models', 'else ["sol"]',
         "test_codex_default_follows_custom_hard_tier_instead_of_a_model_alias"),
        ("retired-default", "subfleet/conversations/service.py", ' and m["id"] != "gpt-6-astra"', '',
         "test_retired_astra_is_never_a_default_even_in_an_old_custom_policy"),
        ("empty-folder", "subfleet/conversations/service.py", 'planned = not workspace', 'planned = False',
         "test_no_folder_check_and_create_agree_without_check_writes"),
        ("missing-provider", "subfleet/conversations/service.py", '    def op_conversation_create(self, args, peer) -> dict:\n        provider = args.get("provider") or "claude"',
         '    def op_conversation_create(self, args, peer) -> dict:\n        provider = args.get("provider")',
         "test_missing_provider_check_and_create_use_claude"),
        ("future-activity", "subfleet/conversations/catalog.py", 'mtime <= latest', 'mtime <= 253402300799',
         "test_future_activity_cannot_pin_an_older_conversation"),
    ]
    for name, file, before, after, node in specs:
        original = replace(folder, file, before, after)
        try:
            tests(folder, "mutation-" + name, ["tests/unit/test_pr124_fixes.py::" + node])
        finally:
            (folder / file).write_text(original)


def ui_mutations(folder):
    flag = lambda name: '(ProcessInfo.processInfo.environment["PR124_MUTATION"] == "' + name + '")'
    file = "app/Sources/UIModel.swift"
    replace(folder, file, 'let url = failedDraftKey.map(retryDraftURL) ?? paths.support.appendingPathComponent("new-conversation-draft.json")',
            'let url = (' + flag("retry-storage") + ' ? nil : failedDraftKey.map(retryDraftURL)) ?? paths.support.appendingPathComponent("new-conversation-draft.json")')
    replace(folder, file, 'guard failedDraftKey != nil else { return }',
            'guard failedDraftKey != nil else { return }\n        if ' + flag("retry-restore") + ' { failedDraftKey = nil; return }')
    replace(folder, "app/Sources/NewConversationDraft.swift", 'let active = models.filter',
            'let active = ' + flag("retired-memory") + ' ? models : models.filter')
    replace(folder, "app/Sources/StatusModel.swift", 'return claude == 0 && (counts["codex"] ?? 0) > 0 ? "codex" : "claude"',
            'return ' + flag("auto") + ' ? (claude > 0 ? "claude" : "codex") : (claude == 0 && (counts["codex"] ?? 0) > 0 ? "codex" : "claude")')
    replace(folder, file, 'guard state.availability.capabilities?.has("workspace.check.v1") == true else',
            'guard state.availability.capabilities?.has("workspace.check.v1") == true || ' + flag("capability") + ' else')
    replace(folder, file, 'let checkSupported = state.availability.capabilities?.has("workspace.check.v1") == true',
            'let checkSupported = state.availability.capabilities?.has("workspace.check.v1") == true || ' + flag("capability"))
    replace(folder, file, 'permission: permission, transient: true)',
            'permission: permission, transient: !' + flag("transient") + ')')
    replace(folder, file, 'if refusedDrafts.isEmpty && problem == refusedDraftNotice',
            'if !' + flag("footer") + ' && refusedDrafts.isEmpty && problem == refusedDraftNotice')
    replace(folder, file, '            readQueue.async {',
            '            let queue = ' + flag("queue") + ' ? outboxQueue : readQueue\n            queue.async {')
    replace(folder, file, 'guard check.ok else', 'guard check.ok || ' + flag("start-guard") + ' else')
    replace(folder, file, 'if let id = newDraft.messageID, engine?.outbox.entry(id) != nil',
            'if !' + flag("crash-clear") + ', let id = newDraft.messageID, engine?.outbox.entry(id) != nil')
    replace(folder, "app/Sources/ConversationStore.swift", 'return entry.conversationID ?? Outbox.draftKey(requestID)',
            'return (' + flag("resolved-create") + ' ? nil : entry.conversationID) ?? Outbox.draftKey(requestID)')
    specs = [
        ("retry-storage", "test_retry_preserves_composer_and_original_message_identity"),
        ("retry-restore", "test_retry_preserves_composer_and_original_message_identity"),
        ("auto", "test_auto_requires_a_ready_codex_lane[0-0-claude]"),
        ("retired-memory", "test_remembered_astra_cannot_override_the_daemon_hard_default"),
        ("capability", "test_older_daemon_skips_optional_check_at_open_and_start"),
        ("transient", "test_unanswered_check_keeps_folder_and_rechecks_on_reconcile"),
        ("footer", "test_last_successful_retry_clears_only_its_notice"),
        ("queue", "test_a_send_does_not_wait_for_folder_git_checks"),
        ("start-guard", "test_start_rechecks_a_folder_and_journals_nothing_if_it_changed"),
        ("crash-clear", "test_relaunch_after_journaling_never_offers_the_same_words_again"),
        ("resolved-create", "test_create_already_acknowledged_after_a_crash_receives_its_original_message"),
    ]
    for name, node in specs:
        tests(folder, "mutation-" + name, ["tests/frontend/test_pr124_fixes_ui.py::" + node], name)
    tests(folder, "mutation-retry-property", ["tests/frontend/test_pr124_recovery_properties.py"], "retry-storage")


if __name__ == "__main__":
    if sys.argv[1] == "baseline":
        folder = baseline_copy()
        tests(folder, "baseline-final-ui", ["tests/frontend/test_pr124_fixes_ui.py"])
        tests(folder, "baseline-property", ["tests/frontend/test_pr124_recovery_properties.py"])
    elif sys.argv[1] == "daemon-mutations":
        daemon_mutations(mutation_copy())
    elif sys.argv[1] == "ui-mutations":
        ui_mutations(mutation_copy())
    else:
        raise SystemExit("expected baseline, daemon-mutations or ui-mutations")
