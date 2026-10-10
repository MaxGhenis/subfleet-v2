"""C-8.5: data-only bundle intake and host-side, non-forcing branch publication.

Never give Git a lane's worktree, gitdir, config, environment, or object store.
Submit reads checkout metadata as data, each file only as a capped regular file
that no link names and no FIFO holds; all Git subprocesses use a fresh bare
repository with only the host's directly configured credential helpers. The
job's bundle is read as bytes through directory handles that never follow a
link, and Git only ever reads the daemon's own copy of it.
"""
from __future__ import annotations

import configparser
import errno
import fnmatch
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from urllib.parse import urlsplit

from . import retention_fs as rfs
from .adapters.base import AdapterError
from .policy import push_settings

TIMEOUT_S = 60
#: Review P3-4: one deadline for every Git call that reads a job's bundle,
#: from `bundle list-heads` to the last tree, however many commits it holds.
VERIFY_DEADLINE_S = 300
#: Entries one streamed `ls-tree` may yield before verification refuses: a few
#: KB of shared subtrees otherwise expand to some 10^12 paths.
TREE_ENTRY_CAP = 1_000_000
SYMLINK_CAP = 10_000              # symlinks in one tree
SYMLINK_TARGET_CAP = 4096         # bytes in one symlink target (PATH_MAX)
OUTPUT_CAP = 64 * 1024 * 1024     # bytes kept from any other Git call
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
GIT = shutil.which("git") or "/usr/bin/git"
#: Review P1-1: the job writes its bundle inside its own working directory, the
#: one place a Codex workspace-write sandbox lets it write. Relative to the job's
#: worktree root (where Codex starts): `mkdir -p .subfleet && git bundle create
#: .subfleet/push.bundle HEAD`.
BUNDLE_PATH = (".subfleet", "push.bundle")
BUNDLE_RELATIVE = "/".join(BUNDLE_PATH)
#: Every directory from the worktree root down is opened without following a link.
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
#: The bundle itself: never a link, and a FIFO cannot hold the open.
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
#: Review r2 P3: the most of each checkout metadata file submit reads.
POINTER_CAP = 64 * 1024           # a gitfile, `commondir`, `HEAD`, a loose ref
CONFIG_CAP = 4 * 1024 * 1024      # `config`, `config.worktree`
PACKED_REFS_CAP = 64 * 1024 * 1024
#: Review P2-2: names Git or a clone could read as something other than a branch.
RESERVED_PREFIXES = ("refs/", "heads/", "remotes/")
SAFE_CONFIG = ("-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
               "-c", "maintenance.auto=false", "-c", "protocol.allow=never",
               "-c", "protocol.file.allow=always", "-c", "protocol.https.allow=always",
               "-c", "protocol.ssh.allow=always")


class PushError(ValueError):
    pass


class Deadline:
    """The time left to every Git call of one verification, together."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.at = time.monotonic() + seconds


def fold(name: str) -> str:
    """A name as a case-insensitive, normalization-insensitive filesystem sees
    it (APFS, and GitHub's ref checks): NFC, casefolded, then NFC again."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def protected_key(branch: str) -> str:
    """How a branch is compared with protected and reserved names and patterns."""
    return fold(branch)


def ownership_key(remote: str) -> str:
    """Review P3-6: one key per repository, however its URL is spelled."""
    return remote_key(remote)


def check_branch(branch: str, settings: dict, default: str | None = None) -> None:
    if (not isinstance(branch, str) or not BRANCH.fullmatch(branch) or ".." in branch
            or "@{" in branch or branch.endswith(".lock") or branch.startswith("/")
            or branch.endswith(("/", ".")) or "//" in branch
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))):
        raise PushError("invalid push branch; use a Git branch name of 1 to 200 allowed characters")
    key = protected_key(branch)
    if key == "head" or key.startswith(RESERVED_PREFIXES):
        raise PushError(f"push branch {branch!r} is reserved: HEAD and names starting refs/, heads/ "
                        "or remotes/ are refused in any case")
    if (key in {"main", "master"} or (default is not None and key == protected_key(default))
            or key.startswith("release/")
            or any(fnmatch.fnmatchcase(key, protected_key(pattern)) for pattern in settings["protected"])):
        raise PushError(f"push branch {branch!r} is protected (branch names compare case-insensitively)")


