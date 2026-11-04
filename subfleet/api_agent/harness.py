"""The harness's process: setup, the tools, and the exit code (C-31.7 to C-31.17).

`main` reads the key once from the `--key-fd` pipe and closes the descriptor (C-31.17);
no environment variable is read for it. Before the SDK is imported, `main` takes every
`ANTHROPIC_*` variable and the proxy variables out of `os.environ` (`__main__` has
already overwritten their values in the exec block), starts nothing before then, and
gives the key to the client only.
"""
from __future__ import annotations

import os
import shutil
import signal
import stat
import sys
import traceback
from pathlib import Path

from subfleet import key_frame
from subfleet.api_agent import cli, hooks, pricing, prompt, sandbox
from subfleet.api_agent.editor import Editor
from subfleet.api_agent.loop import EXIT, Session, ToolOutcome, resume_history
from subfleet.api_agent.records import MESSAGES, HistoryError, Records, Redactor, load_history
from subfleet.api_agent.shell import COMMAND_TIMEOUT_S, Shell

#: The SDK's transport reads these whatever `trust_env` says (2.4.0, `_DefaultHttpxClient`).
PROXY_NAMES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE",
                         "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"})
LANE_ENV = "SUBFLEET_API_LANE"
MAX_PROMPT_BYTES = 10 * 1024 * 1024


class _Terminated(BaseException):
    """SIGTERM from the guardian: clean up, write no `end`, exit 1."""


def _terminate(signum, frame):  # noqa: ARG001 - a signal handler's signature
    raise _Terminated()


