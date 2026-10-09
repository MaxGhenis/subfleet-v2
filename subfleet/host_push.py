"""C-8.5: data-only bundle intake and host-side, non-forcing branch publication.

Never give Git a lane's worktree, gitdir, config, environment, or object store.
Submit reads checkout metadata as text; all Git subprocesses use a fresh bare
repository with only the host's directly configured credential helpers.
"""
from __future__ import annotations

import configparser
import fnmatch
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from urllib.parse import urlsplit

from .adapters.base import AdapterError
from .policy import push_settings

TIMEOUT_S = 60
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
GIT = shutil.which("git") or "/usr/bin/git"


class PushError(ValueError):
    pass


def check_branch(branch: str, settings: dict, default: str | None = None) -> None:
    if (not isinstance(branch, str) or not BRANCH.fullmatch(branch) or ".." in branch
            or "@{" in branch or branch.endswith(".lock") or branch.startswith("/")
            or branch.endswith(("/", ".")) or "//" in branch
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))):
        raise PushError("invalid push branch; use a Git branch name of 1 to 200 allowed characters")
    if (branch in {"main", "master", default} or branch.startswith("release/")
            or any(fnmatch.fnmatchcase(branch, pattern) for pattern in settings["protected"])):
        raise PushError(f"push branch {branch!r} is protected")


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


def _config(path: Path) -> configparser.RawConfigParser:
    # This is a data parser, not `git config`: include/includeIf are never read.
    config = configparser.RawConfigParser(strict=False, interpolation=None, allow_no_value=True)
    config.read_string(path.read_text())
    return config


def checkout_metadata(workdir: Path) -> tuple[str, str, str | None, Path]:
    """Host snapshot at submit, supporting linked worktrees and packed refs."""
    top = next((p for p in (workdir, *workdir.parents) if (p / ".git").exists()), None)
    if top is None:
        raise PushError("push requires a committed Git checkout with an origin")
    gitdir = top / ".git"
    if gitdir.is_file():
        text = gitdir.read_text().strip()
        if not text.startswith("gitdir: "):
            raise PushError("invalid checkout gitdir")
        gitdir = (top / text[8:]).resolve()
    common = gitdir
    if (gitdir / "commondir").is_file():
        common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
    config = _config(common / "config")
    remote = config.get('remote "origin"', "url", fallback=None)
    if (config.get("extensions", "worktreeconfig", fallback="false") or "true").lower() == "true" and (gitdir / "config.worktree").is_file():
        remote = _config(gitdir / "config.worktree").get('remote "origin"', "url", fallback=remote)
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
            loose = next((p / ref for p in (gitdir, common) if (p / ref).is_file()), None)
            if loose:
                value = loose.read_text().strip()
                continue
            packed = common / "packed-refs"
            if packed.is_file():
                for line in packed.read_text().splitlines():
                    if line.endswith(" " + ref) and SHA.fullmatch(line.split(" ", 1)[0]):
                        return line.split(" ", 1)[0]
            break
        raise PushError("push requires a checkout HEAD naming a committed baseline")

    head = (gitdir / "HEAD").read_text().strip()
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


