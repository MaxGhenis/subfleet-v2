"""C-12.4: the job-scoped command rule that keeps a review from publishing.

Both providers run every shell command through zsh, and zsh startup (the
user's `.zshenv` and `.zprofile`, macOS `/etc/zprofile`'s `path_helper`, and
Claude's shell snapshot) puts Homebrew or `/usr/bin` ahead of the PATH wrapper
a review launch carries (#221 review r1b, P1). This rule does not depend on
PATH: each provider's pre-execution hook runs it on the command text before
any shell starts. Claude gets it through `--settings` (and `subfleet hook
PreToolUse` when the launch marker is set); Codex through a second PreToolUse
entry beside the never-rules guard, whose trust the guard preflight verifies.

It refuses a `gh` call that is not a known read (fail closed: unknown verbs,
aliases and extensions are refused), `gh api` with a method other than GET,
fields or input, and `git push` (a second layer behind the launch's git
configuration). It looks through wrappers (`env`, `command`, `xargs`, `sudo`,
`timeout`, ...), `sh|bash|zsh -c`, `eval`, command substitution, `find -exec`,
a git alias given with `-c`, and a command word that only expands at run time
(`$g pr comment`). It cannot see code that runs `gh` from another language
(`python -c`, a script on disk) or a raw HTTPS call with a credential found on
disk. Standard library only, so the hook starts fast.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
import re
import shlex
import sys
from typing import Any, Sequence

#: The marker every publish-denied launch carries (C-5.1 markers' kin). A hook
#: process inherits the provider's environment, which the job cannot change.
PUBLISH_MARKER = "SUBFLEET_PUBLISH_DENIED"

#: `gh <command> <verb>` calls a review may make: each only reads.
GH_READS: dict[str, frozenset[str]] = {
    "pr": frozenset({"view", "diff", "checks", "list", "status"}),
    "issue": frozenset({"view", "list", "status"}),
    "run": frozenset({"view", "list", "watch"}),
    "workflow": frozenset({"view", "list"}),
    "repo": frozenset({"view"}),
    "release": frozenset({"view", "list"}),
    "label": frozenset({"list"}),
    "search": frozenset({"prs", "issues", "commits", "code", "repos"}),
}
#: `gh api` long options that write: they make gh send a body (and POST).
GH_API_WRITES = ("--field", "--raw-field", "--input")
#: Git subcommands that publish. `http-push` is the dumb-HTTP push helper.
GIT_PUBLISH = frozenset({"push", "send-pack", "http-push"})
#: Git's global options that take their value as the next word.
GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                               "--super-prefix", "--config-env", "--exec-path"})

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish"})
#: Words that start a command list without being its command.
KEYWORDS = frozenset({"if", "then", "else", "elif", "fi", "do", "done", "while", "until",
                      "for", "case", "esac", "!", "{", "}", "[[", "]]", "function", "coproc"})
#: Wrappers that run the rest of their words as a command, and the options of
#: each that take the next word as a value.
WRAPPERS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset", "-S", "--split-string", "-C", "--chdir", "-P"}),
    "command": frozenset(), "builtin": frozenset(), "exec": frozenset({"-a"}),
    "nohup": frozenset(), "noglob": frozenset(), "nocorrect": frozenset(),
    "caffeinate": frozenset({"-t", "-w"}), "unbuffer": frozenset(),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "gtimeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-U", "-T", "-r", "-t"}),
    "doas": frozenset({"-u", "-C"}),
    "xargs": frozenset({"-I", "-L", "-n", "-P", "-s", "-E", "-d", "-a", "-J", "-R", "-S"}),
    "parallel": frozenset({"-j", "-S", "--jobs"}),
}
#: Wrappers whose first operand is not the command (a duration).
POSITIONAL_FIRST = frozenset({"timeout", "gtimeout"})
SEPARATORS = ";&|()\n`"
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=.*", re.S)
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
#: Bound on nested `sh -c`, `eval` and substitutions; deeper text is judged by
#: `_mentions_publish` alone.
MAX_DEPTH = 6


def gh_read(args: Sequence[str]) -> bool:
    """Whether `gh <args>` only reads. Fails closed on everything else."""
    args = list(args)
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag in ("-R", "--repo", "--hostname") and args:
            args.pop(0)
        elif flag.startswith(("--repo=", "--hostname=")) or (flag.startswith("-R") and len(flag) > 2):
            continue
        else:
            return False
    if not args:
        return False
    if args[0] == "api":
        return _api_read(args[1:])
    verbs = GH_READS.get(args[0])
    return verbs is not None and len(args) >= 2 and args[1] in verbs


def _api_read(args: Sequence[str]) -> bool:
    """`gh api` reads only with GET and no body (#221 review r1b, P2).

    gh's flag parser lets a boolean flag lead a cluster: `-iXPOST` is `-i -X
    POST` and `-ifbody=x` is `-i -f body=x`. So a single-dash cluster with
    `f`, `F` or `X` in it is refused, except exactly `-XGET`, and `-X GET` as
    two words. Long forms are judged by name.
    """
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--" or arg == "graphql":
            # Do not let a downstream parser reinterpret options after a separator;
            # a GraphQL document can be a mutation.
            return False
        if arg.startswith("--"):
            name, equals, value = arg.partition("=")
            if name in GH_API_WRITES:
                return False
            if name == "--method":
                if not equals:
                    index += 1
                    value = args[index] if index < len(args) else ""
                if value != "GET":
                    return False
        elif arg.startswith("-") and len(arg) > 1:
            if arg == "-X":
                index += 1
                if index == len(args) or args[index] != "GET":
                    return False
            elif arg != "-XGET" and set(arg[1:]) & {"f", "F", "X"}:
                return False
        index += 1
    return True


def git_publishes(args: Sequence[str]) -> bool:
    """Whether `git <args>` would publish: its subcommand, or a `-c` alias for it."""
    aliases: dict[str, str] = {}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in GIT_VALUE_OPTIONS:
            value = args[index + 1] if index + 1 < len(args) else ""
            if arg == "-c":
                name, _, setting = value.partition("=")
                if name.lower().startswith("alias."):
                    aliases[name[len("alias."):].lower()] = setting
            index += 2
            continue
        if arg.startswith("-"):
            if arg.startswith("-c") and len(arg) > 2:
                name, _, setting = arg[2:].partition("=")
                if name.lower().startswith("alias."):
                    aliases[name[len("alias."):].lower()] = setting
            index += 1
            continue
        if arg in GIT_PUBLISH:
            return True
        setting = aliases.get(arg.lower())
        if setting is None:
            return False
        if setting.startswith("!"):
            return command_refusal(setting[1:]) is not None
        words = setting.split()
        return bool(words) and (words[0] in GIT_PUBLISH or git_publishes(words))
    return False


def _name(word: str) -> str:
    # zsh's `=gh` expands to gh's path; a path names its last component.
    return PurePosixPath(word.lstrip("=")).name if word else ""


def _dynamic(word: str) -> bool:
    """A command word that is only known when the shell expands it."""
    return "$" in word or "`" in word


def _strip_heredocs(command: str) -> tuple[str, list[tuple[str, bool]], bool]:
    """The command without heredoc bodies; the bodies, each with whether its
    delimiter was quoted (no expansion inside); and whether a shell reads one."""
    lines: list[str] = []
    bodies: list[tuple[str, bool]] = []
    delimiters: list[tuple[str, bool]] = []
    body: list[str] = []
    fed_to_shell = False
    for line in command.splitlines(keepends=True):
        if delimiters:
            if line.strip() == delimiters[0][0]:
                bodies.append(("".join(body), delimiters.pop(0)[1]))
                body = []
            else:
                body.append(line)
            continue
        lines.append(line)
        found = [(match[1], bool(match[0])) for match in HEREDOC.findall(line)]
        if found:
            delimiters.extend(found)
            words = re.findall(r"[^\s;&|()]+", line)
            fed_to_shell = fed_to_shell or any(_name(word) in SHELLS | {"eval", "source", "."} for word in words)
    if body and delimiters:
        bodies.append(("".join(body), delimiters[0][1]))
    return "".join(lines), bodies, fed_to_shell


#: What a command or process substitution leaves in its place: a word only the
#: shell can expand (`_dynamic`), or the file a process substitution names.
SUBSTITUTED = "$__subfleet_substitution__"
PROCESS_FILE = "/dev/fd/63"


def _close(text: str, start: int) -> int:
    """The index of the `)` closing a substitution whose body starts at `start`, or -1."""
    depth, index, quote = 1, start, None
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "\\":
            index += 2
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _substitutions(text: str, *, literal_quotes: bool = False) -> tuple[str, list[str]] | None:
    """`text` with each `$(...)`, backquote and `<(...)` replaced, and their bodies.

    Single quotes stop expansion; double quotes do not. In an unquoted heredoc
    body (`literal_quotes`) quotes are plain characters. None when a
    substitution never closes.
    """
    out: list[str] = []
    inner: list[str] = []
    index, quote = 0, None
    while index < len(text):
        char = text[index]
        if quote == "'":
            out.append(char)
            quote = None if char == "'" else quote
            index += 1
            continue
        if char == "\\":
            out.append(text[index:index + 2])
            index += 2
            continue
        if not literal_quotes and char in "'\"" and (quote is None or char == quote):
            quote = None if quote else char
            out.append(char)
            index += 1
            continue
        process = quote is None and not literal_quotes and char in "<>" and text.startswith("(", index + 1)
        if text.startswith("$(", index) or process:
            end = _close(text, index + 2 if not process else index + 2)
            if end < 0:
                return None
            inner.append(text[index + 2:end])
            out.append(PROCESS_FILE if process else SUBSTITUTED)
            index = end + 1
            continue
        if char == "`":
            end = index + 1
            while end < len(text) and text[end] != "`":
                end += 2 if text[end] == "\\" else 1
            if end >= len(text):
                return None
            inner.append(text[index + 1:end])
            out.append(SUBSTITUTED)
            index = end + 1
            continue
        out.append(char)
        index += 1
    return "".join(out), inner


def _segments(text: str) -> list[list[str]] | None:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=SEPARATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = []
    words: list[str] = []
    for token in tokens:
        if token and all(char in SEPARATORS for char in token):
            if words:
                segments.append(words)
            words = []
        else:
            words.append(token)
    if words:
        segments.append(words)
    return segments


def _mentions_publish(text: str) -> bool:
    """The fallback for text that cannot be tokenized: any gh call, or git push."""
    return bool(re.search(r"(^|[^\w.-])gh\s", text) or re.search(r"\bgit\b[^;&|\n]*\bpush\b", text))


def _skip_wrapper(name: str, words: list[str], index: int) -> int:
    values = WRAPPERS[name]
    positional = name in POSITIONAL_FIRST
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if name == "env" and ASSIGNMENT.fullmatch(word):
            index += 1
            continue
        if word.startswith("-") and len(word) > 1:
            index += 2 if word in values else 1
            continue
        if positional:
            positional = False
            index += 1
            continue
        break
    return index


def _shell_script(args: list[str]) -> tuple[str | None, bool]:
    """(`-c` script, reads stdin) for `sh|bash|zsh <args>`."""
    command_string = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg in ("-o", "+o", "-O", "+O"):
            index += 2
            continue
        if arg.startswith(("-", "+")) and len(arg) > 1:
            command_string = command_string or (not arg.startswith("--") and "c" in arg[1:])
            index += 1
            continue
        break
    operands = args[index:]
    if command_string:
        return (operands[0] if operands else ""), False
    return None, not operands or operands[0] == "-"


def _judge(words: list[str], depth: int) -> tuple[str | None, bool]:
    """(refusal, reads stdin as a script) for one simple command."""
    index = 0
    while index < len(words):
        word = words[index]
        name = _name(word)
        if ASSIGNMENT.fullmatch(word) or name in KEYWORDS:
            index += 1
        elif name == "command" and index + 1 < len(words) and words[index + 1] in ("-v", "-V"):
            return None, False          # a lookup, not a run
        elif name in WRAPPERS:
            index = _skip_wrapper(name, words, index + 1)
        else:
            break
    if index >= len(words):
        return None, False
    word, rest = words[index], words[index + 1:]
    name = _name(word)
    if name in SHELLS:
        script, stdin = _shell_script(rest)
        return (command_refusal(script, depth + 1) if script else None), stdin
    if name == "eval":
        return command_refusal(" ".join(rest), depth + 1), False
    if name == "gh":
        return (None if gh_read(rest) else "gh " + " ".join(rest[:2])), False
    if name == "git":
        return ("git push" if git_publishes(rest) else None), False
    if name == "find":
        for at, arg in enumerate(rest):
            if arg in ("-exec", "-execdir", "-ok", "-okdir"):
                inner = []
                for item in rest[at + 1:]:
                    if item in (";", "+", "{}+"):
                        break
                    inner.append(item)
                reason, _ = _judge(inner, depth + 1)
                if reason:
                    return reason, False
        return None, False
    if _dynamic(word):
        # `$g pr comment 7`, `$(command -v gh) api -XPOST ...`: judged as gh would be.
        if rest and rest[0] in (*GH_READS, "api", "alias", "extension", "gist", "secret", "variable") \
                and not gh_read(rest):
            return "gh " + " ".join(rest[:2]), False
        if rest and git_publishes(rest):
            return "git push", False
    return None, False


def command_refusal(command: str | Sequence[str], depth: int = 0) -> str | None:
    """What a shell command would publish (`gh pr comment`, `git push`), or None.

    A list is an argument vector, run with no shell; a string is shell text.
    """
    if not isinstance(command, str):
        reason, _ = _judge([str(word) for word in command], depth)
        return reason
    if depth > MAX_DEPTH:
        return "nested command" if _mentions_publish(command) else None
    text, bodies, stdin_shell = _strip_heredocs(command.replace("\\\n", " "))
    expanded = _substitutions(text)
    segments = _segments(expanded[0]) if expanded is not None else None
    if expanded is None or segments is None:
        # The shell would refuse unbalanced quoting too; fail closed on what it names.
        return "unparsed command" if _mentions_publish(command) else None
    inner = list(expanded[1])
    for body, quoted in bodies:
        # An unquoted heredoc still runs its substitutions, whatever reads it.
        found = None if quoted else _substitutions(body, literal_quotes=True)
        if found is not None:
            inner.extend(found[1])
    for text in inner:
        reason = command_refusal(text, depth + 1)
        if reason:
            return reason
    for words in segments:
        reason, stdin = _judge(words, depth)
        if reason:
            return reason
        stdin_shell = stdin_shell or stdin
    if stdin_shell:
        # `... | sh` or `bash <<EOF`: the data may be the script.
        for body, _quoted in bodies:
            reason = command_refusal(body, depth + 1)
            if reason:
                return reason
        for words in segments:
            # `echo gh pr comment 7 | sh`: a segment's arguments, and each quoted string.
            for text in (" ".join(words[1:]), *(word for word in words if any(char.isspace() for char in word))):
                reason = command_refusal(text, depth + 1)
                if reason:
                    return reason
    return None


def payload_command(payload: dict[str, Any]) -> str | list | None:
    """The command a PreToolUse payload would run, in either provider's shape."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if isinstance(command, str) or (isinstance(command, list) and all(isinstance(item, str) for item in command)):
        return command
    return None


def payload_refusal(payload: dict[str, Any]) -> str | None:
    command = payload_command(payload)
    return command_refusal(command) if command is not None else None


def denial(refused: str) -> dict[str, Any]:
    """The PreToolUse deny both providers read (`docs/reference/claude-hooks.md`
    section 4; the never-rules guard's I/O contract for Codex)."""
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"subfleet review publish: denied ({refused}). A review's verdict is its deliverable: "
            "it never pushes, comments, reviews, merges or edits on GitHub. Reads still work: "
            "gh pr view|diff|checks|list|status, gh issue view|list, gh run view|list, gh api GETs."),
    }}


def hook_main(stdin: Any = None, stdout: Any = None) -> int:
    """The PreToolUse hook a publish-denied launch runs. Always exits 0: the
    JSON on stdout decides, and nothing at all means allow."""
    try:
        payload = json.loads((stdin or sys.stdin).read() or "{}")
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    refused = payload_refusal(payload)
    if refused:
        print(json.dumps(denial(refused)), file=stdout or sys.stdout)
    return 0
