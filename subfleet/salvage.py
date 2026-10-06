"""Private git snapshots which leave HEAD, the index and worktree alone."""

from __future__ import annotations

import errno
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .adapters.base import AdapterError
from .contracts import GIT_LOCATION_ENV, GIT_PATHSPEC_ENV
from .sessions.transcripts import open_regular

#: One git call's wall-clock cap when the caller names none. The 15 s this
#: replaced failed jobs whose repository was healthy: on 2026-09-20, under a
#: load average near 10, a snapshot that takes 0.6 s on an idle machine ran past
#: it. `caps.workspace_git_timeout_s` is the daemon's setting; this variable is
#: for callers that have no policy in hand.
DEFAULT_GIT_TIMEOUT_S = 60
GIT_TIMEOUT_ENV = "SUBFLEET_GIT_TIMEOUT_S"

#: `OSError`s that describe the machine at this moment, not the repository: the
#: same call is expected to succeed once the pressure passes.
TRANSIENT_ERRNOS = frozenset({
    errno.EAGAIN, errno.EINTR, errno.ENOMEM, errno.EMFILE, errno.ENFILE,
    errno.EBUSY, errno.ETIMEDOUT, errno.ENOBUFS, errno.ENOSPC,
})

#: git reports the same machine pressure as `TRANSIENT_ERRNOS` through
#: `strerror`, and sometimes omits the errno: an index or ref lock file it
#: could not finish writing, an allocation failure, or a held reftable lock.
#: These all get bounded retries (C-6.8, C-13.1). `cannot lock ref` alone also
#: covers a ref that exists or a name that conflicts, which no retry changes.
#: Read only under the C locale (`_git_env`), on git's own `error:` and `fatal:`
#: lines, and at a line's end, so a file name quoted earlier in a line or
#: ending a `hint:` or `warning:` line is not read as an error. `strerror`
#: follows the host's wording, as git does (for example, EBUSY differs on
#: Darwin and Linux); Python leaves LC_MESSAGES in the C locale.
#: A reftable ref write that failed on a full disk names no errno either
#: (`reftable: transaction failure: I/O error`), where the files backend's says
#: `couldn't write '….lock'` (adversarial review of the round-3 branch).
_TRANSIENT_STRERRORS = "|".join(re.escape(os.strerror(code)) for code in sorted(TRANSIENT_ERRNOS))
_TRANSIENT_GIT = re.compile(
    r"^(?:error|fatal): (?:[^\n]*(?:Unable to create '[^\n]*\.lock': File exists\.?"
    rf"|: (?:{_TRANSIENT_STRERRORS})|write error\. Out of diskspace"
    r"|couldn't write '[^\n]*\.lock'|cannot lock references"
    r"|reftable: transaction (?:failure|prepare): I/O error)"
    r"|unable to write new index file|Out of memory, [^\n]*)$", re.M)



class SalvageError(RuntimeError):
    """A snapshot failed; callers must retain the workspace for reconciliation.

    ``transient`` is true when the failure says nothing about the repository (a
    timeout, an `OSError` in `TRANSIENT_ERRNOS`, or git naming a held lock file
    or a full disk, `_TRANSIENT_GIT`), so the caller may retry. ``timed_out``
    is true for a git call stopped at its cap, which is transient too: it did
    not finish, and a snapshot's seeding step that did not is no reason to
    read the baseline the slow way (`snapshot_tree`).

    The message is valid UTF-8 (`utf8_text`) whatever git quoted in it: it
    reaches receipts, the evidence, notices and replies.
    """

    def __init__(self, message: str, *, transient: bool = False, timed_out: bool = False):
        super().__init__(utf8_text(message))
        self.transient = transient or timed_out
        self.timed_out = timed_out