def check_case_twins(branch: str, existing) -> None:
    """Review P2-2: a fetch into a case-insensitive clone writes a case twin over
    the existing branch's remote-tracking ref, so a new name may not fold onto one."""
    key = fold(branch)
    for name in existing:
        if name != branch and fold(name) == key:
            raise PushError(f"push branch {branch!r} differs only in case or Unicode form from "
                            f"existing remote branch {name!r}")


def remote_key(remote: str) -> str:
    """Only known transports; never ext helpers, URL credentials, or options."""
    if not isinstance(remote, str) or any(c.isspace() or ord(c) < 32 for c in remote):
        raise PushError("origin must be a GitHub HTTPS/SSH URL or an explicitly allowed file URL")
    if remote.startswith("git@github.com:"):
        path = remote[len("git@github.com:"):]
    else:
        url = urlsplit(remote)
        if url.scheme == "file" and not url.netloc and url.path.startswith("/") and not url.query and not url.fragment:
            # Keep the recorded URL byte-for-byte; exact file patterns are for
            # offline tests and must be explicitly enabled by their own policy.
            return remote
        if (url.hostname != "github.com" or url.query or url.fragment or url.port
                or url.password or url.scheme not in {"https", "ssh"}
                or (url.scheme == "https" and url.username)
                or (url.scheme == "ssh" and url.username != "git")):
            raise PushError("origin must be a GitHub HTTPS/SSH URL or an explicitly allowed file URL")
        path = url.path.lstrip("/")
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        raise PushError("origin must name one GitHub owner/repository")
    return "github.com/" + path.lower()


def check_policy(branch: str, remote: str, policy: dict, default: str | None = None) -> dict:
    settings = push_settings(policy)
    if not settings["enabled"]:
        raise PushError("host push is disabled by policy push.enabled")
    check_branch(branch, settings, default)
    key = remote_key(remote)
    if not any(fnmatch.fnmatchcase(key, pattern.lower() if key.startswith("github.com/") else pattern)
               for pattern in settings["allowed_remotes"]):
        raise PushError("origin is not allowed by policy push.allowed_remotes")
    return settings


def _read_metadata(path: Path, cap: int, *, optional: bool = False) -> str | None:
    """One checkout metadata file, as data (review r2 P3): never through a final
    link, never waiting in open() on a FIFO, only a regular file, and never more
    than `cap` bytes (`retention_fs.read_regular`). A FIFO planted at a nested
    `.git/config` had held submit, `_submit_lock` and every submit after it.
    An `optional` file that is absent is None; anything else there refuses."""
    try:
        data = rfs.read_regular(path, cap)
    except (FileNotFoundError, NotADirectoryError):
        if optional:
            return None
        raise PushError(f"checkout metadata {str(path)!r} is missing") from None
    except OSError as exc:
        why = {errno.ELOOP: "is a symlink", errno.EFBIG: f"is larger than {cap} bytes"}.get(
            exc.errno, "is not a regular file")
        raise PushError(f"checkout metadata {str(path)!r} {why}") from None
    return data.decode("utf-8")


def _config(text: str) -> configparser.RawConfigParser:
    # This is a data parser, not `git config`: include/includeIf are never read.
    config = configparser.RawConfigParser(strict=False, interpolation=None, allow_no_value=True)
    config.read_string(text)
    return config


def _git_directories(workdir: Path) -> tuple[Path, Path, Path]:
    """(checkout top, its gitdir, the common gitdir), read as data. `.git` is
    looked at with lstat: a directory, or a gitfile read as one regular file."""
    for top in (workdir, *workdir.parents):
        try:
            info = os.lstat(top / ".git")
        except (FileNotFoundError, NotADirectoryError):
            continue
        break
    else:
        raise PushError("push requires a committed Git checkout with an origin")
    gitdir = top / ".git"
    if stat.S_ISREG(info.st_mode):
        text = _read_metadata(gitdir, POINTER_CAP).strip()
        if not text.startswith("gitdir: "):
            raise PushError("invalid checkout gitdir")
        gitdir = (top / text[8:]).resolve()
    elif not stat.S_ISDIR(info.st_mode):
        raise PushError(f"{str(gitdir)!r} must be a directory or a gitdir file, not a symlink or special file")
    common = gitdir
    pointer = _read_metadata(gitdir / "commondir", POINTER_CAP, optional=True)
    if pointer is not None:
        common = (gitdir / pointer.strip()).resolve()
    return top, gitdir, common


