"""Public handler refusals, apart from request decoding and argument errors (C-25.7).

A handler raises `ProtocolError` or `AdapterError` with a `reason` from this
closed vocabulary and, where its template names a value, `details` holding that
value. The text a client sees is built here from the template and values that
pass their checks. A value that fails its check selects the next, less specific
template for the reason; the last one names no value. The exception's own
message, its args and its repair text are never inputs: they stay with the
exception for its direct callers and reach daemon.log only, scrubbed whole
(`log_detail`). A job's records keep the same public text (`refusal_detail`).
"""
from __future__ import annotations

import errno
import os
import re
import string
from typing import Any

from . import ids
from .contracts import HOLD_NOTE_CREDENTIAL_RULE, HOLD_NOTE_RULE, JOB_ID_SLUG_MAX, Sandbox
from .sessions.registry import CONVERSATION_FIX

# Checkout checks select these fixed diagnostics and repairs at their raise
# sites. Neither exception text nor a path is used to select a public message.
CHECKOUT_REFUSALS = {
    "checkout-missing": ("push requires a committed Git checkout with an origin (checkout not there)",
                         "restore the checkout or resubmit from a committed full clone with an origin"),
    "origin-missing": ("push requires a checkout with an origin URL in its local config",
                       "restore origin in the checkout, or omit --push-branch"),
    "baseline-missing": ("push requires a checkout HEAD naming a committed baseline",
                         "commit a baseline on a feature branch and resubmit"),
    "shallow": ("push requires a full clone: the checkout is shallow",
                "run git fetch --unshallow there, or push it yourself"),
    "alternates": ("the checkout borrows objects through objects/info/alternates",
                   "run git repack -a -d and remove objects/info/alternates, or pass --in-place"),
    "partial": ("the checkout is a partial clone with missing objects",
                "clone it without --filter, or pass --in-place"),
    "metadata-missing": ("checkout metadata is missing", "restore the checkout metadata and resubmit"),
    "metadata-symlink": ("checkout metadata is a symlink or symlink loop",
                         "replace the symlink with regular checkout metadata and resubmit"),
    "metadata-special": ("checkout metadata is not a regular file",
                         "restore regular checkout metadata and resubmit"),
    "metadata-large": ("checkout metadata exceeds its read budget",
                       "use a checkout with bounded metadata, or omit --push-branch"),
    "metadata-unreadable": ("checkout metadata cannot be read", "check repository permissions and resubmit"),
    "gitdir-invalid": ("checkout gitdir is invalid or is a symlink or special file",
                       "restore a regular gitdir file or directory and resubmit"),
    "config-utf8": ("checkout metadata is not UTF-8 (UnicodeDecodeError)",
                    "repair the checkout metadata encoding and resubmit"),
    "config-header": ("checkout config has a line before its section (MissingSectionHeaderError)",
                      "repair the checkout config section headers and resubmit"),
    "config-invalid": ("checkout config cannot be parsed", "repair the checkout config and resubmit"),
    "protected-branch": ("writable in-place jobs require a feature branch",
                         "choose a feature branch or omit --in-place"),
    "object-special": ("baseline object is not a regular file", "resubmit from a full clone with regular objects"),
    "sparse": ("baseline object is sparse; sparse objects are refused",
               "resubmit from a full clone without sparse objects, or pass --in-place"),
    "object-budget": ("baseline object copy exceeds the object budget",
                      "use a smaller full clone, pass --in-place, or omit --push-branch"),
    "object-changed": ("baseline object changed while copying", "wait for checkout writes to finish and resubmit"),
}