def utf8_text(text: str) -> str:
    """`text` as valid UTF-8, for a receipt, the evidence, a notice or a reply.

    A file or branch name that is not UTF-8, in what git prints (read with
    `os.fsdecode`), is carried as surrogates, which `json_bytes` and SQLite
    cannot encode: a salvage error that quoted one made its receipt unwritable,
    so finalization raised on every try and the attempt held its lane (review of
    cda4c161, N1). (An `OSError`'s own text is safe: it quotes a file name with
    `repr`.) Such bytes become `\\xNN`, any other
    lone surrogate `\\udNNN`; everything else, line breaks included, is kept.
    """
    return _SURROGATE.sub(_escape_surrogate, text)


_SURROGATE = re.compile("[\ud800-\udfff]")


def _escape_surrogate(match: re.Match) -> str:
    code = ord(match[0])
    return f"\\x{code - 0xDC00:02x}" if 0xDC80 <= code <= 0xDCFF else f"\\u{code:04x}"


def git_timeout_s(timeout_s: float | None = None) -> float:
    """The explicit cap, else `SUBFLEET_GIT_TIMEOUT_S`, else the default."""
    if timeout_s is not None:
        return timeout_s
    try:
        value = float(os.environ.get(GIT_TIMEOUT_ENV, ""))
    except ValueError:
        return DEFAULT_GIT_TIMEOUT_S
    return value if value > 0 else DEFAULT_GIT_TIMEOUT_S


def transient_os_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in TRANSIENT_ERRNOS


def _transient_git(stderr: str | bytes) -> bool:
    """Whether git's stderr names a failure another try may not meet (`_TRANSIENT_GIT`)."""
    return bool(_TRANSIENT_GIT.search(os.fsdecode(stderr) if isinstance(stderr, bytes) else stderr))


def _git_env(env: dict[str, str] | None) -> dict[str, str]:
    """`env` (the daemon's own when None) with git's messages untranslated, no
    repository named by the environment (`GIT_LOCATION_ENV`; the daemon's own
    `GIT_INDEX_FILE` too, where a caller's `env` names a temporary index), and
    pathspecs read as each one's magic says (`GIT_PATHSPEC_ENV`: under
    `GIT_LITERAL_PATHSPECS` the exclusion `_add_all` relies on was a file name).

    What git prints is read (`_transient_git`), and under another locale git
    translates its messages and even its `error:` and `fatal:` prefixes (review
    of c1f95838: under `de_DE.UTF-8` a nested repository with no commit was
    reported in German and not recognised). `LANGUAGE` is removed too, since
    gettext reads it before `LC_ALL` everywhere but under the C locale.
    """
    own = env is None
    env = dict(os.environ if own else env)
    for name in GIT_LOCATION_ENV + GIT_PATHSPEC_ENV + (("GIT_INDEX_FILE",) if own else ()):
        env.pop(name, None)
    env["LC_ALL"] = "C"
    env.pop("LANGUAGE", None)
    return env


def _failure(verb: str, result: subprocess.CompletedProcess) -> SalvageError | None:
    """A finished git call's failure, or None when it exited 0.

    A git killed by a signal (a negative return code: the kernel's memory-pressure
    kill, or a signal sent to the daemon's process group) did not finish, so it
    is transient, as a timeout is, and never an answer: read as "no HEAD" it
    let a writable job past the main/master refusal (C-6.8, C-13.2; adversarial
    review of the round-3 branch). Otherwise git's stderr decides (`_transient_git`).
    """
    if not result.returncode:
        return None
    if result.returncode < 0:
        try:
            name = signal.Signals(-result.returncode).name
        except ValueError:
            name = f"signal {-result.returncode}"
        return SalvageError(f"git {verb} was killed by {name}", transient=True)
    return SalvageError(f"git {verb} failed: {os.fsdecode(result.stderr).strip()}",
                        transient=_transient_git(result.stderr))


