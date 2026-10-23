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
the flags. gh's command and subcommand must be spelled literally: a word only
known at run time before gh's verb is settled (`gh $v comment`, `gh -R "$r" pr
view`), or arguments that come from `xargs` or GNU `parallel`, make a call no
read (#221 review r4, P2-1). It refuses `git push` (a second layer behind the
launch's git configuration). It looks through wrappers (`env` with its option
clusters and `-S`, `command`, `xargs`, `sudo`, `timeout`, zsh's `-` and
`repeat`, ...), every shell in `SHELLS` given `-c` in any option cluster,
`eval`, command and process substitution, shell input (a pipe, a heredoc, a
here-string, a process substitution given to a shell, `source` or `.`),
`find -exec`, `parallel`, `watch`, leading redirections, and a command word
that is only known at run time (`$g pr comment`, `$(command -v gh) -R o/r pr
comment`, a glob or brace word). Text given to `eval` or a shell that is only
a parameter (`eval "$c"`, `sh -c "$c"`, `${=c}`) is refused when the command
text names gh anywhere. An `env -S` value with a backslash or `${` is refused:
env applies its own escapes and environment there (#221 review r4, P2-2).

Where it cannot name the command (a runner it doesn't know, a shell function,
a value git runs), a fallback judges every word equal to `gh` or ending in
`/gh` (after quote removal, without a leading `!`) as the start of a gh call,
and every word equal to `git` the same way, as do assignment values and the
values of `-c`, `-e` and `--exec`; a word naming a shell, `env` or `xargs`
starts a command judged as the direct path judges it.
Once such a call, or one after a run-time word, reaches a gh command as gh's
own parser finds it (aliases included, and whatever flags come before the
command or its verb), it gets the direct path's judgment: refused unless
`gh_read` accepts it, so one predicate decides gh everywhere (#221 review r3c,
P2). So does a gh call whose command is known only at run time or whose
arguments come from `xargs`, unless its program is a text tool
(`NOT_SHELL_VALUES`), so `grep gh "$f"` passes. A first word gh doesn't know
as a command is not judged there, so `grep gh file` and `echo gh` pass.

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

import contextvars
import fnmatch
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

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh"})
#: The C shells: they have no `-o`, and their `-b` ends the options (#221 review r4, P2-2).
C_SHELLS = frozenset({"csh", "tcsh"})
#: Runners the fallback finds among a command's words, each the start of a command the direct path judges
#: (not among gh's own arguments: `gh run watch`).
NESTED_RUNNERS = SHELLS | {"env", "xargs"}
#: Programs that only print, find or inspect the words they are given, never run them: among their words
#: a shell's name (`which bash`, `ls /bin/sh`) starts no command.
INSPECTORS = frozenset({"echo", "printf", "print", "which", "type", "whence", "where", "whereis", "file", "ls",
                        "stat", "man", "readlink", "realpath", "basename", "dirname", "cat", "head", "tail", "wc"})
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
#: `env`'s option letters that take a value, as the rest of their word or the next word (macOS `env`:
#: `[-0iv] [-C workdir] [-P utilpath] [-S string] [-u name]`; GNU env's `-a`), and GNU env's long options
#: that do. `-S`/`--split-string` splits its value into words.
ENV_SHORT_VALUES = frozenset("CPSua")
ENV_LONG_VALUES = ("--argv0", "--chdir", "--split-string", "--unset")
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
#: `gh` as a word, wherever it is and in any case: `/gh`, `-gh}`, `[gh]` and `GH` count; `ghost`,
#: `gh-dash`, `test_gh` and `GH_TOKEN` do not.
GH_WORD = re.compile(r"(?<![\w.])gh(?![\w.-])", re.IGNORECASE)
#: A variable the command text sets (`g=gh`, `export g="$(command -v gh)"`), with its value, and a `for`
#: loop's variable with its list: a parameter word reading one whose value names gh may be gh (#221 review r4).
SETS_VARIABLE = re.compile(r"(?<![\w$])([A-Za-z_][A-Za-z0-9_]*)\+?="
                           r"('[^']*'|\"(?:[^\"\\]|\\.)*\"|\$\([^)]*\)|`[^`]*`|[^\s;&|()<>]*)")