# reason -> (message, fix), the most specific form. `{name}` is a checked value.
HANDLER_MESSAGES: dict[str, tuple[str, str | None]] = {
    # Jobs, requests and lists.
    "unknown-job": ("unknown job {job_id}", None),
    "different-payload": ("request id already used with a different payload by job {job_id}", None),
    "turn-submit": ("conversation turns are created by the daemon, not submitted",
                    "send a message with message.submit"),
    "turn-block": ("a turn job needs its conversation's turn block", None),
    "unknown-pinned-model": ("unknown pinned model {model}", "pin a model from the live policy"),
    "needs-shell": ("needs a shell: {model} runs {sandbox} jobs without one",
                    "pass -s workspace-write, or pin a Codex model"),
    "list-kind": ("kind must name a job kind", None),
    "list-include-turns": ("include_turns must be true or false", None),
    "list-last": ("last must be a nonnegative integer", None),
    "wait-deadline": ("deadline_s must be a number", None),
    "batch-object": ("batch must be an object with id, label, index and size", None),
    "batch-id": ("batch.id must contain 1 to 128 characters", None),
    "batch-label": ("batch.label must contain 1 to 80 characters", None),
    "batch-index": ("batch.index and batch.size must satisfy 1 <= index <= size <= 256", None),
    "reserve-reason": ("unmeasured_reserve_reason must contain 1 to 2000 characters of reason/evidence", None),
    "reserve-dispatch": ("unmeasured reserve authorization requires a fresh dispatch with explicit "
                         "pinned_lane and pinned_model", None),
    # Where a job writes (C-6.5, C-11.1, C-13.2).
    "workdir-tmp": ("workdir is under /tmp", "pass --allow-tmp or use a durable workdir"),
    "policy-workdir": ("{task} jobs write by policy, and {repository}",
                       "commit a baseline or run it from a repository, or pass -s read-only "
                       "to run it here without writing"),
    "writable-repository": ("writable jobs require a committed git repository",
                            "initialize a feature branch and commit a baseline, or pass -s read-only"),
    "review-publish": ("reviews cannot publish", "omit --push-branch"),
    "writable-branch": ("writable job refused on {branch}",
                        "Check out a task branch before submitting a writable job."),
    "inspect-workdir": ("could not inspect the workdir",
                        "submit again; raise caps.workspace_git_timeout_s if this repository is slow"),
    "inspect-writer": ("could not inspect the worktree of {job_id}", "submit again"),
    "output-path": ("cannot resolve output path", "remove the symlink loop or choose a different -o path"),
    "out-path-held": ("output path is held by another job", "use a different -o path or resolve quarantine"),
    "out-path-live": ("out_path is held by a live job", "use a different -o path or wait for its owner"),
    "path-lease": ("workdir is held by a live job", "wait for the current writer or choose another worktree"),
    "worktree-lease": ("worktree has a lease", "resolve its owner before reusing the workspace"),
    "worktree-writer": ("worktree is held by a live writable job ({job_id})",
                        "wait for the current writer or choose another worktree"),
    "conversation-writer": ("worktree is being written by a conversation turn ({job_id})",
                            "wait for the conversation's turn to end, or run the job in its own "
                            "worktree (without --in-place)"),
    "workspace-prepare": ("could not allocate the worktree; daemon.log has git's output",
                          "check repository and state-root permissions"),
    # D-SH1 round 6 (C-6.6): a policy review's copy of its caller's uncommitted work.
    "uncommitted-copy": ("the caller's uncommitted files changed while they were copied", "submit again"),
    "uncommitted-apply": ("the caller's uncommitted changes could not be copied into the worktree",
                          "submit again, or pass -s read-only to review the checkout in place"),
    # One session's writers (C-6.4, C-6.5, C-23.54).
    "revive-writer": ("session has a live writable job ({job_id}) and a revive is a second instance of it",
                      "wait for {job_id}, or kill it before reviving or dispatching"),
    "unidentified-writer": ("session has a live writable job ({job_id}) and the submitting instance "
                            "cannot be identified",
                            "submit from the session's own shell (CLAUDE_PID), or wait for {job_id}"),
    "other-writer": ("session already has a live writable job ({job_id}) from another instance",
                     "dispatch from that instance, or wait for or kill {job_id}"),
    "writer-cap": ("session already holds {count} live writable jobs (caps.max_writable_per_session {limit})",
                   "wait for one to finish, or raise the cap in policy.json"),
    "writer-changed": ("session has a live writable job ({job_id}) this submission was not checked against",
                       "submit again"),
    "revive-held": ("session already has a live revive ({job_id})",
                    "subfleet runs show {job_id}, or kill it before reviving again"),
    "conversation-session": ("{session_verb} refused: session belongs to a conversation and continues only in its conversation",
                             CONVERSATION_FIX),
    # Parents, gates and reserves.
    "parent-cancelled": ("parent is cancelled", "submit an independent job"),
    "parent-child-cap": ("parent child budget exhausted (caps.max_child_jobs)",
                         "use a new parent job, or raise or remove the cap in policy.json"),
    "gate-round-held": ("gate already has a reserved peer round",
                        "wait for or explicitly abandon the existing gate round"),
    "gate-review-cwd": ("gate review cwd must be a neutral directory under the state root",
                        "allocate the peer cwd under SUBFLEET_HOME outside any repository"),
    # Resume and revive (C-12.9, C-26.13).
    "resume-parent": ("resume requires its source parent_job_id", None),
    "resume-conversation": ("resume refused: {job_id} is a conversation turn, and a conversation's "
                            "session continues only in its conversation", CONVERSATION_FIX),
    "resume-source-active": ("source job is still active or quarantined",
                             "wait for its completion or resolve containment before resuming"),
    "resume-no-attempt": ("source job has no provider attempt", "submit a fresh job"),
    "resume-no-session": ("source attempt has no recorded native session", "submit a fresh job"),
    "resume-retention": ("resume refused: {job_id} is being archived by retention", "retry in a minute"),
    "resume-mcp-mismatch": ("a resume starts its source's MCP servers (C-12.9)",
                            "resume without --mcp, or submit a fresh job naming the servers it needs"),
    "resume-mcp-unreadable": ("the source job's MCP servers cannot be read",
                              "submit a fresh job naming them with --mcp"),
    "resume-identity-missing": ("resume source identity is missing", "resubmit the resume from the original job"),
    "resume-identity-mismatch": ("resume source identity is missing or does not match this attempt",
                                 "resubmit the resume from the original job"),
    "resume-provider": ("{provider} cannot resume a session in place",
                        "use `subfleet sessions handoff` to continue this session"),
    "isolation-resume": ("isolated review cannot resume a contextual session", "submit a fresh isolated review job"),
    "isolation-resume-claude": ("isolated review cannot resume a contextual Claude session",
                                "submit a fresh isolated review job"),
    "isolation-resume-codex": ("isolated review cannot resume a contextual Codex thread",
                               "submit a fresh isolated review job"),
    # MCP servers (C-12.9).
    "mcp-read-only": ("a read-only job starts no MCP servers (C-12.9)",
                      "pass -s workspace-write for a job that needs them, or drop --mcp"),
    "mcp-provider": ("only a Claude launch starts MCP servers, and this job runs on {provider} (C-12.9)",
                     "pin a Claude model (-m opus, -m sonnet) or a --tier whose chain has one, or drop --mcp"),
    "mcp-chain": ("only a Claude launch starts MCP servers, and this job's chain from its tier has no "
                  "Claude model (C-12.9)",
                  "pin a Claude model (-m opus, -m sonnet) or a --tier whose chain has one, or drop --mcp"),
    "mcp-workdir-changed": ("the workdir no longer offers the requested MCP servers", "submit with a new request id"),
    "claude-mcp-config": ("the job's MCP config cannot give the servers it names",
                          "submit the job again with --mcp naming servers its workdir offers"),
    # Isolated review (C-6.5, C-23.3).
    "isolation-sandbox": ("isolated review requires -s read-only and -D REVIEW_ROOT",
                          "pass -I -s read-only -D REVIEW_ROOT"),
    "isolation-env": ("isolated review inherits {variable}",
                      "use an unmanaged review environment without {variable}"),
    "isolation-requirements": ("isolated Codex review has managed requirements",
                               "use a Codex lane without managed requirements"),
    "isolation-layer": ("isolated Codex review has an unsupported {layer} configuration layer",
                        "use a lane with no managed or nonempty {layer} configuration layer"),
    "isolation-inspect": ("cannot safely inspect isolated Codex configuration or MCP inventory",
                          "use a lane whose config layers and MCP inventory can be inspected"),
    # Host push (C-8.5).
    "push-dispatch-only": ("host push is supported for dispatch jobs", "submit a dispatch with --push-branch"),
    "push-read-only": ("host push needs a writable job: it writes its bundle in its workspace",
                       "pass -s workspace-write, or omit --push-branch"),
    "push-in-place-main": ("writable in-place jobs require a feature branch",
                           "choose a feature branch or omit --in-place"),
    "push-refused": ("host push refused; the daemon log names the check",
                     "omit --push-branch, or choose an unprotected branch in an allowed origin after "
                     "the hub enables push policy"),
    "push-location": ("workspace refused: {checkout_problem} ({errno_name})", "{checkout_fix}"),
    "push-checkout": ("host push workspace refused: {checkout_problem} ({errno_name})", "{checkout_fix}"),
    "push-workspace-gone": ("host push workspace disappeared after preparation",
                            "resubmit the job to rebuild its checkout"),
    # Providers, guards and credentials.
    "unknown-provider": ("unknown provider", None),
    "adapter-missing": ("{provider} adapter has no constructor", None),
    "guard-missing": ("Codex guard preflight is not installed", "install the Codex adapter and reviewed guard files"),
    "guard-preflight": ("Codex guard preflight refused the writable job ({guard_kind})", "rerun subfleet doctor"),
    # A verdict that rests on no answer says so, and never asks for a restore (C-14.2):
    # the fixes are `guard.preflight`'s own (`_TIMEOUT_FIX`, `_PROBE_FIX`).
    "guard-preflight-timeout": ("Guard preflight timed out: guard trust is unverified, not mismatched",
                                "Check local CPU/I/O pressure and daemon scheduling, then repeat the guard "
                                "preflight; raise CODEX_GUARD_PREFLIGHT_TIMEOUT (seconds, default 60) for the "
                                "daemon if Codex startup is legitimately slow on this machine. A timeout does "
                                "not establish guard-file or version drift; trust remains unverified."),
    "guard-preflight-probe": ("Guard preflight refused: the Codex app-server could not be probed; guard trust "
                              "is unverified, not mismatched",
                              "Read stderr_tail and exit_status in the attempt's guard-preflight.json (or run "
                              "`subfleet doctor --live`) for the app-server's own error; trust remains "
                              "unverified."),
    # C-14.2 (D-SH1 round 8): any review on Codex, read-only included; the fix is
    # `guard.preflight._PUBLISH_FIX`, word for word.
    "guard-preflight-publish": ("Codex guard preflight refused the review: the never-rules guard verified, but "
                                "Codex did not trust the review's publish hook",
                                "Read the hooks/list answer in the attempt's guard-preflight.json: it must list "
                                "<state root>/publish-guard/review-publish-hook once, enabled and trusted. subfleet "
                                "doctor does not check this hook. Until it verifies, run the review on a Claude "
                                "model (for example -m opus)."),
    "codex-guard-required": ("Writable Codex jobs require the never-rules guard",
                             "Run the guard trust preflight and supply its hooks override."),
    "codex-publish-hook-required": ("Writable Codex reviews require the review publish hook",
                                    "Run the guard trust preflight with the publish hook and supply its hooks override."),
    # C-12.4, #221 review r2 P3-7: the installed hook is run once before launch.
    "publish-hook-self-test": ("the review publish hook did not deny a gh write when it was tested before launch",
                               "check that the hook's interpreter and the subfleet package it imports are in place "
                               "(daemon.log names the failure), then submit again"),
    "codex-home-mismatch": ("Codex credential home does not match the lane",
                            "Resolve CODEX_HOME from this lane's original home."),
    "codex-thread-id": ("Codex resume requires a thread id", "Use the original attempt's native_session_id and lane."),
    "turn-manifest": ("turn job has no turn manifest", "the dispatcher creates turn jobs"),
    "turn-guard": ("a Codex turn needs the verified never-rules guard", None),
    "api-key-home": ("API-key home refused", "log this lane into a subscription account"),
    "credential-env-inherited": ("lane environment credential names a variable every launch inherits",
                                 "store the lane OAuth token under a daemon variable no launch inherits"),
    "credential-env": ("could not resolve lane environment credential",
                       "set the lane's credential variable in the daemon environment to the lane OAuth token"),
    "credential-kind": ("unsupported credential kind for provider", None),
    # D-ST1: the test-only keychain guard (credentials.py); both of its raises read the same.
    "keychain-test-guard": ("refusing personal keychain read under pytest",
                            "provide an isolated credential runner or executable"),
    "credential-keychain": ("could not resolve lane keychain credential",
                            "unlock the configured credential store and verify the lane reference with agent-secret"),
    "claude-keychain-read": ("claude: could not read the lane keychain item",
                             "check the enrolled keychain item with agent-secret"),
    "claude-keychain-missing": ("claude: no keychain token for the lane",
                                "run claude setup-token while signed into the lane account, then store it "
                                "in the enrolled keychain item"),
    "claude-keychain-empty": ("claude: the lane keychain item is empty",
                              "re-enrol the lane with a fresh claude setup-token credential"),
    "claude-credential-kind": ("claude: unknown credential kind",
                               "a Claude lane is either kind 'keychain-token' or kind 'home'"),
    # Lanes: enrollment (C-1.3, C-1.4, C-10.6).
    "enroll-name": ("lanes enroll: name a credential (a home directory or a claude-quota-<email> keychain item)",
                    None),
    "enroll-credential": ("lanes enroll: the credential is neither a home directory nor a "
                          "claude-quota-<email> item", None),
    "enroll-owner": ("lanes enroll: owner must be v1 or v2", None),
    "enroll-bound": ("lanes enroll: the credential is already lane {lane_id}", "subfleet lanes list"),
    "enroll-quarantined": ("enrollment containment is quarantined; its lane remains fenced", None),
    "enroll-receipt": ("enrollment ended without an exit receipt", None),
    "reenroll-desktop": ("re-enrollment requires a non-desktop v2-owned lane", None),
    "reenroll-owner": ("re-enrollment cannot change ownership; use lanes transfer", None),
    "reenroll-changed": ("lane changed during re-enrollment", None),
    "reenroll-live-attempt": ("lane still has a live or quarantined attempt", None),
    "reenroll-lease": ("lane still has an execution lease", None),
    "reenroll-leased": ("lane acquired an execution lease during re-enrollment", None),
    "reenroll-identity": ("re-enrollment could not verify the existing account identity", None),
    "reenroll-account": ("re-enrollment found a different account; use a separate credential reference", None),
    "codex-enroll-home": ("Codex enrollment requires a subscription home",
                          "Use a Codex home signed into a paid ChatGPT subscription."),
    "codex-api-key": ("API-key Codex homes are refused",
                      "Sign into a paid ChatGPT subscription in the lane home and enroll again."),
    "codex-token-missing": ("Codex home has no readable subscription token",
                            "Sign into a paid ChatGPT subscription in the lane home and enroll again."),
    "codex-free-plan": ("Free ChatGPT plans are refused",
                        "Sign into a paid ChatGPT subscription in the lane home and enroll again."),
    "codex-account-missing": ("Codex home has no account id or email",
                              "Sign into a paid ChatGPT subscription in the lane home and enroll again."),
    "claude-credential-provider": ("claude: the credential is for another provider",
                                   "enrol a Claude credential with the Claude adapter"),
    "claude-authenticate": ("claude: the credential did not authenticate",
                            "run claude setup-token while signed into the lane account, then store it "
                            "in the enrolled keychain item"),
    "claude-org-disabled": ("claude: the organisation has disabled Claude Code subscription access for this account",
                            "ask the account's admin to enable Claude Code access"),
    "claude-account-mismatch": ("claude: the credential reference and the signed-in account do not match",
                                "rebind the lane to the account its credential really holds (C-1.3)"),
    "claude-account-missing": ("claude: cannot determine the account for the credential",
                               "name the keychain item claude-quota-<email>, or point the lane at a home "
                               "whose .claude.json carries oauthAccount.emailAddress"),
    "claude-profile-account": ("claude: the profile endpoint answered without naming an account",
                               "retry enrolment; if it persists, the credential is not a Claude Code OAuth "
                               "token and cannot be bound to an account (C-10.6)"),
    # Lanes: holds, releases and transfers (C-9.6, C-10.4).
    "unknown-lane": ("lanes {action}: unknown lane {lane_id}", "subfleet lanes list"),
    "hold-until-past": ("lanes hold: --until must be in the future; hold is not in force", None),
    "hold-until": ("lanes hold: --until must be an ISO 8601 instant", None),
    # The rule a note broke (`contracts.hold_note`), never the note (review r1 of #263).
    "hold-note": ("lanes hold: {hold_rule}", None),
    "release-limits-note": ("lanes release --limits: --note is required", None),
    "release-limits-none": ("lanes release --limits: no open provider-limit or credits closures", None),
    "release-limits-reported": ("lanes release --limits: reported clock; provider clocks cannot be released by hand",
                                None),
    # Tasks: an operator's hold on one task at admission (C-11.10, D-TASKHOLD1).
    "unknown-task": ("tasks: unknown task {task}",
                     "name a task the policy's chains route; subfleet run --help lists them"),
    "task-hold-note": ("tasks hold: --note is required: one line of printable characters, at most 160, "
                       "and no credential", None),
    "task-hold-until": ("tasks hold: --until must be a later ISO 8601 instant", None),
    "tasks-action": ("tasks: the action is list, hold or release", None),
    "transfer-lane": ("lanes transfer: name a lane", None),
    "transfer-owner": ("lanes transfer: --to must be v1 or v2", None),
    "transfer-blocked": ("lanes transfer: the lane cannot move yet; the dry run's blocker says why",
                         "subfleet lanes transfer <lane> --to v2 --dry-run shows the plan"),
    "transfer-held": ("lanes transfer: lane {lane_id} is held (credential-refused={lane_variable}): "
                      "it holds no credential to move",
                      "point the lane at a daemon variable no launch inherits in lanes.json (same lane id, "
                      "provider and account), restart the daemon, then transfer it"),
    "transfer-live": ("lanes transfer: the lane has live work",
                      "wait for its work or containment to finish, then run this again"),
    "transfer-v1-edit": ("lanes transfer: this transfer edits a v1 roster file",
                         "re-run with --i-understand-v1-edit (a backup is written beside it)"),
    "transfer-roster": ("lanes transfer: a roster file cannot be read or edited safely; "
                        "the daemon log names it", None),
    "transfer-lanes-json": ("lanes transfer: lanes.json is not valid JSON; refusing to edit it",
                            "repair lanes.json or remove it"),
    "transfer-relocate": ("lanes transfer: could not relocate the lane home; the daemon log names both paths",
                          "move it by hand and run this again; the re-run sees the home already in place "
                          "and finishes the record and the flip"),
    # Operator commands (C-19).
    "errors-hours": ("errors: --hours must be a finite positive number", None),
    "errors-hours-range": ("errors: --hours is outside the supported date range", None),
    "operations-target": ("name a lane or all", None),
    "unknown-provider-lane": ("unknown {provider} lane {lane_id}", "subfleet lanes list"),
    "operations-dry-run": ("dry_run must be a boolean", None),
    "canonical-model": ("canonical-model: name a model", None),
    "desktop-login": ("desktop login is never a Subfleet lane operation", None),
    "login-lane": ("login codex: name one existing lane", None),
    "login-owner": ("transfer this lane to v2 before re-enrollment", None),
    "login-enabled": ("login requires a disabled lane; healthy bindings are not replaced", None),
    "login-busy": ("lane still has live work or an execution lease", None),
    "cards-target": ("cards: the only target is 'refresh'", None),
    "operations-command": ("unknown operator command", None),
    # Notices, pings and sessions (C-15.8, C-23.39).
    "notice-ack-fingerprints": ("notice.ack: give one fingerprint per id, or none", None),
    "notice-ack-fingerprint-type": ("notice.ack: every fingerprint is a string, as the listing gives it", None),
    "notice-withdraw-fingerprints": ("notice.withdraw: give one fingerprint per id, or none", None),
    "notice-withdraw-fingerprint-type": ("notice.withdraw: every fingerprint is a string, as the listing gives it",
                                         None),
    "notice-withdraw-job": ("notice.withdraw: a job's notice is its terminal record and is acknowledged, "
                            "never withdrawn (C-15.8)", "subfleet notices --session <id> --ack"),
    "notice-state": ("unknown notice state", None),
    "ping-text": ("ping: text must be a string (C-15.8)", None),
    "ping-session": ("ping: no session named, and alerts.operator_session is not set; a notice addressed "
                     "to no session is never read (C-15.8)", "name the session: subfleet ping --session <id> TEXT"),
    "sessions-id": ("sessions {action}: session_id is required", None),
    "sessions-action": ("unknown sessions action", None),
}