def _git(workdir: str | Path, *args: str, env: dict[str, str] | None = None,
         optional: bool = False, timeout_s: float | None = None) -> str | None:
    """One capped git call's stdout, stripped. Both streams are read as the file
    system names things (`os.fsdecode`), never as strict UTF-8: a path or ref name
    that is not UTF-8 is carried, where `text=True` raised `UnicodeDecodeError`,
    which no caller catches (review of cda4c161, N1)."""
    cap = git_timeout_s(timeout_s)
    try:
        result = subprocess.run(["git", "-C", str(workdir), *args], env=_git_env(env),
                                capture_output=True, timeout=cap)
    except subprocess.TimeoutExpired as exc:
        # Never `optional`: a call that did not finish has not said "no HEAD" or
        # "no branch", and reading it that way admits a writable job with no
        # baseline or lets one past the main/master refusal.
        raise SalvageError(f"git {args[0]} timed out after {cap:g} s", timed_out=True) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        if transient_os_error(exc):
            raise SalvageError(f"git {args[0]} could not run: {exc}", transient=True) from exc
        if optional:
            return None
        raise SalvageError(f"git {args[0]} unavailable: {type(exc).__name__}: {exc}") from exc
    failure = _failure(args[0], result)
    if failure is None:
        return os.fsdecode(result.stdout).strip()
    # An optional lookup may answer "absent", but host pressure or a git that did
    # not finish establishes nothing about HEAD or the checkout, as a timeout does not.
    if optional and not failure.transient:
        return None
    raise failure


def _git_bytes(workdir: str | Path, *args: str, env: dict[str, str] | None = None,
               stdin: bytes | None = None, timeout_s: float | None = None) -> bytes | None:
    """``_git`` for byte streams (paths need not be UTF-8); None on a non-zero
    exit. Transient git failures, timeouts and OS errors raise as in ``_git``."""
    cap = git_timeout_s(timeout_s)
    try:
        result = subprocess.run(["git", "-C", str(workdir), *args], env=_git_env(env), input=stdin,
                                capture_output=True, timeout=cap)
    except subprocess.TimeoutExpired as exc:
        raise SalvageError(f"git {args[0]} timed out after {cap:g} s", timed_out=True) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        if transient_os_error(exc):
            raise SalvageError(f"git {args[0]} could not run: {exc}", transient=True) from exc
        return None
    failure = _failure(args[0], result)
    if failure is None:
        return result.stdout
    if failure.transient:
        raise failure
    return None


def git_head(workdir: str | Path, *, timeout_s: float | None = None) -> str | None:
    return _git(workdir, "rev-parse", "--verify", "HEAD", optional=True, timeout_s=timeout_s)


def git_branch(workdir: str | Path, *, timeout_s: float | None = None) -> str | None:
    return _git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD", optional=True,
                timeout_s=timeout_s)


def git_toplevel(workdir: str | Path, *, timeout_s: float | None = None) -> str | None:
    """C-6.5: the real path of the worktree that holds ``workdir``, or None outside one.

    Two directories of one checkout (`/repo` and `/repo/sub`) are one place to
    write; two linked worktrees of one repository are two.
    """
    # Only git's line end is removed: a directory whose name ends in a space is
    # still that directory (review of 5aa2718).
    raw = _git_bytes(workdir, "rev-parse", "--show-toplevel", timeout_s=timeout_s)
    top = os.fsdecode(raw[:-1] if raw and raw.endswith(b"\n") else raw or b"")
    return os.path.realpath(top) if top else None


def git_tree(workdir: str | Path, commit: str, *, timeout_s: float | None = None) -> str | None:
    """The tree of ``commit``, or None when the repository cannot name it."""
    return _git(workdir, "rev-parse", "--verify", f"{commit}^{{tree}}", optional=True,
                timeout_s=timeout_s)


def working_tree(workdir: str | Path, baseline_commit: str, *,
                 timeout_s: float | None = None, left_out: list[str] | None = None) -> str:
    """The tree of `snapshot_tree`; `left_out`, when given, receives the paths it
    left out (`_add_all`), as `salvage`'s does."""
    tree, skipped = snapshot_tree(workdir, baseline_commit, timeout_s=timeout_s)
    if left_out is not None:
        left_out.extend(skipped)
    return tree