FOR_VARIABLE = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+([^;\n]*)")
#: The variable names a word reads (`$g`, `${g}`, `${=g}`, `${g:-x}`).
READS_VARIABLE = re.compile(r"\$\{?[=#!^~+]*([A-Za-z_][A-Za-z0-9_]*)")
#: A command substitution whose command is gh (`$(gh pr list -q …)`): it prints gh's output, not gh.
GH_CALL = re.compile(r"\$\(\s*(?:[^\s;&|()]*/)?gh(?=[\s)])[^)]*\)|`\s*(?:[^\s;&|`]*/)?gh(?=[\s`])[^`]*`",
                     re.IGNORECASE)
#: Bound on nested `sh -c`, `eval` and substitutions; deeper text is judged by
#: `_mentions_publish` alone.
MAX_DEPTH = 6
#: The variables the command being judged sets to a value naming gh (`SETS_VARIABLE`), set by the
#: outermost `command_refusal` for the calls under it.
_GH_VARIABLES: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar("gh_variables",
                                                                              default=frozenset())


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


def _runtime_word(word: str) -> bool:
    """A word the shell expands at run time (`_dynamic`), or the file a process substitution names."""
    return _dynamic(word) or word == PROCESS_FILE


def _gh_unsettled(args: Sequence[str]) -> bool:
    """Whether a word known only at run time comes before gh's verb is settled
    (#221 review r4, P2-1): the command, the subcommand of a command that has
    them, or a flag or a flag's value before either, as `_gh_path` finds them.
    Such a word can be, or split into, the command itself (`gh $v comment`, `gh
    ${=a}`, `gh -R ${=a} pr view`). After a command without subcommands, one gh
    doesn't know, or the subcommand, the verb is settled: `gh pr view "$n"` and
    `gh api "repos/$r/pulls"` are judged as written."""
    rest = list(args)
    for level, switches in enumerate(GH_SWITCHES):
        index = 0
        while index < len(rest) and rest[index] != "--":
            arg = rest[index]
            if _runtime_word(arg):
                return True
            if arg.startswith("-"):
                takes_value = "=" not in arg and arg not in switches and (arg.startswith("--") or len(arg) == 2)
                if takes_value and index + 1 < len(rest) and _runtime_word(rest[index + 1]):
                    return True
                index += 2 if takes_value else 1
            elif arg:
                if level or not GH_SUBCOMMANDS.get(rest.pop(index)):
                    return False
                break
            else:
                index += 1
        else:
            return False
    return False


def gh_read(args: Sequence[str]) -> bool:
    """Whether `gh <args>` only reads. Fails closed on everything else, a call
    that would print the credential included. gh's `-R`/`--repo`/`--hostname`
    may come before the command and before its subcommand: gh reads `gh pr -R
    o/r view 7` as `pr view` (#221 review r3c, P3). The command, the
    subcommand and every word before them must be literal (`_gh_unsettled`,
    #221 review r4, P2-1)."""
    if gh_credential(args) or _gh_unsettled(args):
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


def _gh_reaches(args: Sequence[str]) -> bool:
    """Whether gh's command parser would reach one of gh's commands (`_gh_path`;
    `GH_SUBCOMMANDS`, aliases included), or the call would print the credential."""
    path, _ = _gh_path(args)
    return bool(path and path[0] in GH_SUBCOMMANDS) or gh_credential(args)


def gh_writes(args: Sequence[str], *, fed: bool = False, unsettled: bool = True) -> Refusal | None:
    """The judgment of `gh <args>` where the rule could not see `gh` as the
    command (the fallback; #221 review r3c, P2).

    Once gh's command parser would reach one of gh's commands, or the call
    would print the credential, it is the direct path's judgment, `_gh_verdict`:
    so one predicate, `gh_read`, decides gh everywhere, and an alias, a hidden
    verb or a flag before the verb is refused as it is there. So is a call
    whose arguments come from `xargs` (`fed`), or one with a word known only
    at run time before its verb is settled (`_gh_unsettled`, #221 review r4,
    P2-1); `unsettled=False` leaves that out, for gh named as a text tool's
    argument (`grep gh "$f"`). A first word gh does not know as a command is
    not judged: gh would refuse it, or run an alias or extension from its own
    config (C-12.4's stated limit), so `grep gh file` and `echo gh` pass.
    """
    if fed or (unsettled and _gh_unsettled(args)) or _gh_reaches(args):
        return _gh_verdict(args, fed)
    return None