class _Setup(Exception):
    """A launch that cannot run; recorded as an `error` and an `error` end."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def _scrub_process_environment() -> tuple[dict[str, str], str | None]:
    """Remove the SDK's and the transport's control variables; return the launch
    environment the tools start from, and the test-only URL."""
    test_url = os.environ.pop(cli.TEST_URL_ENV, None)
    launch = dict(os.environ)
    for name in list(os.environ):
        if name.startswith("ANTHROPIC_") or name.upper() in PROXY_NAMES:
            del os.environ[name]
    return launch, test_url


def read_key(fd: int | None) -> tuple[str | None, str | None]:
    """C-31.17: the key, read once from the `--key-fd` pipe, whose descriptor is then
    closed; or None and why not. The key is kept in memory only, and the reason never
    holds a byte of it. Never raises."""
    if fd is None:
        return None, "no key arrived: the launch named no --key-fd"
    payload = None
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            return None, "no key arrived: --key-fd is not a pipe"
        payload = key_frame.read_frame(fd)
        return key_frame.decode(payload), None
    except OSError as exc:
        return None, f"no key arrived: --key-fd could not be read ({exc.strerror or type(exc).__name__})"
    except key_frame.FrameError as exc:
        return None, f"no key arrived: {exc}"
    finally:
        if payload is not None:
            key_frame.wipe(payload)
        try:
            os.close(fd)
        except OSError:
            pass


def _read_prompt(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise _Setup("prompt", f"cannot open the prompt file: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_PROMPT_BYTES + 1)
    if len(data) > MAX_PROMPT_BYTES:
        raise _Setup("prompt", f"the prompt file is larger than {MAX_PROMPT_BYTES} bytes")
    text = data.decode("utf-8", "replace")
    if not text.strip():
        raise _Setup("prompt", "the prompt file is empty")
    return text


class Tools:
    """The two client tools, behind the guard and the publish hook."""

    def __init__(self, *, layout: sandbox.Layout, shell: Shell, editor: Editor, guard: hooks.Hook | None,
                 publish: hooks.Hook | None, model: str) -> None:
        self.layout = layout
        self.shell = shell
        self.editor = editor
        self.guard = guard
        self.publish = publish
        self.model = model
        self.current_id = ""

    def guard_patch(self, patch: str) -> hooks.Verdict:
        if self.guard is None:
            return hooks.ALLOW
        call = hooks.payload("apply_patch", patch, str(self.layout.workdir), self.current_id, self.model)
        return self.guard.check(call, str(self.layout.workdir))

    def __call__(self, block: dict) -> ToolOutcome:
        self.current_id = str(block.get("id") or "")
        name, data = block.get("name"), block.get("input")
        if name == "bash":
            return self._bash(data)
        if name == "str_replace_based_edit_tool":
            result = self.editor.run(data)
            return ToolOutcome(result.text, result.is_error, None, result.denied, result.reason)
        return ToolOutcome(f"There is no tool named {name!r}; use bash or str_replace_based_edit_tool.", True,
                           None, False, None)

    def _bash(self, data: object) -> ToolOutcome:
        if not isinstance(data, dict):
            return ToolOutcome("The bash tool's input must be an object.", True, None, False, None)
        if data.get("restart") is True:
            self.shell.restart()
            return ToolOutcome("The shell was restarted in the worktree.", False, None, False, None)
        command = data.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolOutcome("The bash tool needs a `command`.", True, None, False, None)
        for hook in (self.guard, self.publish):
            if hook is None:
                continue
            verdict = hook.check(hooks.payload("Bash", command, self.shell.cwd, self.current_id, self.model),
                                 self.shell.cwd)
            if not verdict.allowed:
                return ToolOutcome(f"The {hook.label} refused this command, so it did not run: {verdict.reason}",
                                   True, None, True, verdict.reason)
        result = self.shell.run(command)
        text = result.output
        if result.timed_out:
            return ToolOutcome(text + f"\n[The command ran past {COMMAND_TIMEOUT_S:g} s and was stopped. The "
                               "shell was restarted, so its directory and variables are reset.]", True, None,
                               False, None)
        if result.exited:
            text += (f"\n[The shell exited with status {result.rc}; the next command starts a new shell in "
                     "the worktree.]")
        elif result.rc:
            text += f"\n[exit status {result.rc}]"
        return ToolOutcome(text, False, result.rc, False, None)


def main(argv: list[str]) -> int:
    launch, test_url = _scrub_process_environment()
    try:
        options = cli.parse(argv)
    except cli.UsageError as exc:
        print(f"subfleet.api_agent: {exc}", file=sys.stderr)
        return EXIT["error"]
    # C-31.17: the one path a key takes in; a launch with none ends as a `no-key` error.
    key, key_problem = read_key(options.key_fd)
    redact = Redactor(key)
    attempt = options.attempt_dir
    if not attempt.is_dir() or attempt.is_symlink():
        print("subfleet.api_agent: --attempt-dir is not a folder", file=sys.stderr)
        return EXIT["error"]
    signal.signal(signal.SIGTERM, _terminate)
    records = None
    try:
        records = Records(attempt, redact)
        records.event("start", model=options.model, lane=launch.get(LANE_ENV), effort=options.effort,
                      sandbox=options.sandbox)
        try:
            return _run_held(options, key, key_problem, test_url, launch, records, redact)
        except _Setup as setup:
            records.event("error", status=None, error_type=setup.error_type, message=redact(setup.message))
            records.event("end", outcome="error", turns=0, usd=0.0)
            return EXIT["error"]
    except _Terminated:
        return EXIT["error"]
    except Exception as exc:  # noqa: BLE001 - every failure still ends the stream
        print(redact("".join(traceback.format_exception(exc))), file=sys.stderr)
        if records is not None:
            try:
                records.event("error", status=None, error_type="harness",
                              message=redact(f"{type(exc).__name__}: {exc}")[:2000])
                records.event("end", outcome="error", turns=0, usd=0.0)
            except Exception:  # noqa: BLE001
                pass
        return EXIT["error"]
    finally:
        if records is not None:
            records.close()


def _run_held(options: cli.Options, key: str | None, key_problem: str | None, test_url: str | None,
              launch: dict[str, str], records: Records, redact: Redactor) -> int:
    holder: dict[str, object] = {}
    try:
        return _run(options, key, key_problem, test_url, launch, records, redact, holder)
    finally:
        shell = holder.get("shell")
        if shell is not None:
            shell.close()
        api = holder.get("api")
        if api is not None:
            api.close()
        layout = holder.get("layout")
        if layout is not None:
            shutil.rmtree(layout.tmp, ignore_errors=True)


def _run(options: cli.Options, key: str | None, key_problem: str | None, test_url: str | None,
         launch: dict[str, str], records: Records, redact: Redactor, holder: dict) -> int:
    if not key:
        raise _Setup("no-key", key_problem or "no key arrived")
    if not pricing.known(options.model):
        raise _Setup("model", f"no price for model {options.model}, so --max-usd cannot hold")
    if not options.workdir.is_dir():
        raise _Setup("workdir", "--workdir is not a folder")
    guard = publish = None
    tmp_env = Path(os.path.realpath(options.attempt_dir)) / sandbox.TMP_DIR
    tool_env = sandbox.tool_environment(launch, tmp_env)
    for label, path in (("guard", options.guard_hook), ("publish", options.publish_hook)):
        if path is None:
            continue
        problem = hooks.validate(path)
        if problem:
            raise _Setup(f"{label}-hook", f"--{label}-hook {problem}")
        hook = hooks.Hook(path, "guard" if label == "guard" else "publish guard", tool_env)
        if label == "guard":
            guard = hook
        else:
            publish = hook
    if options.sandbox == cli.WORKSPACE_WRITE and guard is None:
        raise _Setup("guard-required", "a workspace-write attempt needs --guard-hook")
    job_prompt = _read_prompt(options.prompt)
    existing = load_history(options.attempt_dir / MESSAGES) if options.resume else None
    if not options.resume and (options.attempt_dir / MESSAGES).stat().st_size:
        raise _Setup("history", "messages.jsonl already holds a conversation; a resume passes --resume")
    layout = sandbox.layout(workdir=options.workdir, attempt=options.attempt_dir, sandbox=options.sandbox,
                            network=options.network, extra_protected=[options.prompt], environ=launch)
    holder["layout"] = layout
    profile_path = layout.control / "sandbox.sb"
    profile_path.write_text(sandbox.profile(layout), encoding="utf-8")
    shell = Shell(sandbox.command(profile_path, []), tool_env, layout.workdir, layout.control)
    holder["shell"] = shell
    tools = Tools(layout=layout, shell=shell, editor=None, guard=guard, publish=publish, model=options.model)
    tools.editor = Editor(layout, tools.guard_patch if guard else None, layout.control / "backups",
                          readonly=options.sandbox != cli.WORKSPACE_WRITE)

    from subfleet.api_agent import client  # the SDK loads only now, with the environment clean
    api = client.Api(key, client.endpoint(test_url))
    holder["api"] = api

    if existing:
        history = list(existing)
        tail = resume_history(history)
    else:
        history = []
        tail = [{"role": "user", "content": [{"type": "text", "text": job_prompt}]}]
    session = Session(model=options.model, effort=options.effort,
                      system=prompt.system_blocks(options.sandbox, options.network), tools=prompt.tools(),
                      history=history, max_usd=options.max_usd, max_turns=options.max_turns, records=records,
                      redact=redact, api=api, run_tool=tools)
    for message in tail:
        session._append(message)
    return session.run()