# reason -> less specific forms, tried in order when a value fails its check.
# The last form of every reason that names a value names none.
LESS_SPECIFIC: dict[str, tuple[tuple[str, str | None], ...]] = {
    "conversation-session": (("session belongs to a conversation and continues only in its conversation", CONVERSATION_FIX),),
    "push-location": (("workspace refused: {checkout_problem}", "{checkout_fix}"),
                      ("workspace refused; the daemon log names the check", "choose a committed feature branch in a full clone")),
    "push-checkout": (("host push workspace refused: {checkout_problem}", "{checkout_fix}"),
                      ("host push workspace refused; the daemon log names the check",
                       "resubmit from a full clone whose origin and HEAD still name the commit to start from, or omit --push-branch")),
    "guard-preflight": (("Codex guard preflight refused the writable job", "rerun subfleet doctor"),),
    "unknown-job": (("unknown job", None),),
    "different-payload": (("request id already used with a different payload", None),),
    "unknown-pinned-model": (("unknown pinned model", HANDLER_MESSAGES["unknown-pinned-model"][1]),),
    "needs-shell": (("needs a shell: the pinned launch runs without one", HANDLER_MESSAGES["needs-shell"][1]),),
    "policy-workdir": (("these jobs write by policy, and {repository}", HANDLER_MESSAGES["policy-workdir"][1]),
                       ("these jobs write by policy, outside a committed repository",
                        HANDLER_MESSAGES["policy-workdir"][1])),
    "writable-branch": (("writable job refused on a protected branch", HANDLER_MESSAGES["writable-branch"][1]),),
    "inspect-writer": (("could not inspect a live writer's worktree", "submit again"),),
    "worktree-writer": (("worktree is held by a live writable job", HANDLER_MESSAGES["worktree-writer"][1]),),
    "conversation-writer": (("worktree is being written by a conversation turn",
                             HANDLER_MESSAGES["conversation-writer"][1]),),
    "revive-writer": (("session has a live writable job and a revive is a second instance of it",
                       "wait for the live job, or kill it before reviving or dispatching"),),
    "unidentified-writer": (("session has a live writable job and the submitting instance cannot be identified",
                             "submit from the session's own shell (CLAUDE_PID), or wait for the live job"),),
    "other-writer": (("session already has a live writable job from another instance",
                      "dispatch from that instance, or wait for or kill the live job"),),
    "writer-cap": (("session already holds its allowed live writable jobs (caps.max_writable_per_session)",
                    HANDLER_MESSAGES["writer-cap"][1]),),
    "writer-changed": (("session has a live writable job this submission was not checked against",
                        "submit again"),),
    "revive-held": (("session already has a live revive",
                     "inspect or kill the live job before reviving again"),),
    "resume-conversation": (("resume refused: a conversation's session continues only in its conversation",
                             HANDLER_MESSAGES["resume-conversation"][1]),),
    "resume-retention": (("resume refused: the source job is being archived by retention", "retry in a minute"),),
    "resume-provider": (("provider cannot resume a session in place", HANDLER_MESSAGES["resume-provider"][1]),),
    "mcp-provider": (("only a Claude launch starts MCP servers (C-12.9)", HANDLER_MESSAGES["mcp-provider"][1]),),
    "isolation-env": (("isolated review inherits a managed settings variable",
                       "use an unmanaged review environment"),),
    "isolation-layer": (("isolated Codex review has an unsupported configuration layer",
                         "use a lane with no managed or nonempty configuration layer"),),
    "adapter-missing": (("provider adapter has no constructor", None),),
    "enroll-bound": (("lanes enroll: the credential is already bound to a lane", "subfleet lanes list"),),
    "hold-note": ((f"lanes hold: {HOLD_NOTE_RULE}", None),),
    "unknown-lane": (("lanes {action}: unknown lane", "subfleet lanes list"),
                     ("lanes: unknown lane", "subfleet lanes list")),
    "transfer-held": (("lanes transfer: lane {lane_id} is held: its credential variable is one no lane "
                       "may name, so it holds no credential to move", HANDLER_MESSAGES["transfer-held"][1]),
                      ("lanes transfer: the lane is held: its credential variable is one no lane may name, "
                       "so it holds no credential to move", HANDLER_MESSAGES["transfer-held"][1])),
    "unknown-provider-lane": (("unknown {provider} lane", "subfleet lanes list"),
                              ("unknown lane", "subfleet lanes list")),
    "sessions-id": (("sessions: session_id is required", None),),
    "unknown-task": (("tasks: unknown task", HANDLER_MESSAGES["unknown-task"][1]),),
}