def checkout_metadata(workdir: Path) -> tuple[str, str, str | None, Path]:
    """Host snapshot at submit, supporting linked worktrees and packed refs."""
    top, gitdir, common = _git_directories(workdir)
    config = _config(_read_metadata(common / "config", CONFIG_CAP))
    remote = config.get('remote "origin"', "url", fallback=None)
    if (config.get("extensions", "worktreeconfig", fallback="false") or "true").lower() == "true":
        text = _read_metadata(gitdir / "config.worktree", CONFIG_CAP, optional=True)
        if text is not None:
            remote = _config(text).get('remote "origin"', "url", fallback=remote)
    if remote and remote.startswith('"') and remote.endswith('"'):
        remote = remote[1:-1]
    if not remote:
        raise PushError("push requires a checkout with an origin URL in its local config")

    def read_ref(value: str) -> str:
        for _ in range(8):
            if SHA.fullmatch(value):
                return value
            if not value.startswith("ref: refs/"):
                break
            ref = value[5:]
            if ".." in ref or "\\" in ref:
                break
            loose = next((text for text in (_read_metadata(p / ref, POINTER_CAP, optional=True)
                                            for p in (gitdir, common)) if text is not None), None)
            if loose is not None:
                value = loose.strip()
                continue
            packed = _read_metadata(common / "packed-refs", PACKED_REFS_CAP, optional=True)
            if packed is not None:
                for line in packed.splitlines():
                    if line.endswith(" " + ref) and SHA.fullmatch(line.split(" ", 1)[0]):
                        return line.split(" ", 1)[0]
            break
        raise PushError("push requires a checkout HEAD naming a committed baseline")

    head = _read_metadata(gitdir / "HEAD", POINTER_CAP).strip()
    branch = head.removeprefix("ref: refs/heads/") if head.startswith("ref: refs/heads/") else None
    return remote, read_ref(head), branch, top