def snapshot_tree(workdir: str | Path, baseline_commit: str, *,
                  timeout_s: float | None = None) -> tuple[str, tuple[str, ...]]:
    """Snapshot tracked and untracked files without changing the real index.

    The temporary index holds ``baseline_commit`` before ``add -A`` records
    the worktree over it. It is seeded from a copy of the real index and
    read with ``read-tree -m``: the entries are the baseline's either way,
    but git keeps the real index's stat data for every path whose content
    matches, so ``add -A`` hashes only the files that changed. Read into an
    empty index, the baseline has no stat data and every tracked file is
    hashed again, which on a large checkout outlasts the preparation cap
    (C-6.8). The copy's assume-unchanged and skip-worktree bits, which
    ``read-tree -m`` keeps, are cleared before ``add -A``: it skips such paths, and the empty-index read has no such
    bits, so the two reads give the same tree. When the real index cannot
    seed it (there is none yet, it has unmerged entries, git cannot read the
    copy or clear its bits), the baseline is read into an empty index as
    before, in a temporary directory of its own.

    Any failure of a seeding step means "no seed", whatever it was: the
    empty-index read gives the same tree, and the seed only saves time. A
    transient one included, which a snapshot's own failure is not: git 2.55
    is killed by SIGSEGV reading an index whose entries are garbage (`DIRC`,
    version 2, then junk), and that crash, transient as any git killed by a
    signal is (`_failure`), failed every try of the snapshot the same way,
    salvage's and admission's alike, the copy's `index.lock` left beside it
    for the fallback to trip on (review of ceacf18b, P3-1). A seeding step
    that reached its cap is the exception: it did not finish, and raises as
    before (transient). Under the load that stops the fast seeded read, the
    unseeded one, which hashes every tracked file, is slower still (C-6.8's
    incident: 0.5 s seeded, 39 to 51 s and then past the cap unseeded), so
    reading it would only spend a second cap on each try before the same
    failure (review of the P3-1 fix).

    Trusting stat data is git's own model (``git status`` does the same): a
    file rewritten with the same size, mtime and inode is read as unchanged,
    where the empty-index read would hash it.

    Returns the tree and the nested repositories with no commit that it leaves
    out (`_add_all`).
    """
    gitdir = _git(workdir, "rev-parse", "--absolute-git-dir", timeout_s=timeout_s)
    with tempfile.TemporaryDirectory(prefix="subfleet-salvage-", dir=gitdir) as temporary:
        index = Path(temporary) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        if _seeded(workdir, index, env, baseline_commit, timeout_s=timeout_s):
            return _record(workdir, env, timeout_s=timeout_s)
    # A fresh directory: whatever a failed seeding step left (the copy, a
    # crashed git's `index.lock`) is gone with the first.
    with tempfile.TemporaryDirectory(prefix="subfleet-salvage-", dir=gitdir) as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        _git(workdir, "read-tree", baseline_commit, env=env, timeout_s=timeout_s)
        return _record(workdir, env, timeout_s=timeout_s)


def _seeded(workdir: str | Path, index: Path, env: dict[str, str], baseline_commit: str, *,
            timeout_s: float | None = None) -> bool:
    """Whether `index` now holds `baseline_commit` seeded from the real index, its
    skip bits cleared (`snapshot_tree`); False when any step did not, failures
    and git killed by a signal included. A step stopped at its cap raises."""
    try:
        return bool(_seed_index(workdir, index, timeout_s=timeout_s)
                    and _git(workdir, "read-tree", "-m", baseline_commit, env=env,
                             optional=True, timeout_s=timeout_s) is not None
                    and _clear_skip_bits(workdir, env, timeout_s=timeout_s))
    except SalvageError as exc:
        if exc.timed_out:
            raise
        return False


def _record(workdir: str | Path, env: dict[str, str], *,
            timeout_s: float | None = None) -> tuple[str, tuple[str, ...]]:
    """`add -A` over the baseline in `env`'s temporary index, then its tree (`snapshot_tree`)."""
    skipped = _add_all(workdir, env, timeout_s=timeout_s)
    return _git(workdir, "write-tree", env=env, timeout_s=timeout_s), skipped