FALLBACK: tuple[str, None] = ("operation: request was refused", None)

#: `details` key a by-policy refusal carries (d261): the task whose policy made the job write.
BY_POLICY = "by_policy"
_BY_POLICY_FIX = "pass -s read-only"

_SHAPES = {
    "job_id": re.compile(r"[0-9]{8}-[0-9]{6}-[a-z0-9-]{1," + str(JOB_ID_SLUG_MAX) + r"}\Z"),
    "lane_id": re.compile(r"(?:codex|claude)-[1-9][0-9]{0,8}\Z"),
    # A policy task name; the policy file is the operator's, the value still has a shape.
    "task": re.compile(r"[a-z][a-z0-9-]{0,39}\Z"),
    "model": re.compile(r"[a-z][a-z0-9._-]{0,79}\Z"),
}


#: A guard preflight verdict kind (`guard.preflight`) and its repair (C-14.2): the
#: kinds `checked` admits are this table's, so each one renders its own repair.
GUARD_REPAIRS = {
    "trust": "rerun subfleet doctor",
    "version": "install the supported Codex version, then rerun subfleet doctor",
    "config": "correct the guard preflight configuration, then rerun subfleet doctor",
    "environment": "restore the required directories and binaries, then rerun subfleet doctor",
}


def _choices() -> dict[str, tuple[str, ...]]:
    from .adapters.isolation import CODEX_LAYERS, MANAGED_ENV
    from .launch_env import ALLOWED_NAMES, SESSION_PROBE_NAMES, TEST_SESSION_NAMES, refused_reference
    return {
        # `lanes` actions, then `sessions` actions: the daemon names them, never the caller.
        "action": ("hold", "release", "release --limits", "transfer", "retire", "unretire", "revived", "nudged"),
        # `lanes hold --note`: the rule a note broke, each a contract text (C-9.6).
        "hold_rule": (HOLD_NOTE_RULE, HOLD_NOTE_CREDENTIAL_RULE),
        "repository": ("this repository has no commit yet", "this directory is not a git repository"),
        "variable": MANAGED_ENV,
        "layer": CODEX_LAYERS,
        "branch": ("main", "master"),
        "provider": ("claude", "codex"),
        "sandbox": tuple(sandbox.value for sandbox in Sandbox),
        "session_verb": ("resume", "revive"),
        "guard_kind": tuple(GUARD_REPAIRS),
        "checkout_problem": tuple(message for message, _ in CHECKOUT_REFUSALS.values()),
        "checkout_fix": tuple(fix for _, fix in CHECKOUT_REFUSALS.values()),
        "errno_name": tuple(errno.errorcode.values()),
        # D-ENV2: a held lane's variable, a name and never a value: each listed,
        # session or probe name that is refused. A harness hook is not listed, so
        # its refusal takes the next form, which names none.
        "lane_variable": tuple(sorted(name for name in (*ALLOWED_NAMES, *TEST_SESSION_NAMES, *SESSION_PROBE_NAMES)
                                      if refused_reference(name))),
    }