def _environment() -> dict[str, str]:
    # Only the host process's identity/credential locations survive. No lane
    # launch environment, GIT_* overrides, askpass, proxy, or Python variables.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "XDG_CONFIG_HOME", "SSH_AUTH_SOCK") if key in os.environ}
    env.update(LC_ALL="C", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    return env


def validate_write_location(workdir: Path) -> None:
    try:
        _, _, current, _ = checkout_metadata(workdir)
        if current in {"main", "master"}:
            raise PushError("writable in-place jobs require a feature branch")
    except (ValueError, OSError, configparser.Error) as exc:
        raise AdapterError(f"workspace refused: {exc}", code=7,
                           fix="choose a committed feature branch") from exc


def _stop(process: subprocess.Popen) -> None:
    # A helper or transport must not outlive a failed host push. macOS answers
    # EPERM for a group whose only member is Git's unreaped zombie; its pid,
    # the group's id, cannot be reused before the wait below reaps it.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    process.wait()


def git(repo: Path, *args: str, credentials: tuple[str, ...] = (), check: bool = True,
        deadline: Deadline | None = None, consume=None, stdin=None) -> bytes:
    """One Git call with the quarantine `repo` as gitdir and cwd.

    Its process group is killed when TIMEOUT_S, or the verification's
    `deadline`, passes first. `consume` takes stdout as it arrives and may raise
    to stop Git; otherwise at most OUTPUT_CAP bytes are kept. stderr is never
    read: a credential helper's can contain secrets.
    """
    command = [GIT, "--git-dir=" + str(repo), *SAFE_CONFIG, *credentials, *args]
    ends = time.monotonic() + TIMEOUT_S
    if deadline is not None and deadline.at < ends:
        ends = deadline.at

    def expired() -> PushError:
        if deadline is not None and time.monotonic() >= deadline.at:
            return PushError(f"host push verification exceeded its {deadline.seconds:g} s deadline (git {args[0]})")
        return PushError(f"host git {args[0]} failed (TimeoutExpired)")

    if time.monotonic() >= ends:
        raise expired()
    output = bytearray()
    try:
        process = subprocess.Popen(command, cwd=repo, env=_environment(),
                                   stdin=subprocess.DEVNULL if stdin is None else stdin,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PushError(f"host git {args[0]} failed ({type(exc).__name__})") from exc
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                left = ends - time.monotonic()
                if left <= 0:
                    raise expired()
                if not selector.select(left):
                    continue
                chunk = os.read(process.stdout.fileno(), 1 << 16)
                if not chunk:
                    break
                if consume is not None:
                    consume(chunk)
                    continue
                output += chunk
                if len(output) > OUTPUT_CAP:
                    raise PushError(f"host git {args[0]} printed more than {OUTPUT_CAP} bytes")
        try:
            returncode = process.wait(timeout=max(ends - time.monotonic(), 0.01))
        except subprocess.TimeoutExpired:
            raise expired() from None
    except BaseException:
        _stop(process)
        raise
    finally:
        process.stdout.close()
    if check and returncode:
        # Never publish stderr from a credential helper: it can contain secrets.
        raise PushError(f"host git {args[0]} failed (exit {returncode})")
    return bytes(output) if not returncode else b""


class _Records:
    """Each NUL-terminated record of a stream, refusing past `cap` of them."""

    def __init__(self, each, cap: int):
        self.each, self.cap, self.count, self.rest = each, cap, 0, b""

    def __call__(self, chunk: bytes) -> None:
        records = (self.rest + chunk).split(b"\0")
        self.rest = records.pop()
        if len(self.rest) > 1 << 20:
            raise PushError("a pushed tree has a path longer than 1 MiB")
        for record in records:
            self.count += 1
            if self.count > self.cap:
                raise PushError(f"a pushed tree has more than {self.cap} entries")
            self.each(record)


class _Blobs:
    """`cat-file --batch` output, each blob at most `cap` bytes."""

    def __init__(self, cap: int):
        self.cap, self.buffer, self.size, self.blobs = cap, bytearray(), None, []

    def __call__(self, chunk: bytes) -> None:
        self.buffer += chunk
        while True:
            if self.size is None:
                end = self.buffer.find(b"\n")
                if end < 0:
                    if len(self.buffer) > 256:
                        raise PushError("a symlink object cannot be read")
                    return
                header = bytes(self.buffer[:end]).split(b" ")
                del self.buffer[:end + 1]
                if len(header) != 3 or header[1] != b"blob" or not header[2].isdigit():
                    raise PushError("a symlink object cannot be read")
                self.size = int(header[2])
                if self.size > self.cap:
                    raise PushError(f"a symlink target is longer than {self.cap} bytes")
            if len(self.buffer) < self.size + 1:
                return
            self.blobs.append(bytes(self.buffer[:self.size]))
            del self.buffer[:self.size + 1]
            self.size = None


def host_credentials(repo: Path) -> tuple[str, ...]:
    """Read direct host credential settings only, ignoring global includes."""
    home = Path(os.environ.get("HOME", "/nonexistent"))
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    settings = []
    for path in (xdg / "git/config", home / ".gitconfig"):
        if not path.is_file():
            continue
        raw = git(repo, "config", "--file", str(path), "--no-includes", "--null", "--list")
        for entry in raw.split(b"\0"):
            key, sep, value = entry.partition(b"\n")
            name = key.decode("utf-8", "strict")
            if sep and name.startswith("credential.") and name.rsplit(".", 1)[-1] in {"helper", "usehttppath"}:
                settings.extend(("-c", name + "=" + value.decode("utf-8", "strict")))
    return tuple(settings)


@contextmanager
def quarantine(root: Path, head: str):
    parent = root / "push-quarantine"
    parent.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="intake-", dir=parent) as directory:
        repo = Path(directory) / "repo.git"
        repo.mkdir(mode=0o700)
        template = Path(directory) / "empty-template"
        template.mkdir()
        git(repo, "init", "--bare", "--template=" + str(template),
            "--object-format=" + ("sha256" if len(head) == 64 else "sha1"), ".")
        yield repo, host_credentials(repo)


def remote_refs(repo: Path, remote: str, credentials: tuple[str, ...]) -> tuple[str | None, dict[str, str]]:
    raw = git(repo, "ls-remote", "--symref", remote, "HEAD", "refs/heads/*", credentials=credentials)
    default, refs = None, {}
    for line in raw.decode().splitlines():
        value, ref = line.split("\t", 1)
        if ref == "HEAD" and value.startswith("ref: refs/heads/"):
            default = value[len("ref: refs/heads/"):]
        elif ref.startswith("refs/heads/") and SHA.fullmatch(value):
            refs[ref[len("refs/heads/"):]] = value
    return default, refs


def validate_submit(workdir: Path, branch: str, policy: dict, root: Path):
    try:
        settings = push_settings(policy)
        if not settings["enabled"]:
            raise PushError("host push is disabled by policy push.enabled")
        check_branch(branch, settings)
        remote, head, current, top = checkout_metadata(workdir)
        check_policy(branch, remote, policy)
        with quarantine(root, head) as (repo, credentials):
            default, refs = remote_refs(repo, remote, credentials)
        if default is None:
            raise PushError("remote default branch cannot be verified")
        check_branch(branch, settings, default)
        check_case_twins(branch, refs)
        return remote, head, default, current, top
    except (ValueError, OSError, configparser.Error) as exc:
        raise AdapterError(f"host push refused: {exc}", code=7,
                           fix="omit --push-branch, or choose an unprotected branch in an allowed origin after the hub enables push policy") from exc


def _open_directory(root: Path) -> int:
    """The job's worktree root, itself never a link, as a daemon-held handle."""
    try:
        return os.open(root, DIRECTORY_FLAGS)
    except FileNotFoundError:
        raise PushError(f"no push bundle: the job's workspace {str(root)!r} is gone") from None
    except OSError as exc:
        raise PushError(f"the job's workspace is not a plain directory ({type(exc).__name__})") from None


def intake_bundle(root: Path, target: Path, maximum: int) -> Path:
    """Copy `<root>/.subfleet/push.bundle` into the daemon-owned `target`.

    Review P1-1: the job controls every name below its worktree root, so each
    is opened from the handle on its parent, a directory with DIRECTORY_FLAGS
    and the bundle with FILE_FLAGS; nothing a link names is ever read. The bytes
    read through that one open file are what Git later verifies: a file swapped
    in afterwards is never seen. At most `maximum` bytes are copied.
    """
    descriptors = [_open_directory(root)]
    try:
        for name in BUNDLE_PATH[:-1]:
            try:
                descriptors.append(os.open(name, DIRECTORY_FLAGS, dir_fd=descriptors[-1]))
            except FileNotFoundError:
                raise PushError(f"no push bundle at {BUNDLE_RELATIVE} in the job's workspace") from None
            except OSError as exc:
                raise PushError(f"{name} must be a plain directory, not a symlink or file "
                                f"({type(exc).__name__})") from None
        try:
            source = os.open(BUNDLE_PATH[-1], FILE_FLAGS, dir_fd=descriptors[-1])
        except FileNotFoundError:
            raise PushError(f"no push bundle at {BUNDLE_RELATIVE} in the job's workspace") from None
        except OSError as exc:
            raise PushError(f"{BUNDLE_RELATIVE} must be a regular file, not a symlink "
                            f"({type(exc).__name__})") from None
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    info = os.fstat(source)
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        os.close(source)
        raise PushError(f"{BUNDLE_RELATIVE} must be a regular file" if not stat.S_ISREG(info.st_mode)
                        else "push.bundle exceeds push.max_bundle_mb")
    with os.fdopen(source, "rb") as intake:
        partial = target.with_name(target.name + ".intake")
        partial.unlink(missing_ok=True)
        with partial.open("xb") as output:
            left = maximum
            while data := intake.read(min(left + 1, 1024 * 1024)):
                left -= len(data)
                if left < 0:
                    output.close()
                    partial.unlink()
                    raise PushError("push.bundle exceeds push.max_bundle_mb")
                output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(partial, target)
    return target


def clear_bundle(root: Path) -> None:
    """Before each attempt: a bundle left by earlier work in this place is never
    taken for this attempt's. What cannot be removed here, intake refuses."""
    try:
        directory = os.open(root, DIRECTORY_FLAGS)
    except OSError:
        return
    try:
        subdirectory = os.open(BUNDLE_PATH[0], DIRECTORY_FLAGS, dir_fd=directory)
    except OSError:
        return
    finally:
        os.close(directory)
    try:
        os.unlink(BUNDLE_PATH[1], dir_fd=subdirectory)
    except OSError:
        pass
    finally:
        os.close(subdirectory)


def verify_bundle(repo: Path, bundle: Path, base: str, settings: dict, deadline: Deadline | None = None) -> str:
    deadline = deadline or Deadline(VERIFY_DEADLINE_S)
    heads = git(repo, "bundle", "list-heads", str(bundle), deadline=deadline).decode(errors="replace").splitlines()
    if len(heads) != 1 or heads[0].split(" ")[1:] != ["HEAD"]:
        raise PushError("push.bundle must advertise exactly HEAD; create it with git bundle create <path> HEAD")
    tip = heads[0].split(" ", 1)[0]
    if not SHA.fullmatch(tip):
        raise PushError("invalid bundle tip")
    try:
        return _verify_contents(repo, bundle, base, settings, tip, deadline)
    except PushError as exc:
        exc.sha = tip
        raise


def github_entries(repo: Path, commit: str, deadline: Deadline) -> frozenset[bytes]:
    """The root tree's entries named `.github` in any case or Unicode form,
    read without recursing: any change below one changes its tree id."""
    found = set()

    def each(record: bytes) -> None:
        meta, _, name = record.partition(b"\t")
        if fold(name.decode("utf-8", "surrogateescape")) == ".github":
            found.add(record)
    git(repo, "ls-tree", "-z", "--full-tree", commit, deadline=deadline, consume=_Records(each, TREE_ENTRY_CAP))
    return frozenset(found)


def _parent_component(part: str) -> bool:
    """`..` in any case or Unicode form (NFKC also folds `．．` and `‥`), with the
    trailing dots and spaces Windows ignores."""
    return re.fullmatch(r"[. ]*\.\.[. ]*", unicodedata.normalize("NFKC", part).casefold()) is not None


def check_symlinks(links: dict[bytes, bytes]) -> None:
    """Review P2-3: a symlink may name only a place inside its tree that no other
    symlink stands on. Refused when its target is absolute, has a `..`
    component, or passes through or ends at a symlink of the same tree; paths
    compare casefolded and NFC-normalized, as a macOS checkout resolves them."""
    def text(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise PushError("a symlink path or target is not UTF-8") from None

    names = {fold(text(path)) for path in links}
    for path, raw in links.items():
        name, target = text(path), text(raw)
        shown = repr(name[:200])
        if (not target or target.startswith("/") or "\\" in target or "\0" in target
                or re.match(r"[A-Za-z]:", target)):
            raise PushError(f"symlink {shown} has an absolute target")
        parts = target.split("/")
        # Normalize before splitting too: NFKC can turn a fullwidth slash
        # into a separator, exposing a normalized parent component.
        if any(_parent_component(part) for part in unicodedata.normalize("NFKC", target).split("/")):
            raise PushError(f"symlink {shown} has a '..' component in its target")
        walk = name.split("/")[:-1]
        steps = [walk[:end] for end in range(1, len(walk) + 1)]
        for part in parts:
            if part not in ("", "."):
                walk = [*walk, part]
                steps.append(walk)
        if any(fold("/".join(step)) in names for step in steps):
            raise PushError(f"symlink {shown} passes through another symlink")


def _check_tree(repo: Path, commit: str, deadline: Deadline, targets: dict[bytes, bytes]) -> None:
    """Every entry of one commit's tree, streamed: no gitlinks, safe symlinks."""
    links: dict[bytes, bytes] = {}

    def each(record: bytes) -> None:
        meta, _, path = record.partition(b"\t")
        mode, _, oid = meta.split(b" ")
        if mode == b"160000":
            raise PushError("gitlinks (submodules) are refused")
        if mode == b"120000":
            if len(links) >= SYMLINK_CAP:
                raise PushError(f"a pushed tree has more than {SYMLINK_CAP} symlinks")
            links[path] = oid
    git(repo, "ls-tree", "-r", "-z", "--full-tree", commit, deadline=deadline,
        consume=_Records(each, TREE_ENTRY_CAP))
    wanted = sorted({oid for oid in links.values() if oid not in targets})
    if wanted:
        request = repo.parent / "symlinks.txt"
        request.write_bytes(b"".join(oid + b"\n" for oid in wanted))
        blobs = _Blobs(SYMLINK_TARGET_CAP)
        with request.open("rb") as stdin:
            git(repo, "cat-file", "--batch", deadline=deadline, consume=blobs, stdin=stdin)
        if len(blobs.blobs) != len(wanted):
            raise PushError("a symlink object cannot be read")
        targets.update(zip(wanted, blobs.blobs))
    check_symlinks({path: targets[oid] for path, oid in links.items()})


def _verify_contents(repo: Path, bundle: Path, base: str, settings: dict, tip: str, deadline: Deadline) -> str:
    # An empty quarantine accepts only a full, self-contained bundle.
    git(repo, "bundle", "verify", str(bundle), deadline=deadline)
    git(repo, "fetch", "--no-tags", "--no-write-fetch-head", str(bundle), "HEAD:refs/heads/intake", deadline=deadline)
    git(repo, "fsck", "--strict", "--no-reflogs", deadline=deadline)
    if git(repo, "rev-parse", "refs/heads/intake^{commit}", deadline=deadline).decode().strip() != tip:
        raise PushError("bundle HEAD must be a commit")
    # Use --is-ancestor's actual exit status, never a graph inferred from text.
    try:
        git(repo, "merge-base", "--is-ancestor", base, tip, deadline=deadline)
    except PushError as exc:
        raise PushError("bundle tip does not descend from recorded workdir_head") from exc
    commits = git(repo, "rev-list", "--max-count=" + str(settings["max_commits"] + 1), f"{base}..{tip}",
                  deadline=deadline).decode().splitlines()
    if len(commits) > settings["max_commits"]:
        raise PushError("bundle exceeds push.max_commits")
    # Every introduced commit, not only the tip: reverted workflows and unsafe
    # links in intermediate commits must not reach the remote either. No call
    # here recurses through trees except the capped, streamed `ls-tree -r`.
    reference = github_entries(repo, base, deadline)
    introduced = list(dict.fromkeys([tip, *commits]))
    for commit in introduced:
        if github_entries(repo, commit, deadline) != reference:
            raise PushError("changes under .github/ require a human or hub push (CI secrets)")
    targets: dict[bytes, bytes] = {}
    for commit in introduced:
        _check_tree(repo, commit, deadline, targets)
    return tip


def push_refspec(sha: str, branch: str) -> str:
    # Separate command boundary: even a later refactor must not permit a '+',
    # an option, symbolic source, delete, or wildcard destination here.
    if not SHA.fullmatch(sha):
        raise PushError("push requires a verified commit SHA")
    check_branch(branch, {"protected": []})
    refspec = f"{sha}:refs/heads/{branch}"
    if refspec.startswith("+") or refspec.count(":") != 1:
        raise PushError("push must use one non-forcing, non-deleting refspec")
    return refspec


def prepare_workspace(root: Path, source: Path, base: str, destination: Path) -> None:
    """Allocate before launch by copying objects as data, ignoring alternates.

    `git worktree add` in the caller's repo would execute its config/hooks.
    A standalone checkout also keeps its later config edits away from the host.
    """
    checkout_metadata(source)
    _, _, gitdir = _git_directories(source)
    with quarantine(root, base) as (repo, _):
        objects = gitdir / "objects"
        for folder in objects.iterdir():
            if folder.is_symlink() or not folder.is_dir():
                continue
            if folder.name != "pack" and not re.fullmatch(r"[0-9a-f]{2}", folder.name):
                continue
            for item in folder.iterdir():
                pattern = r"pack-[0-9a-f]{40,64}\.(pack|idx)" if folder.name == "pack" else r"[0-9a-f]{38}|[0-9a-f]{62}"
                if not re.fullmatch(pattern, item.name):
                    continue
                target = repo / "objects" / folder.name / item.name
                target.parent.mkdir(exist_ok=True)
                fd = os.open(item, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as data:
                    if not stat.S_ISREG(os.fstat(data.fileno()).st_mode):
                        raise PushError("baseline object is not a regular file")
                    with target.open("xb") as output:
                        shutil.copyfileobj(data, output)
        git(repo, "cat-file", "-e", base + "^{commit}")
        git(repo, "fsck", "--strict", "--no-reflogs", base)
        git(repo, "update-ref", "refs/heads/work", base)
        git(repo, "symbolic-ref", "HEAD", "refs/heads/work")
        git(repo, "clone", "--no-local", "--template=" + str(repo.parent / "empty-template"),
            "--", str(repo), str(destination))
        (destination / ".git/HEAD").write_text(base + "\n")
        # The bundle is the job's delivery, never part of its commits.
        (destination / ".git/info").mkdir(exist_ok=True)
        (destination / ".git/info/exclude").write_text(f"/{BUNDLE_PATH[0]}/\n")