def git(repo: Path, *args: str, credentials: tuple[str, ...] = (), check: bool = True) -> bytes:
    command = [GIT, "--git-dir=" + str(repo), "-c", "core.hooksPath=" + os.devnull,
               "-c", "core.fsmonitor=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
               "-c", "protocol.allow=never",
               "-c", "protocol.file.allow=always", "-c", "protocol.https.allow=always",
               "-c", "protocol.ssh.allow=always", *credentials, *args]
    try:
        with subprocess.Popen(command, cwd=repo, env=_environment(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, start_new_session=True) as process:
            try:
                stdout, _stderr = process.communicate(timeout=TIMEOUT_S)
            except subprocess.TimeoutExpired:
                # A helper or transport must not outlive a failed host push.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise
            returncode = process.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        raise PushError(f"host git {args[0]} failed ({type(exc).__name__})") from exc
    if check and returncode:
        # Never publish stderr from a credential helper: it can contain secrets.
        raise PushError(f"host git {args[0]} failed (exit {returncode})")
    return stdout if not returncode else b""


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
            default, _ = remote_refs(repo, remote, credentials)
        if default is None:
            raise PushError("remote default branch cannot be verified")
        check_branch(branch, settings, default)
        return remote, head, default, current, top
    except (ValueError, OSError, configparser.Error) as exc:
        raise AdapterError(f"host push refused: {exc}", code=7,
                           fix="omit --push-branch, or choose an unprotected branch in an allowed origin after the hub enables push policy") from exc


def copy_bundle(source: Path, repo: Path, maximum: int) -> Path:
    target = repo.parent / "push.bundle"
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as intake:
        if not stat.S_ISREG(os.fstat(intake.fileno()).st_mode):
            raise PushError("push.bundle must be a regular file")
        if os.fstat(intake.fileno()).st_size > maximum:
            raise PushError("push.bundle exceeds push.max_bundle_mb")
        with target.open("xb") as output:
            left = maximum
            while data := intake.read(min(left + 1, 1024 * 1024)):
                left -= len(data)
                if left < 0:
                    raise PushError("push.bundle exceeds push.max_bundle_mb")
                output.write(data)
    return target


def verify_bundle(repo: Path, bundle: Path, base: str, settings: dict) -> str:
    heads = git(repo, "bundle", "list-heads", str(bundle)).decode().splitlines()
    if len(heads) != 1 or heads[0].split(" ")[1:] != ["HEAD"]:
        raise PushError("push.bundle must advertise exactly HEAD; create it with git bundle create <path> HEAD")
    tip = heads[0].split(" ", 1)[0]
    if not SHA.fullmatch(tip):
        raise PushError("invalid bundle tip")
    try:
        return _verify_contents(repo, bundle, base, settings, tip)
    except PushError as exc:
        exc.sha = tip
        raise


def _verify_contents(repo: Path, bundle: Path, base: str, settings: dict, tip: str) -> str:
    # An empty quarantine accepts only a full, self-contained bundle.
    git(repo, "bundle", "verify", str(bundle))
    git(repo, "fetch", "--no-tags", "--no-write-fetch-head", str(bundle), "HEAD:refs/heads/intake")
    git(repo, "fsck", "--strict", "--no-reflogs")
    if git(repo, "rev-parse", "refs/heads/intake^{commit}").decode().strip() != tip:
        raise PushError("bundle HEAD must be a commit")
    # Use --is-ancestor's actual exit status, never a graph inferred from text.
    try:
        git(repo, "merge-base", "--is-ancestor", base, tip)
    except PushError as exc:
        raise PushError("bundle tip does not descend from recorded workdir_head") from exc
    commits = git(repo, "rev-list", "--max-count=" + str(settings["max_commits"] + 1), f"{base}..{tip}").decode().splitlines()
    if len(commits) > settings["max_commits"]:
        raise PushError("bundle exceeds push.max_commits")
    # Inspect each introduced tree as well as its diff; reverted workflows and
    # unsafe links in intermediate commits must not reach the remote either.
    for commit in commits:
        paths = git(repo, "diff-tree", "--root", "-m", "--no-commit-id", "--no-renames", "--name-only", "-r", "-z", commit)
        if any(p.lower() == b".github" or p.lower().startswith(b".github/") for p in paths.split(b"\0")):
            raise PushError("changes under .github/ require a human or hub push (CI secrets)")
    for commit in set([tip, *commits]):
        links = {}
        for entry in git(repo, "ls-tree", "-r", "-z", commit).split(b"\0"):
            if not entry:
                continue
            meta, path = entry.split(b"\t", 1)
            mode, _, blob = meta.split(b" ")
            if mode == b"160000":
                raise PushError("gitlinks (submodules) are refused")
            if mode == b"120000":
                target = git(repo, "cat-file", "blob", blob.decode())
                if (target.startswith((b"/", b"\\")) or b"\\" in target or b"\0" in target
                        or re.match(rb"[A-Za-z]:", target)):
                    raise PushError("symlink points outside the repository")
                links[path] = target
        for path, target in links.items():
            pending = path.split(b"/")[:-1] + target.split(b"/")
            resolved, expanded = [], 0
            while pending:
                part = pending.pop(0)
                if part == b"..":
                    if not resolved:
                        raise PushError("symlink points outside the repository")
                    resolved.pop()
                elif part not in (b"", b"."):
                    candidate = b"/".join([*resolved, part])
                    if candidate in links:
                        expanded += 1
                        if expanded > 40:
                            raise PushError("cyclic or excessively chained symlink")
                        pending = links[candidate].split(b"/") + pending
                    else:
                        resolved.append(part)
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
    _, _, _, top = checkout_metadata(source)
    gitdir = top / ".git"
    if gitdir.is_file():
        gitdir = (top / gitdir.read_text().strip()[8:]).resolve()
    if (gitdir / "commondir").is_file():
        gitdir = (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
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
