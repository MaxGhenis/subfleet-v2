"""C-12.4: the job-scoped command rule that keeps a review from publishing.

Both providers run every shell command through zsh, and zsh startup (the
user's `.zshenv` and `.zprofile`, macOS `/etc/zprofile`'s `path_helper`, and
Claude's shell snapshot) puts Homebrew or `/usr/bin` ahead of the PATH wrapper
a review launch carries (#221 review r1b, P1). This rule does not depend on
PATH: each provider's pre-execution hook runs it on the command text before
any shell starts. Claude gets it through `--settings` (and `subfleet hook
PreToolUse` when the launch marker is set); Codex through a second PreToolUse
entry beside the never-rules guard, whose trust the guard preflight verifies.

Where it can name the command, it judges `gh` fail closed: a call that is not
a known read is refused (unknown verbs, aliases and extensions included), and
so is `gh api` unless its method is GET, judged as gh's own flag parser reads
the flags. It refuses `git push` (a second layer behind the launch's git
configuration). It looks through wrappers (`env`, `env -S`, `command`,
`xargs`, `sudo`, `timeout`, zsh's `-` and `repeat`, ...), `sh|bash|zsh -c`,
`eval`, command and process substitution, shell input (a pipe, a heredoc, a
here-string, a process substitution given to a shell, `source` or `.`),
`find -exec`, `parallel`, `watch`, leading redirections, and a command word
that is only known at run time (`$g pr comment`, `$(command -v gh) -R o/r pr
comment`, a glob or brace word). Text given to `eval` or a shell that is only
a parameter (`eval "$c"`, `sh -c "$c"`, `${=c}`) is refused when the command
text names gh anywhere.

Where it cannot name the command (a runner it doesn't know, a shell function,
a value git runs), a fallback judges every word equal to `gh` or ending in
`/gh` (after quote removal, without a leading `!`) as the start of a gh call,
and every word equal to `git` the same way, as do assignment values and the
values of `-c`, `-e` and `--exec`. Once such a call, or one after a run-time
word, reaches a gh command as gh's own parser finds it (aliases included, and
whatever flags come before the command or its verb), it gets the direct
path's judgment: refused unless `gh_read` accepts it, so one predicate decides
gh everywhere (#221 review r3c, P2). A first word gh doesn't know as a command
is not judged there, so `grep gh file` and `echo gh` pass.

It also refuses copying gh under another name (`ln`, `cp`, `install`, `mv`,
`rsync`, `ditto`, a `cat` or `dd` into a file), and calls that would print the
GitHub credential into the review's output (`gh auth token`, `gh auth
git-credential`, `gh auth status -t`, `--show-token`), with their own wording.

It cannot see code that runs `gh` or `git` from another language or a script
on disk, git that discards its environment, a raw HTTPS call with a credential
found on disk, a gh binary given another name outside the command text, a gh
alias or extension name reached through a runner the rule doesn't know, a
command assembled at run time from pieces that never spell gh, or a zsh alias
(which zsh does not expand in these shells). Standard library only, so the
hook starts fast.
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

#: `gh <command> <verb>` calls a review may make: each only reads. `ls` is gh's
#: own alias for each of these `list`s (#221 review r3c).
GH_READS: dict[str, frozenset[str]] = {
    "pr": frozenset({"view", "diff", "checks", "list", "ls", "status"}),
    "issue": frozenset({"view", "list", "ls", "status"}),
    "run": frozenset({"view", "list", "ls", "watch"}),
    "workflow": frozenset({"view", "list", "ls"}),
    "repo": frozenset({"view"}),
    "release": frozenset({"view", "list", "ls"}),
    "label": frozenset({"list", "ls"}),
    "search": frozenset({"prs", "issues", "commits", "code", "repos"}),
}
#: Every command gh 2.100.0 lists, and each one's subcommands, aliases included
#: (`gh help`, `gh <command> --help` and `gh <command> <subcommand> --help`,
#: read offline 2026-10-19, and for the aliases 2026-10-22, #221 review r3c):
#: `pr new` is `pr create`, `secret remove` is `secret delete`, `ls` is `list`.
#: Also commands of their own: cobra's `help`, `co` (the alias gh writes into a
#: new config, `pr checkout`), and the hidden commands found by name
#: (`credits`, `repo credits`, `repo garden`, `auth git-credential`). Where the
#: fallback or a run-time word reaches one of these commands, `gh_read` judges
#: the call, as on the direct path (`gh_writes`).
GH_SUBCOMMANDS: dict[str, frozenset[str]] = {name: frozenset(verbs.split()) for name, verbs in {
    "agent-task": "create list view", "alias": "delete import list ls set", "api": "",
    "attestation": "download trusted-root verify",
    "auth": "git-credential login logout refresh setup-git status switch token", "browse": "",
    "cache": "delete list ls", "co": "",
    "codespace": "code cp create delete edit jupyter list ls logs ports rebuild ssh stop view", "completion": "",
    "config": "clear-cache get list ls set", "copilot": "", "credits": "",
    "discussion": "create list ls comment edit view",
    "extension": "browse create exec install list ls remove uninstall search upgrade",
    "gist": "clone create new delete edit list ls rename view", "gpg-key": "add delete list ls", "help": "",
    "issue": ("create new list ls status close comment delete develop edit lock pin reopen transfer unlock unpin "
              "view"),
    "label": "clone create delete edit list ls", "licenses": "", "org": "list ls", "preview": "prompter",
    "pr": ("create new list ls status checkout co checks close comment diff edit lock merge ready reopen revert "
           "review unlock update-branch view"),
    "project": ("close copy create delete edit field-create field-delete field-list item-add item-archive "
                "item-create item-delete item-edit item-list link list ls mark-template unlink view"),
    "release": "create new list ls delete delete-asset download edit upload verify verify-asset view",
    "repo": ("create new list ls archive autolink clone credits delete deploy-key edit fork garden gitignore "
             "license read-dir read-file rename set-default sync unarchive view"),
    "ruleset": "check list ls view", "run": "cancel delete download list ls rerun view watch",
    "search": "code commits issues prs repos", "secret": "delete remove list ls set",
    "skill": "install add list ls preview show publish search update", "ssh-key": "add delete list ls",
    "status": "", "variable": "delete remove get list ls set", "workflow": "disable enable list ls run view",
    "version": "",
}.items()}
#: gh's command aliases, each listed in its command's `--help` (`gh cs` is
#: `gh codespace`): commands of their own, with their command's subcommands.
GH_COMMAND_ALIASES = {"agent": "agent-task", "agents": "agent-task", "agent-tasks": "agent-task",
                      "at": "attestation", "cs": "codespace", "ext": "extension", "extensions": "extension",
                      "rs": "ruleset", "skills": "skill"}
GH_SUBCOMMANDS.update((alias, GH_SUBCOMMANDS[name]) for alias, name in GH_COMMAND_ALIASES.items())
#: gh's flags that may come before its command or its subcommand, each taking a value.
GH_GLOBAL_VALUES = ("-R", "--repo", "--hostname")
#: `gh api`'s flags (`gh api --help`, 2.100.0): long names, and the shorthand
#: letters gh's flag parser (pflag) reads in a cluster: a boolean letter may
#: lead, and a value letter takes the rest of its word, or the next word.
API_LONG_VALUES = frozenset({"--cache", "--field", "--header", "--hostname", "--input", "--jq", "--method",
                             "--preview", "--raw-field", "--template"})
API_LONG_BOOLEANS = frozenset({"--allow-escape-sequences", "--include", "--paginate", "--silent", "--slurp",
                               "--verbose", "--help"})
API_SHORT_VALUES = {"F": "--field", "H": "--header", "q": "--jq", "X": "--method", "p": "--preview",
                    "f": "--raw-field", "t": "--template"}
API_SHORT_BOOLEANS = frozenset("i")
#: Git subcommands that publish. `http-push` is the dumb-HTTP push helper.
GIT_PUBLISH = frozenset({"push", "send-pack", "http-push"})
#: Git's global options that take their value as the next word.
GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                               "--super-prefix", "--config-env", "--exec-path"})

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish"})
#: Builtins that run a file's text in the current shell.
SOURCES = frozenset({"source", "."})
#: The files a shell or `source` reads its standard input through.
STDIN_FILES = frozenset({"-", "/dev/stdin", "/dev/fd/0"})
#: Words that start a command list without being its command.
KEYWORDS = frozenset({"if", "then", "else", "elif", "fi", "do", "done", "while", "until",
                      "for", "case", "esac", "!", "{", "}", "[[", "]]", "function", "coproc"})
#: Wrappers that run the rest of their words as a command, and the options of
#: each that take the next word as a value.
WRAPPERS: dict[str, frozenset[str]] = {
    "command": frozenset(), "builtin": frozenset(), "exec": frozenset({"-a"}),
    "nohup": frozenset(), "noglob": frozenset(), "nocorrect": frozenset(), "-": frozenset(),
    "caffeinate": frozenset({"-t", "-w"}), "unbuffer": frozenset(),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "gtimeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-U", "-T", "-r", "-t"}),
    "doas": frozenset({"-u", "-C"}),
    "xargs": frozenset({"-I", "-L", "-n", "-P", "-s", "-E", "-d", "-a", "-J", "-R", "-S"}),
    "repeat": frozenset(),
}
#: Wrappers that take no options: every word after them is the command (zsh precommands).
BARE_WRAPPERS = frozenset({"-", "noglob", "nocorrect"})
#: Wrappers whose first operand is not the command (a duration, zsh's `repeat` count).
POSITIONAL_FIRST = frozenset({"timeout", "gtimeout", "repeat"})
#: `env`'s options that take a value; `-S`/`--split-string` splits its value into words.
ENV_VALUES = frozenset({"-u", "--unset", "-S", "--split-string", "-C", "--chdir", "-P"})
#: GNU parallel's options that take a value, and the words that start its arguments.
PARALLEL_VALUES = frozenset({"-j", "-S", "--jobs", "--sshlogin", "-a", "--arg-file", "--colsep", "-d",
                             "--delimiter", "-E", "-I", "--joblog", "--results", "--tmpdir", "-X"})
PARALLEL_ARGUMENTS = frozenset({":::", "::::", ":::+", "::::+"})
#: `watch` joins its operands and runs them with `sh -c`; these options take a value.
WATCH_VALUES = frozenset({"-n", "--interval", "-q", "--equexit"})
#: Copiers: given a gh binary, each makes gh under another name (#221 review r2, P2 item 3).
COPIERS = frozenset({"ln", "cp", "install", "mv", "rsync", "ditto", "gln", "gcp", "ginstall", "gmv"})
#: Programs whose `-e` is a pattern, or whose `-c`/`-e` is another language's
#: code (C-12.4's stated limit): the fallback does not read those values as
#: shell text. Git's `-c` is read by `_git_refusal` instead.
NOT_SHELL_VALUES = frozenset({"grep", "egrep", "fgrep", "zgrep", "rg", "ag", "ack", "sed", "gsed", "awk",
                              "gawk", "perl", "ruby", "node", "bun", "deno", "osascript", "jq", "git"})
#: The flags whose value the fallback reads as shell text.
COMMAND_VALUE_FLAGS = frozenset({"-c", "-e", "--exec"})
#: `test`'s spellings: a `[` command word is not a glob.
TEST_WORDS = frozenset({"[", "]", "[[", "]]"})
SEPARATORS = ";&|()\n`"
PUNCTUATION = SEPARATORS + "<>"
#: Operators a punctuation run splits into: redirections first, then separators.
OPERATOR = re.compile(r"&>>|&>|<<<|<<|<>|<&|>&|>>|>\||[<>]|\|\||&&|;;|\|&|[;&|()\n`]")
REDIRECTIONS = frozenset({"<", ">", ">>", "<<", "<<<", "<>", "<&", ">&", ">|", "&>", "&>>"})
WRITES_FILE = frozenset({">", ">>", ">|", "&>", "&>>"})
ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?)=(.*)", re.S)
#: A heredoc's start, never a here-string's `<<<`.
HEREDOC = re.compile(r"(?<!<)<<-?(?!<)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
#: A word made only of parameter expansions (`$c`, `${=c}`, `$c$d`, `$@`): as
#: a command word its value may carry the whole command.
PARAMETERS = re.compile(r"(?:\$(?:\{[^{}]*\}|[A-Za-z_][A-Za-z0-9_]*|[@*#?$!0-9-])(?:\[[^\]]*\])?)+")
GLOB = re.compile(r"[*?\[]|\{[^{}]*[,.][^{}]*\}")
#: `gh` as a word, wherever it is: `/gh`, `-gh}` and `[gh]` count; `ghost`, `gh-dash` and `test_gh` do not.
GH_WORD = re.compile(r"(?<![\w.])gh(?![\w.-])")
#: Bound on nested `sh -c`, `eval` and substitutions; deeper text is judged by
#: `_mentions_publish` alone.
MAX_DEPTH = 6


class Refusal(str):
    """What a command would do that a review may not, in a few words (`gh pr
    comment`). `kind` picks the denial's wording (`DENIALS`): `publish`, or
    `credential` for a call that would print the GitHub token."""

    kind = "publish"


def _refusal(words: str, kind: str = "publish") -> Refusal:
    found = Refusal(words)
    found.kind = kind
    return found


def _without_globals(args: Sequence[str]) -> tuple[list[str], bool]:
    """gh's words after the `-R`/`--repo`/`--hostname` flags that may lead
    them, and whether only those led (another leading flag gives False)."""
    args = list(args)
    while args and args[0].startswith("-"):
        flag = args[0]
        if flag in GH_GLOBAL_VALUES and len(args) > 1:
            del args[:2]
        elif flag.startswith(("--repo=", "--hostname=")) or (flag.startswith("-R") and len(flag) > 2):
            del args[:1]
        else:
            return args, False
    return args, True


def _auth_status_reads(args: Sequence[str]) -> bool:
    """`gh auth status <args>` only reads: `-a`/`--active` and `-h`/`--hostname`.
    `-t`/`--show-token` prints the token; `--json` and the formats are not
    judged a read, since this rule could not verify offline that they mask it."""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--hostname":
            index += 2
            continue
        if arg == "--active" or arg.startswith("--hostname="):
            index += 1
            continue
        if not arg.startswith("-") or arg.startswith("--") or len(arg) < 2:
            return False
        for at, letter in enumerate(arg[1:], 1):
            if letter == "a":
                continue
            if letter == "h":
                index += at == len(arg) - 1      # the hostname is the next word
                break
            return False                          # `t`, or a letter gh doesn't define here
        index += 1
    return True


#: The flags gh's command parser reads as switches while it looks for the
#: command, then for the subcommand: gh defines `--help` everywhere and
#: `--version` only at its top level (its `-h` is `--hostname`, never help).
GH_SWITCHES = (frozenset({"--help", "--version"}), frozenset({"--help"}))


def _gh_path(args: Sequence[str]) -> tuple[list[str], list[str]]:
    """The command and subcommand gh would run for `gh <args>`, and the words it
    would give them, found the way gh's command parser (cobra) finds them
    (#221 review r3c, P2). Level by level, from the first word each time, the
    first word that is not a flag or a flag's value is taken, where a flag
    without `=` takes the next word as its value unless it is a switch there
    (`GH_SWITCHES`), a cluster such as `-Ro/r` takes none, and `--` ends the
    search. Real gh 2.100.0, offline: `gh --body x pr comment 7`, `gh pr -b x
    comment 7`, `gh pr -R o/r new` and `gh auth -h h token` found `pr comment`,
    `pr create` and `auth token`; `gh --version pr comment 7` found `pr` alone,
    and `gh -h pr comment` and `gh -- pr view` no command."""
    path: list[str] = []
    rest = list(args)
    for switches in GH_SWITCHES:
        index = 0
        while index < len(rest) and rest[index] != "--":
            arg = rest[index]
            if arg.startswith("-"):
                takes_value = "=" not in arg and arg not in switches and (arg.startswith("--") or len(arg) == 2)
                index += 2 if takes_value else 1
            elif arg:
                path.append(rest.pop(index))
                break
            else:
                index += 1
        else:
            break
    return path, rest


def gh_credential(args: Sequence[str]) -> bool:
    """Whether `gh <args>` would print the GitHub credential (#221 review r2,
    P2 item 4; r3c): `gh auth token`, `gh auth git-credential` (git's
    credential helper, which prints `password=<token>`), `gh auth status` with
    anything but its read flags, or `--show-token` anywhere, whatever flags
    come before the subcommand. Not publishing, but it would copy the token
    into the review's output."""
    if any(arg == "--show-token" or arg.startswith("--show-token=") for arg in args):
        return True
    path, rest = _gh_path(args)
    if path in (["auth", "token"], ["auth", "git-credential"]):
        return True
    return path == ["auth", "status"] and not _auth_status_reads(rest)


def gh_read(args: Sequence[str]) -> bool:
    """Whether `gh <args>` only reads. Fails closed on everything else, a call
    that would print the credential included. gh's `-R`/`--repo`/`--hostname`
    may come before the command and before its subcommand: gh reads `gh pr -R
    o/r view 7` as `pr view` (#221 review r3c, P3)."""
    if gh_credential(args):
        return False
    rest, only_globals = _without_globals(args)
    if not only_globals:
        return rest == ["--version"]
    if not rest:
        return False
    if rest == ["version"]:
        return True
    if rest[0] == "api":
        return _api_read(rest[1:])
    verb, only_globals = _without_globals(rest[1:])
    if not only_globals or not verb:
        return False
    if rest[0] == "auth" and verb[0] == "status":
        return _auth_status_reads(verb[1:])
    return verb[0] in GH_READS.get(rest[0], frozenset())


def _api_read(args: Sequence[str]) -> bool:
    """`gh api` reads only when its method is GET (#221 review r1b P2, r2 P3-5).

    The flags are read as gh's parser (pflag) reads them, and the last method
    wins. Real gh 2.100.0 against a closed localhost port sent GET for `-X GET
    -f q=…`, `--method GET --raw-field q=…`, `-iX GET`, `-X=GET` and `-Xget`
    (fields go to the query string), and POST for `-fq=x`, `-iXPOST` and `-X
    GET -X POST`. Fields with no method, `--input`, `graphql`, `--` and a flag
    gh doesn't define are not reads.
    """
    method: str | None = None
    fields = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--" or arg == "graphql":
            # Do not let a downstream parser reinterpret options after a separator;
            # a GraphQL document can be a mutation.
            return False
        if arg.startswith("--"):
            name, equals, value = arg.partition("=")
            if name in API_LONG_VALUES:
                if not equals:
                    index += 1
                    if index == len(args):
                        return False
                    value = args[index]
                if name == "--input":
                    return False
                if name == "--method":
                    method = value
                fields = fields or name in ("--field", "--raw-field")
            elif name not in API_LONG_BOOLEANS:
                return False
        elif arg.startswith("-") and len(arg) > 1:
            at = 1
            while at < len(arg):
                letter = arg[at]
                if letter in API_SHORT_BOOLEANS:
                    at += 1
                    continue
                if letter not in API_SHORT_VALUES:
                    return False
                value = arg[at + 1:]
                if value.startswith("="):
                    value = value[1:]                 # pflag: `-X=GET`
                elif not value:
                    index += 1
                    if index == len(args):
                        return False
                    value = args[index]
                if letter == "X":
                    method = value
                fields = fields or letter in "fF"
                break
        index += 1
    if method is None:
        method = "POST" if fields else "GET"
    return method.upper() == "GET"


def gh_writes(args: Sequence[str]) -> Refusal | None:
    """The judgment of `gh <args>` where the rule could not see `gh` as the
    command (the fallback, a run-time word; #221 review r3c, P2).

    Once gh's command parser would reach one of gh's commands (`_gh_path`;
    `GH_SUBCOMMANDS`, aliases included), or the call would print the
    credential, it is the direct path's judgment, `_gh_verdict`: so one
    predicate, `gh_read`, decides gh everywhere, and an alias, a hidden verb or
    a flag before the verb is refused as it is there. A first word gh does not
    know as a command is not judged: gh would refuse it, or run an alias or
    extension from its own config (C-12.4's stated limit), so `grep gh file`
    and `echo gh` pass.
    """
    path, _ = _gh_path(args)
    if (path and path[0] in GH_SUBCOMMANDS) or gh_credential(args):
        return _gh_verdict(args)
    return None


def _gh_verdict(args: Sequence[str]) -> Refusal | None:
    """`gh <args>`, wherever the rule finds the call: refused unless a known
    read (fail closed), with the credential wording when it would print the
    token. The refusal names the command and subcommand gh would run."""
    if gh_read(args):
        return None
    path, _ = _gh_path(args)
    return _refusal(" ".join(["gh", *(path or args[:2])]), "credential" if gh_credential(args) else "publish")


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
    # zsh's `=gh` expands to gh's path; a path names its last component; `.` is the builtin.
    return (PurePosixPath(word.lstrip("=")).name or word) if word else ""


def _names_gh_binary(word: str) -> bool:
    """A word that names gh: `gh`, `…/gh` (or `…/gh/`), `!gh`, `=gh`."""
    bare = word.lstrip("!").lstrip("=").rstrip("/")
    return bare == "gh" or bare.endswith("/gh")


def _names_gh(text: str) -> bool:
    """Whether the text names gh anywhere, quotes and backslashes removed."""
    return bool(GH_WORD.search(re.sub(r"[\\'\"]", "", text)))


def _dynamic(word: str) -> bool:
    """A command word that is only known when the shell expands it: a
    parameter, a substitution, a glob or a brace expansion."""
    return "$" in word or "`" in word or (word not in TEST_WORDS and bool(GLOB.search(word)))


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
            words = re.findall(r"[^\s;&|()<>]+", line)
            fed_to_shell = fed_to_shell or any(_name(word) in SHELLS | SOURCES | {"eval"} for word in words)
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


def _substitutions(text: str, *, literal_quotes: bool = False) -> tuple[str, list[str], list[str]] | None:
    """`text` with each `$(...)`, backquote and `<(...)`, `>(...)` or zsh
    `=(...)` replaced; the command substitutions' bodies; and the process
    substitutions' bodies.

    Single quotes stop expansion; double quotes do not. In an unquoted heredoc
    body (`literal_quotes`) quotes are plain characters. None when a
    substitution never closes.
    """
    out: list[str] = []
    commands: list[str] = []
    processes: list[str] = []
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
        opens = text.startswith("(", index + 1)
        process = quote is None and not literal_quotes and opens and (
            char in "<>" or (char == "=" and (index == 0 or text[index - 1] in " \t\n;&|(")))
        if text.startswith("$(", index) or process:
            end = _close(text, index + 2)
            if end < 0:
                return None
            (processes if process else commands).append(text[index + 2:end])
            out.append(PROCESS_FILE if process else SUBSTITUTED)
            index = end + 1
            continue
        if char == "`":
            end = index + 1
            while end < len(text) and text[end] != "`":
                end += 2 if text[end] == "\\" else 1
            if end >= len(text):
                return None
            commands.append(text[index + 1:end])
            out.append(SUBSTITUTED)
            index = end + 1
            continue
        out.append(char)
        index += 1
    return "".join(out), commands, processes


def _segments(text: str) -> list[list[str]] | None:
    """Simple commands, as words; a redirection operator is a word of its own."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=PUNCTUATION)
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
        if token and all(char in PUNCTUATION for char in token):
            for part in OPERATOR.findall(token):
                if all(char in SEPARATORS for char in part):
                    if words:
                        segments.append(words)
                    words = []
                else:
                    words.append(part)
        else:
            words.append(token)
    if words:
        segments.append(words)
    return segments


def _mentions_publish(text: str) -> bool:
    """The fallback for text that cannot be tokenized: any gh call, or git push."""
    return bool(re.search(r"(^|[^\w.-])gh\s", text) or re.search(r"\bgit\b[^;&|\n]*\bpush\b", text))


def _redirections(words: list[str]) -> tuple[list[str], list[str], bool, bool]:
    """(the words without redirections, here-string texts, whether standard
    input is a process substitution, whether output goes to a file)."""
    kept: list[str] = []
    herestrings: list[str] = []
    stdin_process = writes_file = False
    index = 0
    while index < len(words):
        word = words[index]
        follows = words[index + 1] if index + 1 < len(words) else ""
        if follows in REDIRECTIONS and (word.isdigit() or re.fullmatch(r"\{[A-Za-z_][A-Za-z0-9_]*\}", word)):
            index += 1                              # `2>`, zsh's `{fd}>`: the descriptor
            continue
        if word in REDIRECTIONS:
            if word == "<<<":
                herestrings.append(follows)
            elif word in ("<", "<>") and follows == PROCESS_FILE:
                stdin_process = True
            elif word in WRITES_FILE and follows not in ("/dev/null", "-") and not follows.isdigit():
                writes_file = True
            index += 2
            continue
        kept.append(word)
        index += 1
    return kept, herestrings, stdin_process, writes_file


def _skip_wrapper(name: str, words: list[str], index: int) -> int:
    if name in BARE_WRAPPERS:
        return index
    values = WRAPPERS[name]
    positional = name in POSITIONAL_FIRST
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if word.startswith("-") and len(word) > 1:
            index += 2 if word in values else 1
            continue
        if positional:
            positional = False
            index += 1
            continue
        break
    return index


def _split(value: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def _env(words: list[str], index: int) -> tuple[list[str], int]:
    """`env`'s words from `index`: the command's index, with any `-S` value
    split into words in its place (`env -S 'gh pr comment 7'`)."""
    while index < len(words):
        word = words[index]
        if word == "--":
            return words, index + 1
        name, equals, value = word.partition("=")
        split = None
        if word in ("-S", "--split-string") and index + 1 < len(words):
            split, after = words[index + 1], index + 2
        elif word.startswith("-S") and len(word) > 2:
            split, after = word[2:], index + 1
        elif name == "--split-string" and equals:
            split, after = value, index + 1
        if split is not None:
            words = words[:index] + _split(split) + words[after:]
            continue
        if ASSIGNMENT.fullmatch(word) or (word.startswith("-") and len(word) > 1 and word not in ENV_VALUES):
            index += 1
        elif word in ENV_VALUES:
            index += 2
        else:
            break
    return words, index


def _shell_script(args: list[str]) -> tuple[str | None, bool, str | None]:
    """(`-c` script, reads stdin as its script, script operand) for `sh|bash|zsh <args>`."""
    command_string = stdin_flag = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            index += 2
            continue
        if arg.startswith(("-", "+")) and len(arg) > 1 and not any(char.isspace() for char in arg):
            short = not arg.startswith("--")
            command_string = command_string or (short and "c" in arg[1:])
            stdin_flag = stdin_flag or (short and arg.startswith("-") and "s" in arg[1:])
            index += 1
            continue
        break
    operands = args[index:]
    if command_string:
        return (operands[0] if operands else ""), False, None
    operand = operands[0] if operands else None
    return None, stdin_flag or operand is None or operand in STDIN_FILES, None if stdin_flag else operand


def _value_refusal(value: str, depth: int, names_gh: bool) -> Refusal | None:
    """A value a program runs as a command (an editor, an alias, `-c`): shell
    text when it has more than one word."""
    value = value[1:] if value.startswith("!") else value
    if not re.search(r"[\s;&|()<>`$]", value):
        return None
    return command_refusal(value, depth + 1, names_gh)


def _git_refusal(args: list[str], depth: int, names_gh: bool) -> Refusal | None:
    """`git push`, or a git config value that runs gh (an editor, a pager, a
    `!` alias): from `-c name=value` and from `git config <name> <value>`."""
    if git_publishes(args):
        return _refusal("git push")
    values: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in GIT_VALUE_OPTIONS:
            if arg == "-c" and index + 1 < len(args):
                values.append(args[index + 1].partition("=")[2])
            index += 2
            continue
        if arg.startswith("-"):
            if arg.startswith("-c") and len(arg) > 2:
                values.append(arg[2:].partition("=")[2])
            index += 1
            continue
        if arg == "config":
            values.extend(word for word in args[index + 1:] if not word.startswith("-"))
        break
    for value in values:
        found = _value_refusal(value, depth, names_gh)
        if found:
            return found
        if any(_names_gh_binary(word) for word in value.lstrip("!").split()):
            return _refusal("gh under another name")
    return None


def _parallel(args: list[str], depth: int, names_gh: bool) -> tuple[Refusal | None, bool]:
    """GNU parallel: its command template and its arguments are shell text, and
    with neither it reads commands from standard input. (refusal, reads stdin)"""
    index = 0
    while index < len(args) and args[index].startswith("-") and args[index] not in PARALLEL_ARGUMENTS:
        index += 2 if args[index] in PARALLEL_VALUES else 1
    words = args[index:]
    cut = next((at for at, word in enumerate(words) if word in PARALLEL_ARGUMENTS), None)
    template = words if cut is None else words[:cut]
    texts = [" ".join(template)] if template else []
    texts += [word for word in (words[cut + 1:] if cut is not None else []) if word not in PARALLEL_ARGUMENTS]
    for text in texts:
        found = command_refusal(text, depth + 1, names_gh)
        if found:
            return found, False
    return None, not template and cut is None


def _runtime(word: str, rest: list[str], names_gh: bool) -> Refusal | None:
    """A command word only known at run time (#221 review r2, P2).

    Followed by a gh read, or by words that reach a gh command whatever flags
    come first (`$(command -v gh) -R o/r pr comment`, `{g,}h pr new`), it is
    judged as gh would be (`gh_writes`, #221 review r3c); followed by a push,
    as git. A word that is only parameters (`$c`, `${=c}`, `"$@"`) may carry
    the whole command, so it is refused when the command text names gh
    anywhere.
    """
    if gh_read(rest):
        return None
    found = gh_writes(rest)
    if found:
        return found
    if rest and git_publishes(rest):
        return _refusal("git push")
    if names_gh and PARAMETERS.fullmatch(word):
        return _refusal("gh through a run-time command")
    return None


def _fallback(words: list[str], command: str, depth: int, names_gh: bool) -> Refusal | None:
    """The words of a command the rule could not otherwise judge (#221 review
    r2, P2 item 2): every word naming gh starts a gh call, judged as the direct
    path judges gh once it reaches a gh command (`gh_writes`, r3c), as does a
    word naming git (`git_publishes`); assignment values and the values of
    `-c`, `-e` and `--exec` are read as shell text."""
    shell_values = command not in NOT_SHELL_VALUES and not command.startswith("python")
    for at, word in enumerate(words):
        bare = word.lstrip("!").lstrip("=")
        found = None
        if bare == "gh" or bare.endswith("/gh"):
            found = gh_writes(words[at + 1:])
        elif (bare == "git" or bare.endswith("/git")) and git_publishes(words[at + 1:]):
            found = _refusal("git push")
        elif _dynamic(word):
            # `arch -arm64 $(command -v gh) pr comment 7`: a run-time word behind
            # a runner the rule doesn't know, judged as a run-time command word.
            found = _runtime(word, words[at + 1:], False)
        assignment = ASSIGNMENT.fullmatch(word)
        if found is None and assignment:
            found = _value_refusal(assignment[2], depth, names_gh)
        if found is None and shell_values:
            if word in COMMAND_VALUE_FLAGS and at + 1 < len(words):
                found = _value_refusal(words[at + 1], depth, names_gh)
            elif word.startswith("--exec="):
                found = _value_refusal(word[len("--exec="):], depth, names_gh)
        if found:
            return found
    return None


def _judge(words: list[str], depth: int, names_gh: bool = False) -> tuple[Refusal | None, set[str]]:
    """(refusal, what this command reads as a script: `stdin`, `process`) for one simple command."""
    words, herestrings, stdin_process, writes_file = _redirections(words)
    index = 0
    via_xargs = False
    while index < len(words):
        word = words[index]
        name = _name(word)
        if ASSIGNMENT.fullmatch(word) or name in KEYWORDS:
            index += 1
        elif name == "command" and index + 1 < len(words) and words[index + 1] in ("-v", "-V"):
            return None, set()          # a lookup, not a run
        elif name == "env":
            words, index = _env(words, index + 1)
        elif name in WRAPPERS:
            via_xargs = via_xargs or name == "xargs"
            index = _skip_wrapper(name, words, index + 1)
        else:
            break
    if index >= len(words):
        return _fallback(words, "", depth, names_gh), set()
    word, rest = words[index], words[index + 1:]
    name = _name(word)
    reason: Refusal | None = None
    feeds: set[str] = set()
    if via_xargs and (name in SHELLS or name == "eval"):
        feeds.add("stdin")              # `xargs -I{} sh -c '{}'`: the input becomes the script
    if name in SHELLS or name in SOURCES:
        script, reads_stdin, operand = (_shell_script(rest) if name in SHELLS else
                                        (None, bool(rest) and rest[0] in STDIN_FILES, rest[0] if rest else None))
        if script is not None:
            reason = command_refusal(script, depth + 1, names_gh)
        if operand == PROCESS_FILE:
            feeds.add("process")        # `bash <(echo …)`, `source <(…)`
        if reads_stdin and script is None:
            feeds.add("stdin")
            if stdin_process:
                feeds.add("process")    # `bash < <(echo …)`
            for text in herestrings:    # `bash <<< '…'`
                reason = reason or command_refusal(text, depth + 1, names_gh)
    elif name == "eval":
        reason = command_refusal(" ".join(rest), depth + 1, names_gh)
    elif name == "gh":
        reason = _gh_verdict(rest)
    elif name == "git":
        reason = _git_refusal(rest, depth, names_gh)
    elif name == "find":
        for at, arg in enumerate(rest):
            if arg in ("-exec", "-execdir", "-ok", "-okdir"):
                inner = []
                for item in rest[at + 1:]:
                    if item in (";", "+", "{}+"):
                        break
                    inner.append(item)
                reason = reason or _judge(inner, depth + 1, names_gh)[0]
    elif name == "parallel":
        reason, reads_stdin = _parallel(rest, depth, names_gh)
        if reads_stdin:
            feeds.add("stdin")
    elif name == "watch":
        at = 0
        while at < len(rest) and rest[at].startswith("-") and not any(char.isspace() for char in rest[at]):
            at += 2 if rest[at] in WATCH_VALUES else 1
        reason = command_refusal(" ".join(rest[at:]), depth + 1, names_gh)
    elif name in COPIERS:
        if any(_names_gh_binary(arg) or (names_gh and _dynamic(arg)) for arg in rest if not arg.startswith("-")):
            reason = _refusal("gh under another name")
    elif name == "dd":
        if any(arg.startswith("if=") and _names_gh_binary(arg[3:]) for arg in rest):
            reason = _refusal("gh under another name")
    elif name == "cat" and writes_file:
        if any(_names_gh_binary(arg) for arg in rest if not arg.startswith("-")):
            reason = _refusal("gh under another name")
    elif _dynamic(word):
        reason = _runtime(word, rest, names_gh)
    return reason or _fallback(words, name, depth, names_gh), feeds


def _data_refusal(segments: list[list[str]], depth: int, names_gh: bool) -> Refusal | None:
    """Data a shell will read as its script (`echo gh pr comment 7 | sh`): each
    command's arguments, joined, and each of its words that holds a space."""
    for words in segments:
        for text in (" ".join(words[1:]), *(word for word in words if any(char.isspace() for char in word))):
            reason = command_refusal(text, depth + 1, names_gh)
            if reason:
                return reason
    return None


def command_refusal(command: str | Sequence[str], depth: int = 0,
                    names_gh: bool | None = None) -> Refusal | None:
    """What a shell command would publish (`gh pr comment`, `git push`), or None.

    A list is an argument vector, run with no shell; a string is shell text.
    `names_gh` is whether the whole command, as first given, names gh.
    """
    if names_gh is None:
        names_gh = _names_gh(command if isinstance(command, str) else " ".join(map(str, command)))
    if not isinstance(command, str):
        reason, _ = _judge([str(word) for word in command], depth, names_gh)
        return reason
    if depth > MAX_DEPTH:
        return _refusal("nested command") if _mentions_publish(command) else None
    text, bodies, stdin_shell = _strip_heredocs(command.replace("\\\n", " "))
    expanded = _substitutions(text)
    segments = _segments(expanded[0]) if expanded is not None else None
    if expanded is None or segments is None:
        # The shell would refuse unbalanced quoting too; fail closed on what it names.
        return _refusal("unparsed command") if _mentions_publish(command) else None
    inner = [*expanded[1], *expanded[2]]
    processes = list(expanded[2])
    for body, quoted in bodies:
        # An unquoted heredoc still runs its substitutions, whatever reads it.
        found = None if quoted else _substitutions(body, literal_quotes=True)
        if found is not None:
            inner.extend([*found[1], *found[2]])
    for text in inner:
        reason = command_refusal(text, depth + 1, names_gh)
        if reason:
            return reason
    process_shell = False
    for words in segments:
        reason, feeds = _judge(words, depth, names_gh)
        if reason:
            return reason
        stdin_shell = stdin_shell or "stdin" in feeds
        process_shell = process_shell or "process" in feeds
    if stdin_shell:
        # `... | sh` or `bash <<EOF`: the data may be the script.
        for body, _quoted in bodies:
            reason = command_refusal(body, depth + 1, names_gh)
            if reason:
                return reason
        reason = _data_refusal(segments, depth, names_gh)
        if reason:
            return reason
    if stdin_shell or process_shell:
        # `bash <(echo gh pr comment 7)`: what the process prints is the script.
        for body in processes:
            found = _segments(body)
            reason = command_refusal(body, depth + 1, names_gh) or (
                _data_refusal(found, depth, names_gh) if found else None)
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


def payload_refusal(payload: dict[str, Any]) -> Refusal | None:
    command = payload_command(payload)
    return command_refusal(command) if command is not None else None


#: The deny reason each kind of refusal carries (C-12.4).
DENIALS = {
    "publish": ("subfleet review publish: denied ({refused}). A review's verdict is its deliverable: "
                "it never pushes, comments, reviews, merges or edits on GitHub. Reads still work: "
                "gh pr view|diff|checks|list|status, gh issue view|list, gh run view|list, gh api GETs."),
    "credential": ("subfleet review publish: denied ({refused}). A review never prints a GitHub credential: "
                   "gh auth token, gh auth git-credential, gh auth status -t and --show-token would copy the "
                   "token into the review's output. gh auth status alone still works."),
}


def denial(refused: str) -> dict[str, Any]:
    """The PreToolUse deny both providers read (`docs/reference/claude-hooks.md`
    section 4; the never-rules guard's I/O contract for Codex)."""
    template = DENIALS.get(getattr(refused, "kind", "publish"), DENIALS["publish"])
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": template.format(refused=refused),
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