_COUNTS = ("count", "limit")


def value_names() -> frozenset[str]:
    """Every value name a template may format; each has a check (`checked`)."""
    return frozenset((*_choices(), *_SHAPES, *_COUNTS))


def fields(template: str | None) -> tuple[str, ...]:
    """The value names a template formats."""
    if template is None:
        return ()
    return tuple(name for _, name, _, _ in string.Formatter().parse(template) if name is not None)


def checked(name: str, value: Any) -> bool:
    """Is `value` one this module renders for `name`? Only exact types are read."""
    if name in _COUNTS:
        return type(value) is int and 0 <= value < 10 ** 9
    if type(value) is not str:
        return False
    choices = _choices()
    if name in choices:
        return value in choices[name]
    shape = _SHAPES.get(name)
    if shape is None or shape.fullmatch(value) is None:
        return False
    # Read from each hyphen as well: a job id's slug is the caller's name (review r6, P2).
    return not ids.holds_credential(value)


def render(reason: Any, details: Any = None) -> tuple[str, str | None]:
    """The public message and fix for a handler refusal's reason and details."""
    if type(reason) is not str or reason not in HANDLER_MESSAGES:
        return FALLBACK
    details = details if type(details) is dict else {}
    for message, fix in (HANDLER_MESSAGES[reason], *LESS_SPECIFIC.get(reason, ())):
        names = (*fields(message), *fields(fix))
        if all(checked(name, details.get(name)) for name in names):
            values = {name: details[name] for name in names}
            message, fix = message.format(**values), fix.format(**values) if fix is not None else None
            break
    else:                                   # unreachable while the last forms name no value
        return FALLBACK
    if reason == "guard-preflight" and checked("guard_kind", details.get("guard_kind")):
        kind = details["guard_kind"]
        if kind == "trust":
            message += "; verify hook SHA-256, TRUST and the pinned Codex version"
        # The renderer never raises: a kind admitted without a repair gets the generic one.
        fix = GUARD_REPAIRS.get(kind, HANDLER_MESSAGES["guard-preflight"][1])
    if details.get(BY_POLICY) is True:
        # d261: a writer's refusal of a job its caller did not ask to write.
        task = details.get("task") if checked("task", details.get("task")) else "these"
        message = f"{message} ({task} jobs write by policy)"
        fix = (fix + "; or " if fix else "") + _BY_POLICY_FIX
    return message, fix