def _gh_verdict(args: Sequence[str], fed: bool = False) -> Refusal | None:
    """`gh <args>`, wherever the rule finds the call: refused unless a known
    read (fail closed), with the credential wording when it would print the
    token. A call whose arguments come from `xargs` or `parallel` (`fed`) is no
    read: its words are not in the text (#221 review r4, P2-1). The refusal
    names the command and subcommand gh would run."""
    if not fed and gh_read(args):
        return None
    path, _ = _gh_path(args)
    words = " ".join(["gh", *(SHOWN.get(word, word) for word in path or args[:2])])
    if gh_credential(args):
        return _refusal(words, "credential")
    if fed:
        return _refusal(f"{words}, its arguments from xargs or parallel")
    if _gh_unsettled(args):
        return _refusal(f"{words}, with a word known only at run time before its verb")
    return _refusal(words)


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
    # In lower case: macOS's default volume is case-insensitive, so the shell runs gh for `GH`.
    return (PurePosixPath(word.lstrip("=")).name or word).lower() if word else ""


def _names_gh_binary(word: str) -> bool:
    """A word that names gh, in any case: `gh`, `…/gh` (or `…/gh/`), `!gh`, `=gh`, `GH`."""
    bare = word.lstrip("!").lstrip("=").rstrip("/").lower()
    return bare == "gh" or bare.endswith("/gh")


def _names_gh(text: str) -> bool:
    """Whether the text names gh anywhere, quotes and backslashes removed, and
    env's `\\_` read as the word break it is in an `env -S` value."""
    return bool(GH_WORD.search(re.sub(r"[\\'\"]", "", text.replace("\\_", " "))))


def _names_gh_value(text: str) -> bool:
    """Whether text the shell expands may produce the word gh: it names gh
    other than as the command of a substitution (`$(command -v gh)` and `echo
    gh` may; `$(gh pr list -q …)` prints gh's output, not gh)."""
    return _names_gh(GH_CALL.sub("", text))


def _gh_variables(text: str) -> frozenset[str]:
    """The variables the command text sets to a value naming gh, or loops over a list naming gh."""
    return frozenset({name for name, value in SETS_VARIABLE.findall(text) if _names_gh_value(value)}
                     | {name for name, items in FOR_VARIABLE.findall(text) if _names_gh_value(items)})