def _add_all(workdir: str | Path, env: dict[str, str], *,
             timeout_s: float | None = None) -> tuple[str, ...]:
    """`add -A` into the temporary index, leaving out nested repositories with no commit.

    git cannot index a nested repository with no commit checked out (a gitlink
    names a commit, and there is none), and `add -A` then fails the whole
    snapshot, and every retry in the same way: on 2026-09-27 two finished
    attempts held their Codex lanes for hours in `finalizing`, their salvage
    failing on a test fixture's empty repository under an untracked scratch
    directory. So when `add -A` fails, the untracked nested repositories are
    listed (`ls-files -o` names each as `path/`, inside an untracked directory
    too, and honours `.gitignore` as `add -A` does), each whose HEAD does not
    resolve is excluded by a literal pathspec, and `add -A` runs once more.

    Nothing git prints decides what is left out, and any other failure fails
    the snapshot as before, including one beside such a repository: an
    unreadable file, or an object git could not write (a read-only
    `.git/objects/xx`, a full disk), which `--ignore-errors` had let through
    as a path left out (review of c1f95838). The listing runs only after a
    failure: it walks the untracked tree again, 0.1 to 1.1 s on large checkouts
    (2026-09-27), and a snapshot that succeeds needs none.

    Returns the excluded paths relative to the top level, as `ls-files` names
    them, decoded as the file system names them (`os.fsdecode`), so a name that
    is not UTF-8 is carried, not a decoding error.
    """
    failure = _add(workdir, env, None, timeout_s)
    if failure is None:
        return ()
    excluded = _uncommitted_repositories(workdir, env, timeout_s=timeout_s)
    if not excluded:
        raise failure
    spec = b"".join(b":(top,exclude,literal)" + path + b"\0" for path in excluded)
    failure = _add(workdir, env, b":/\0" + spec, timeout_s)
    if failure is not None:
        raise failure
    return tuple(os.fsdecode(path) for path in excluded)


def _add(workdir: str | Path, env: dict[str, str], pathspec: bytes | None,
         timeout_s: float | None) -> SalvageError | None:
    """One `add -A` (limited to NUL-separated `pathspec` when given): None when it
    succeeds, else the error to raise. A timeout, or a git killed by a signal,
    raises at once, as a transient one."""
    cap = git_timeout_s(timeout_s)
    limit = ("--pathspec-from-file=-", "--pathspec-file-nul") if pathspec is not None else ()
    try:
        result = subprocess.run(["git", "-C", str(workdir), "add", "-A", *limit], env=_git_env(env),
                                input=pathspec, capture_output=True, timeout=cap)
    except subprocess.TimeoutExpired as exc:
        raise SalvageError(f"git add timed out after {cap:g} s", timed_out=True) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise SalvageError(f"git add could not run: {exc}", transient=transient_os_error(exc)) from exc
    failure = _failure("add", result)
    if failure is not None and result.returncode < 0:
        raise failure
    return failure