def handler_refusal(error: BaseException) -> tuple[str, str | None]:
    """Reconstruct public text without rendering an exception or its repair."""
    return render(getattr(error, "reason", None), getattr(error, "details", None))


def by_policy(details: dict | None, task: str | None) -> dict:
    """`details` marked as a refusal of a job that writes because its task's policy says so."""
    return {**(details if type(details) is dict else {}), BY_POLICY: True, "task": task}


def refusal_detail(error: BaseException) -> str:
    """A refusal as a job's records keep it (its notice, an attempt's detail): the
    public message and fix (`protocol.refusal_text`), as the wire would show them."""
    from .protocol import refusal_text
    message, fix = refusal_text(error)
    return message + (f"; fix: {fix}" if fix else "")


#: An exception class name a record may carry; anything else reads as `Exception`.
_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def class_name(error: BaseException) -> str:
    name = type(error).__name__
    return name if _CLASS_NAME.match(name) else "Exception"


def errno_text(error: BaseException) -> str | None:
    """`[Errno N] <os.strerror(N)>` for an OS error with a known errno, else None."""
    number = getattr(error, "errno", None) if isinstance(error, OSError) else None
    if type(number) is not int or number not in errno.errorcode:
        return None
    return f"[Errno {number}] {os.strerror(number)}"