def _braces(word: str, limit: int = 64) -> list[str]:
    """`word` with its brace expansions expanded, innermost first; a range (`{a..z}`) stands for any text."""
    found = re.search(r"\{([^{}]*)\}", word)
    if not found or limit <= 1:
        return [word]
    if "," in found[1]:
        options = found[1].split(",")
    elif ".." in found[1]:
        options = ["*"]
    else:
        return [word]
    expanded: list[str] = []
    for option in options:
        expanded += _braces(word[:found.start()] + option + word[found.end():], limit // len(options))
    return expanded


def _could_be_gh(word: str) -> bool:
    """Whether a run-time word may expand to gh (#221 review r4, P2-1): it names
    gh itself (`"$GH"`, `${GH:-gh}`, `$dir/gh`, a substitution whose text names
    gh), it reads a variable the command text sets to gh (`g=gh; … $g`), or it
    is a glob or brace word whose last part matches gh and spells g or h
    (`g?`, `{g,}h`, `/opt/homebrew/bin/g[h]`), in any case."""
    if word == SUBSTITUTED_GH or _names_gh(word):
        return True
    if _GH_VARIABLES.get().intersection(READS_VARIABLE.findall(word)):
        return True
    if word in TEST_WORDS or not GLOB.search(word):
        return False
    for expansion in _braces(word):
        last = expansion.rstrip("/").rpartition("/")[2].lower()
        if ("g" in last or "h" in last) and fnmatch.fnmatchcase("gh", last):
            return True
    return False


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
#: shell can expand (`_dynamic`; `SUBSTITUTED_GH` when its text names gh, so
#: `$(command -v gh)` may be gh, `_could_be_gh`), or the file a process
#: substitution names.
SUBSTITUTED = "$__subfleet_substitution__"
SUBSTITUTED_GH = "$__subfleet_gh_substitution__"
PROCESS_FILE = "/dev/fd/63"
#: How a refusal names those words.
SHOWN = {SUBSTITUTED: "$(…)", SUBSTITUTED_GH: "$(…)", PROCESS_FILE: "<(…)"}


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
            body = text[index + 2:end]
            (processes if process else commands).append(body)
            out.append(PROCESS_FILE if process else
                       SUBSTITUTED_GH if _names_gh_value(f"$({body})") else SUBSTITUTED)
            index = end + 1
            continue
        if char == "`":
            end = index + 1
            while end < len(text) and text[end] != "`":
                end += 2 if text[end] == "\\" else 1
            if end >= len(text):
                return None
            body = text[index + 1:end]
            commands.append(body)
            out.append(SUBSTITUTED_GH if _names_gh_value(f"$({body})") else SUBSTITUTED)
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
    return bool(re.search(r"(^|[^\w.-])gh\s", text, re.IGNORECASE)
                or re.search(r"\bgit\b[^;&|\n]*\bpush\b", text, re.IGNORECASE))


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


def _env(words: list[str], index: int) -> tuple[list[str], int, Refusal | None]:
    """`env`'s words from `index`: the command's index, with any `-S` value
    split into words in its place (`env -S 'gh pr comment 7'`), and a refusal
    for an `-S` value the rule can't split.

    The options are read as env's getopt reads them (#221 review r4, P2-2): a
    cluster of letters, where a value letter (`ENV_SHORT_VALUES`) takes the
    rest of its word or the next word, so `-vS 'gh …'`, `-iS'gh …'` and
    `-uVAR` count, and GNU env's long options by any unambiguous prefix. The
    words an `-S` value splits into are env's own arguments again, options
    included. An `-S` value holding a backslash or `${` is refused: there env
    applies its own escapes (macOS env: `\\_` separates words, `\\c` ends the
    string) and substitutes `${NAME}` from its own environment, which this
    rule does not model.
    """
    while index < len(words):
        word = words[index]
        if word == "--":
            return words, index + 1, None
        if ASSIGNMENT.fullmatch(word):
            index += 1
            continue
        split = None
        after = index + 1
        if word.startswith("--"):
            name, equals, value = word.partition("=")
            options = [option for option in ENV_LONG_VALUES if len(name) > 2 and option.startswith(name)]
            if len(options) == 1:
                if not equals:
                    value, after = (words[index + 1] if index + 1 < len(words) else ""), index + 2
                if options[0] == "--split-string":
                    split = value
        elif word.startswith("-") and len(word) > 1:
            for at, letter in enumerate(word[1:], 1):
                if letter in ENV_SHORT_VALUES:
                    value = word[at + 1:]
                    if not value:
                        value, after = (words[index + 1] if index + 1 < len(words) else ""), index + 2
                    if letter == "S":
                        split = value
                    break
        else:
            break
        if split is None:
            index = after
            continue
        if "\\" in split or "${" in split:
            return words, index, _refusal("env -S with an escape or ${…}")
        words = words[:index] + _split(split) + words[after:]
    return words, index, None


def _shell_script(args: list[str], shell: str = "") -> tuple[list[str], bool, list[str]]:
    """(the `-c` scripts, whether it may read standard input as its script, the
    script operands) for `<shell> <args>`, over every reading of its options.

    `c` counts anywhere in a cluster (`-ec`, `-fc`, `-cf`), and so does `s`.
    A cluster's `o` or `O` takes the next word as its value in bash and sh
    (`-euo pipefail -c …`), but this host's ksh ran `ksh -oc 'cmd'` and zsh's
    `-O` takes none, so both readings are judged (#221 review r4, P2-2). The C
    shells have no `o`, and their `-b` ends the options. fish isn't on this
    host: any of its option words holding `c` or `C` is read as carrying a
    script, in the rest of the word or the next word, as is a long option's
    value.
    """
    scripts: list[str] = []
    operands: list[str] = []
    reads_stdin = False

    def walk(index: int, command_string: bool, stdin_flag: bool, branches: int) -> None:
        nonlocal reads_stdin
        while index < len(args):
            arg = args[index]
            if arg == "--":
                index += 1
                break
            if shell == "fish" and arg.startswith("-") and len(arg) > 1:
                name, equals, value = arg.partition("=")
                if arg.startswith("--"):
                    if equals:
                        scripts.append(value)
                    elif index + 1 < len(args):
                        scripts.append(args[index + 1])
                    index += 1
                    continue
                letter = next((at for at, char in enumerate(arg[1:], 1) if char in "cC"), None)
                if letter is not None:
                    scripts.extend(text for text in (arg[letter + 1:], *args[index + 1:index + 2]) if text)
                    command_string = command_string or "c" in arg[1:]
                index += 1
                continue
            if arg in ("--rcfile", "--init-file", "--emulate"):
                index += 2
                continue
            if arg.startswith(("-", "+")) and len(arg) > 1 and not any(char.isspace() for char in arg):
                letters = "" if arg.startswith("--") else arg[1:]
                command_string = command_string or "c" in letters
                stdin_flag = stdin_flag or (arg.startswith("-") and "s" in letters)
                if shell in C_SHELLS and "b" in letters:
                    index += 1
                    break
                values = 0 if shell in C_SHELLS else sum(letter in "oO" for letter in letters)
                if values and branches < 4:
                    walk(index + 1, command_string, stdin_flag, branches + 1)
                index += 1 + values
                continue
            break
        rest = args[index:]
        if command_string:
            if shell != "fish":
                scripts.append(rest[0] if rest else "")
            return
        operand = rest[0] if rest else None
        reads_stdin = reads_stdin or stdin_flag or operand is None or operand in STDIN_FILES
        if operand is not None and not stdin_flag:
            operands.append(operand)

    walk(0, False, False, 0)
    return scripts, reads_stdin, operands


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
    reads_stdin = not template and cut is None
    texts = [" ".join(template)] if template else []
    texts += [word for word in (words[cut + 1:] if cut is not None else []) if word not in PARALLEL_ARGUMENTS]
    for at, text in enumerate(texts):
        # The template's arguments come from its `:::` lists, or from standard input (#221 review r4).
        found = command_refusal(text, depth + 1, names_gh, fed=at == 0 and bool(template))
        if found:
            return found, False
    return None, reads_stdin


def _runtime(word: str, rest: list[str], names_gh: bool, *, fed: bool = False,
             may_be_gh: bool = True) -> Refusal | None:
    """A command word only known at run time (#221 review r2, P2).

    Followed by a gh read, or by words that reach a gh command whatever flags
    come first (`$(command -v gh) -R o/r pr comment`, `{g,}h pr new`), it is
    judged as gh would be (`gh_writes`, #221 review r3c); followed by a push,
    as git. A word that may be gh (`_could_be_gh`, or only parameters while
    the text names gh) is judged as gh is when gh is spelled: refused with a
    word known only at run time before gh's verb is settled (`{g,}h $v
    comment`), or with arguments from xargs (#221 review r4, P2-1);
    `may_be_gh=False` leaves that out, for a text tool's arguments. A word that
    is only parameters (`$c`, `${=c}`, `"$@"`) may carry the whole command, so
    it is refused when the command text names gh anywhere.
    """
    gh = may_be_gh and (_could_be_gh(word) or (names_gh and bool(PARAMETERS.fullmatch(word))))
    fed = fed and gh
    if not fed and gh_read(rest):
        return None
    if fed or (gh and _gh_unsettled(rest)) or _gh_reaches(rest):
        return _gh_verdict(rest, fed)
    if rest and git_publishes(rest):
        return _refusal("git push")
    if names_gh and PARAMETERS.fullmatch(word):
        return _refusal("gh through a run-time command")
    return None


def _fallback(words: list[str], command: str, depth: int, names_gh: bool, *, fed: bool = False,
              nested: bool = False) -> tuple[Refusal | None, set[str]]:
    """The words of a command the rule could not otherwise judge (#221 review
    r2, P2 item 2), and what they read as a script: every word naming gh
    starts a gh call, judged as the direct path judges gh once it reaches a gh
    command (`gh_writes`, r3c), or once a run-time word comes before its verb
    is settled or its arguments come from xargs (`fed`, r4 P2-1), as does a
    word naming git (`git_publishes`); a word naming a shell, `env` or `xargs`
    starts a command `_judge` judges (`arch -arm64 bash -ec '…'`, `… | arch
    -arm64 tcsh`, r4 P2-2), unless this is such a command already (`nested`),
    gh's own argument or an inspector's (`which bash`, `INSPECTORS`);
    assignment values and the values of `-c`, `-e` and
    `--exec` are read as shell text. The text tools' arguments
    (`NOT_SHELL_VALUES`) get none of r4's additions, so `grep gh "$f"` passes."""
    shell_values = command not in NOT_SHELL_VALUES and not command.startswith("python")
    runs_words = shell_values and not nested and command not in INSPECTORS and command != "gh"
    feeds: set[str] = set()
    for at, word in enumerate(words):
        bare = word.lstrip("!").lstrip("=").lower()
        found = None
        if bare == "gh" or bare.endswith("/gh"):
            found = gh_writes(words[at + 1:], fed=fed and shell_values, unsettled=shell_values)
        elif (bare == "git" or bare.endswith("/git")) and git_publishes(words[at + 1:]):
            found = _refusal("git push")
        elif _dynamic(word):
            # `arch -arm64 $(command -v gh) pr comment 7`: a run-time word behind
            # a runner the rule doesn't know, judged as a run-time command word.
            found = _runtime(word, words[at + 1:], False, fed=fed, may_be_gh=shell_values)
        elif runs_words and at and _name(word) in NESTED_RUNNERS:
            found, inner = _judge(words[at:], depth + 1, names_gh, fed=fed, nested=True)
            feeds |= inner
        assignment = ASSIGNMENT.fullmatch(word)
        if found is None and assignment:
            found = _value_refusal(assignment[2], depth, names_gh)
        if found is None and shell_values:
            if word in COMMAND_VALUE_FLAGS and at + 1 < len(words):
                found = _value_refusal(words[at + 1], depth, names_gh)
            elif word.startswith("--exec="):
                found = _value_refusal(word[len("--exec="):], depth, names_gh)
        if found:
            return found, feeds
    return None, feeds


def _judge(words: list[str], depth: int, names_gh: bool = False, *, fed: bool = False,
           nested: bool = False) -> tuple[Refusal | None, set[str]]:
    """(refusal, what this command reads as a script: `stdin`, `process`) for
    one simple command. `fed`: its arguments come from xargs or parallel."""
    words, herestrings, stdin_process, writes_file = _redirections(words)
    index = 0
    via_xargs = fed
    while index < len(words):
        word = words[index]
        name = _name(word)
        if ASSIGNMENT.fullmatch(word) or name in KEYWORDS:
            index += 1
        elif name == "command" and index + 1 < len(words) and words[index + 1] in ("-v", "-V"):
            return None, set()          # a lookup, not a run
        elif name == "env":
            words, index, refused = _env(words, index + 1)
            if refused:
                return refused, set()
        elif name in WRAPPERS:
            via_xargs = via_xargs or name == "xargs"
            index = _skip_wrapper(name, words, index + 1)
        else:
            break
    if index >= len(words):
        return _fallback(words, "", depth, names_gh, fed=via_xargs, nested=nested)
    word, rest = words[index], words[index + 1:]
    name = _name(word)
    reason: Refusal | None = None
    feeds: set[str] = set()
    if via_xargs and (name in SHELLS or name == "eval"):
        feeds.add("stdin")              # `xargs -I{} sh -c '{}'`: the input becomes the script
    if name in SHELLS or name in SOURCES:
        scripts, reads_stdin, operands = (_shell_script(rest, name) if name in SHELLS else
                                          ([], bool(rest) and rest[0] in STDIN_FILES, rest[:1]))
        for script in scripts:          # run by xargs, its words may come from xargs too (`"$@"`)
            reason = reason or command_refusal(script, depth + 1, names_gh, fed=via_xargs)
        if PROCESS_FILE in operands:
            feeds.add("process")        # `bash <(echo …)`, `source <(…)`
        if reads_stdin:
            feeds.add("stdin")
    elif name == "eval":
        reason = command_refusal(" ".join(rest), depth + 1, names_gh, fed=via_xargs)
    elif name == "gh":
        reason = _gh_verdict(rest, via_xargs)
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
        reason = _runtime(word, rest, names_gh, fed=via_xargs)
    found, inner = _fallback(words, name, depth, names_gh, fed=via_xargs, nested=nested)
    reason = reason or found
    feeds |= inner
    if "stdin" in feeds:
        if stdin_process:
            feeds.add("process")        # `bash < <(echo …)`
        for text in herestrings:        # `bash <<< '…'`
            reason = reason or command_refusal(text, depth + 1, names_gh)
    return reason, feeds


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
                    names_gh: bool | None = None, *, fed: bool = False) -> Refusal | None:
    """What a shell command would publish (`gh pr comment`, `git push`), or None.

    A list is an argument vector, run with no shell; a string is shell text.
    `names_gh` is whether the whole command, as first given, names gh; the
    outermost call also notes the variables the text sets to gh
    (`_GH_VARIABLES`). `fed`: GNU parallel gives the command arguments.
    """
    if names_gh is None:
        text = command if isinstance(command, str) else " ".join(map(str, command))
        token = _GH_VARIABLES.set(_gh_variables(text))
        try:
            return command_refusal(command, depth, _names_gh(text), fed=fed)
        finally:
            _GH_VARIABLES.reset(token)
    if not isinstance(command, str):
        reason, _ = _judge([str(word) for word in command], depth, names_gh, fed=fed)
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
        reason, feeds = _judge(words, depth, names_gh, fed=fed)
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