def _uncommitted_repositories(workdir: str | Path, env: dict[str, str], *,
                              timeout_s: float | None = None) -> list[bytes]:
    """The untracked nested repositories whose HEAD does not resolve, as `ls-files`
    names them (relative to the top level, ending in `/`).

    `ls-files -o` (not `--directory`, which names only an untracked directory's
    top and would hide a repository inside it) lists a nested repository as one
    `path/` entry, by the test `add -A` itself uses, and it reads the temporary
    index, so "untracked" is what it is to `add -A`. It does not list one where
    a tracked file was (`git init x` after `rm x`): the index still names `x`,
    but `add -A` records the file's removal and then refuses `x/` (adversarial
    review of the round-3 branch). So each tracked path `diff-files` finds
    changed that is now a directory holding `.git` is a candidate too; its
    exclusion, `x/`, names only the directory, and the removal is still recorded.
    A repository is excluded only when `rev-parse --verify HEAD` in it exits 1
    (HEAD does not resolve): one git cannot open (exit 128) is left for `add -A`
    to judge.
    """
    top = _git_bytes(workdir, "rev-parse", "--show-toplevel", env=env, timeout_s=timeout_s)
    if not top:
        return []
    top = top[:-1] if top.endswith(b"\n") else top
    listed = _git_bytes(workdir, "ls-files", "-o", "--exclude-standard", "-z", "--full-name", "--", ":/",
                        env=env, timeout_s=timeout_s)
    candidates = [path for path in (listed or b"").split(b"\0") if path.endswith(b"/")]
    # Paths relative to the top level; a submodule's own changes are not asked for.
    changed = _git_bytes(workdir, "diff-files", "--name-only", "-z", "--ignore-submodules=all",
                         env=env, timeout_s=timeout_s)
    candidates += [path + b"/" for path in (changed or b"").split(b"\0")
                   if path and os.path.lexists(os.path.join(top, path, b".git"))]
    # The nested repository's own git directory, never the temporary index.
    nested = {key: value for key, value in env.items() if key != "GIT_INDEX_FILE"}
    return [path for path in dict.fromkeys(candidates)
            if _head_status(os.path.join(top, path, b".git"), nested, timeout_s) == 1]


def _head_status(gitdir: bytes, env: dict[str, str], timeout_s: float | None) -> int:
    """`rev-parse -q --verify HEAD`'s exit status in the repository at `gitdir`:
    0 when HEAD names a commit, 1 when it does not resolve, 128 when git cannot
    open the repository. `--git-dir` never lets git look above `gitdir`."""
    cap = git_timeout_s(timeout_s)
    try:
        result = subprocess.run(["git", b"--git-dir=" + gitdir, "rev-parse", "-q", "--verify", "HEAD"],
                                env=_git_env(env), capture_output=True, timeout=cap)
    except subprocess.TimeoutExpired as exc:
        raise SalvageError(f"git rev-parse timed out after {cap:g} s", timed_out=True) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise SalvageError(f"git rev-parse could not run: {exc}", transient=transient_os_error(exc)) from exc
    failure = _failure("rev-parse", result)
    if failure is not None and failure.transient:
        raise failure
    return result.returncode


def path_text(path: str) -> str:
    """A path left out of a snapshot, as one line of valid UTF-8 for a receipt,
    the evidence or a notice: bytes that are not UTF-8 (`os.fsdecode` carries
    them as surrogates, which `json_bytes` cannot encode, `utf8_text`) and control
    characters (a newline would start a line of its own in a notice) are escaped."""
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in utf8_text(path))