def fixed_error(error: BaseException) -> str:
    """An exception as a record a client reads may name it (C-25.7): its class name,
    and for an OS error its validated errno and `os.strerror` text; never its
    message, args or filename, which `scrubbed_words` gives daemon.log."""
    text = errno_text(error)
    return class_name(error) + (f": {text}" if text else "")


#: How much of one diagnostic daemon.log keeps.
LOG_LINE_MAX = 2000


def scrubbed_line(text: str, limit: int = LOG_LINE_MAX) -> str:
    """`text` for the local daemon.log only: scrubbed whole, folded, scrubbed again
    and cut without splitting a marker (C-25.5, `suggestions.scrubbed_line`)."""
    from .conversations.suggestions import scrubbed_line as line
    return line(text, limit)


def scrubbed_words(error: BaseException) -> str:
    """An exception's own words, `<class>: <text>`, for the local daemon.log only
    (`scrubbed_line`); its class alone when its text cannot be rendered."""
    try:
        text = f"{class_name(error)}: {error}"
    except Exception:                       # noqa: BLE001 - what cannot be rendered is not logged
        return class_name(error)
    return scrubbed_line(text)


def detail_line(op: str, error: BaseException, *, subject: str = "request") -> str | None:
    """A refusal's own words as daemon.log keeps them, scrubbed whole first (C-25.7):
    what its public template leaves out, such as which host-push check refused,
    which roster file a transfer could not read, or the OS error an argument check
    met. `subject` and `op` say what was refused: `request show`, or `job <id>` for
    a refusal a job's records keep. None when the template is all there is, or the
    words cannot be rendered."""
    from .protocol import REFUSAL_MESSAGES
    reason = getattr(error, "reason", None)
    try:
        words = str(error)
        if type(reason) is str and reason in REFUSAL_MESSAGES and words == REFUSAL_MESSAGES[reason]:
            return None                     # a request error: its template is all there is
        text = f"{subject} {op} refused ({class_name(error)}): {words}"
        fix = getattr(error, "fix", None)
        if fix:
            text += f"; fix: {fix}"
    except Exception:                       # noqa: BLE001 - what cannot be rendered is not logged
        return None
    return scrubbed_line(text)


def log_detail(log, op: str, error: BaseException, *, subject: str = "request") -> None:
    """`detail_line`, for the local daemon.log only."""
    line = detail_line(op, error, subject=subject)
    if line is not None:
        log.info("%s", line)