def _seed_index(workdir: str | Path, index: Path, *,
                timeout_s: float | None = None) -> bool:
    """Copy the real index to ``index``; False when there is none to copy.

    The copy keeps the file's mtime: git treats entries at or after the
    index's own mtime as possibly modified and hashes them, and a copy
    stamped now would hide exactly those edits. The bytes and the mtime come
    from one open file, so a concurrent rewrite of the index (git replaces
    it by rename) cannot pair one index's entries with another's mtime.

    The real index is read only as a regular file, and the copy is a new file
    (O_EXCL, O_NOFOLLOW): this runs outside git's time limit, and a FIFO in
    the index's place had held a diff, and with it the file pool and the
    conversation service's close(), until a writer came (review of aa41312).
    Anything but a regular file there is no index to copy.
    """
    real = _git(workdir, "rev-parse", "--git-path", "index", optional=True,
                timeout_s=timeout_s)
    if not real:
        return False
    source = Path(real) if os.path.isabs(real) else Path(workdir) / real
    try:
        out = os.open(index, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with open(out, "wb") as writer, open_regular(source) as reader:
            stat = os.fstat(reader.fileno())
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.utime(writer.fileno(), ns=(stat.st_atime_ns, stat.st_mtime_ns))
    except OSError:
        index.unlink(missing_ok=True)
        return False
    return True


def _clear_skip_bits(workdir: str | Path, env: dict[str, str], *,
                     timeout_s: float | None = None) -> bool:
    """Clear assume-unchanged and skip-worktree bits in the temporary index.

    Flipping a bit hashes nothing. ``update-index`` applies only the first
    of the two options it is given, so each bit gets its own call.
    """
    # From the toplevel: from a subdirectory, ls-files lists only that
    # subdirectory's entries, and add -A still records the whole tree.
    top = _git(workdir, "rev-parse", "--show-toplevel", optional=True, timeout_s=timeout_s)
    if not top:
        return False
    listed = _git_bytes(top, "ls-files", "-v", "-z", env=env, timeout_s=timeout_s)
    if listed is None:
        return False
    assumed: list[bytes] = []
    skipped: list[bytes] = []
    for record in listed.split(b"\0"):
        if len(record) < 3:
            continue
        tag, path = record[:1], record[2:]
        if tag.islower():
            assumed.append(path)
        if tag in (b"S", b"s"):
            skipped.append(path)
    for option, paths in (("--no-assume-unchanged", assumed),
                          ("--no-skip-worktree", skipped)):
        if paths and _git_bytes(top, "update-index", option, "-z", "--stdin",
                                stdin=b"\0".join(paths) + b"\0", env=env,
                                timeout_s=timeout_s) is None:
            return False
    return True


def validate_writable_workdir(workdir: str | Path, *, timeout_s: float | None = None) -> None:
    """Refuse writable admission on main/master while permitting private refs."""
    branch = git_branch(workdir, timeout_s=timeout_s)
    if branch in {"main", "master"}:
        raise AdapterError(f"writable job refused on {branch}", code=7,
                           fix="Check out a task branch before submitting a writable job.")


@dataclass(frozen=True)
class SalvageResult:
    ref: str
    commit: str
    tree: str
    baseline: str
    #: Nested repositories with no commit, left out of the snapshot (`_add_all`).
    skipped: tuple[str, ...] = ()


#: The longest branch name, sanitized, that a salvage ref's name carries (`salvage`).
BRANCH_SLUG_MAX = 200


def _stamp(timestamp: str | datetime | None) -> str:
    if timestamp is None:
        value = datetime.now(UTC)
    elif isinstance(timestamp, str):
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    else:
        value = timestamp
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def salvage(workdir: str | Path, baseline_commit: str, seq: int, *, writable: bool = True,
            state: str = "finalizing", timestamp: str | datetime | None = None,
            baseline_tree: str | None = None,
            timeout_s: float | None = None, left_out: list[str] | None = None) -> SalvageResult | None:
    """C-13.1: commit a differing working tree beneath a private salvage ref.

    ``baseline_commit`` is the HEAD recorded at reservation and always the
    snapshot's parent. ``baseline_tree`` is the reservation's working tree,
    including any pre-existing dirty files; older callers default to HEAD. Supply
    the recorded attempt timestamp to make a finalization replay idempotent.
    Private refs are permitted even when the current branch is main (C-13.2).
    ``timeout_s`` caps each git call (`git_timeout_s`). ``left_out``, when given,
    receives the paths the snapshot left out (`_add_all`) even when no ref is
    written: a worktree whose only change is such a path returns None.
    """
    if not writable:
        return None
    if state not in {"finalizing", "lost", "kill"}:
        raise ValueError("salvage requires finalizing, lost or kill state")
    if seq < 1:
        raise ValueError("attempt sequence must be positive")
    baseline = _git(workdir, "rev-parse", "--verify", f"{baseline_commit}^{{commit}}", timeout_s=timeout_s)
    baseline_tree = _git(workdir, "rev-parse", "--verify", f"{baseline_tree or baseline}^{{tree}}", timeout_s=timeout_s)
    branch = re.sub(r"[^A-Za-z0-9_-]+", "-", git_branch(workdir, timeout_s=timeout_s) or "detached").strip("-") or "detached"
    # One file name under a files-backend ref directory: a branch whose parts each
    # fit made a ref git could never write, on every try (adversarial review of the
    # round-3 branch). The stamp, `_hold`'s `-<tree>` and git's `.lock` fit beside it.
    ref = f"refs/subfleet-salvage/{branch[:BRANCH_SLUG_MAX].rstrip('-')}-{_stamp(timestamp)}-a{seq}"
    # A private temporary directory avoids index-name races and never points
    # git at the user's real index, including in linked worktrees.
    tree, skipped = snapshot_tree(workdir, baseline, timeout_s=timeout_s)
    if left_out is not None:
        left_out.extend(skipped)
    if tree == baseline_tree:
        # Nothing to commit. A nested repository left out still makes the
        # worktree dirty. Retention must preserve its bytes itself before
        # removing the worktree; no salvage ref covers this path (C-13.4).
        return None
    ref, commit = _hold(workdir, ref, tree, baseline, f"subfleet salvage attempt a{seq}", timeout_s=timeout_s)
    return SalvageResult(ref, commit, tree, baseline, skipped)


def pin_baseline(workdir: str | Path, job_id: str, seq: int, tree: str, head: str, *,
                 timeout_s: float | None = None) -> tuple[str, str]:
    """C-13.1: hold attempt `seq`'s start snapshot `tree` (on `head`, its parent) under
    `refs/subfleet-salvage/<job id>-a<seq>-baseline`; the ref and its commit.

    Admission calls it when the job's previous attempt's salvage failed: that
    attempt's work is then only in this snapshot, a tree object no ref holds,
    which `gc` may prune and the new attempt goes on to edit (review of cda4c161,
    N2). The first snapshot after the failed salvage is kept across admission
    passes, even if the caller edits or commits while the retry waits. Later
    snapshots must not replace that evidence or collide with its ref.
    """
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", job_id).strip("-") or "job"
    ref = f"refs/subfleet-salvage/{name}-a{seq}-baseline"
    previous = _git(workdir, "rev-parse", "--verify", f"{ref}^{{commit}}",
                    optional=True, timeout_s=timeout_s)
    if previous:
        return ref, previous
    return _hold(workdir, ref, tree, head,
                 f"subfleet baseline of attempt a{seq}", timeout_s=timeout_s)


def _hold(workdir: str | Path, ref: str, tree: str, parent: str, message: str, *,
          timeout_s: float | None = None) -> tuple[str, str]:
    """Commit `tree` on `parent` beneath `ref`; the ref written and its commit.

    A ref that already holds `tree` on `parent` (a replay) is reused. One that
    holds other bytes is never overwritten: the snapshot goes beside it, under
    `<ref>-<first 12 of the tree>`, and `update-ref` creates only (its old value
    is all zeros), so a ref another writer made meanwhile fails the call."""
    previous = _git(workdir, "rev-parse", "--verify", ref, optional=True, timeout_s=timeout_s)
    if previous:
        previous_tree = _git(workdir, "rev-parse", f"{previous}^{{tree}}", timeout_s=timeout_s)
        previous_parent = _git(workdir, "rev-parse", f"{previous}^", optional=True, timeout_s=timeout_s)
        if previous_tree == tree and previous_parent == parent:
            return ref, previous
        # Never overwrite a previous snapshot with different bytes.
        ref = f"{ref}-{tree[:12]}"
    commit = _git(workdir, "-c", "user.name=subfleet", "-c", "user.email=subfleet@localhost",
                  "commit-tree", tree, "-p", parent, "-m", message, timeout_s=timeout_s)
    existing = _git(workdir, "rev-parse", "--verify", ref, optional=True, timeout_s=timeout_s)
    if existing:
        if (_git(workdir, "rev-parse", f"{existing}^{{tree}}", timeout_s=timeout_s) == tree
                and _git(workdir, "rev-parse", f"{existing}^", timeout_s=timeout_s) == parent):
            return ref, existing
        raise SalvageError("salvage reference already names a different snapshot")
    _git(workdir, "update-ref", ref, commit, "0" * 40, timeout_s=timeout_s)
    return ref, commit
