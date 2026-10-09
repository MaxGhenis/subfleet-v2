"""Prepared disk fixtures with fresh daemons and complete SQL reset per world.

Only the proof TestCase opts in. No process/census/resolver call is replaced.
The immutable repository and admission setup are reused; mutable receipts and
both real databases are restored after closing every previous connection.
"""
from copy import deepcopy
import os
from pathlib import Path
import sqlite3

from subfleet.salvage import _git


def tree_paths(root, *, prune_git=False):
    paths = set()
    for directory, subdirectories, files in os.walk(root):
        if prune_git:
            subdirectories[:] = [name for name in subdirectories if name != ".git"]
        paths.update(Path(directory) / name for name in (*subdirectories, *files))
    return paths


class PreparedWorld:
    def __init__(self, machine):
        self.directory = machine.directory
        self.harness = machine.harness
        self.fixture = machine.fixture
        self.daemon_type = type(machine.daemon)
        self.attempts = deepcopy(machine.attempts)
        self.leases = deepcopy(machine.protected_leases)
        self.workspaces = deepcopy(machine.protected_workspaces)
        self.databases = []
        for connection, path in self.connections(machine.daemon):
            snapshot = sqlite3.connect(":memory:")
            connection.backup(snapshot)
            self.databases.append((snapshot, Path(path), tuple(snapshot.iterdump())))
        self.files = {}
        self.workspace_paths = {}
        self.ref_paths = {}
        for workspace in set().union(*self.workspaces.values()):
            self.workspace_paths[workspace] = tree_paths(workspace, prune_git=True)
            common = Path(_git(workspace, "rev-parse", "--git-common-dir",
                               timeout_s=machine.daemon.policy["caps"]["workspace_git_timeout_s"]))
            common = (common if common.is_absolute() else workspace / common).resolve()
            refs = common / "refs"
            self.ref_paths[refs] = tree_paths(refs)
            for path in workspace.rglob("*"):
                if path.is_file():
                    self.files[path] = path.read_bytes()
        self.artifacts = machine.daemon.store.query("SELECT * FROM artifacts WHERE role='salvage' ORDER BY artifact_id")
        self.published = {}
        self.log_path = machine.daemon.root / "daemon.log"
        self.log_baseline = self.log_path.read_bytes()
        self.active = machine
        self.completed = 0
        self.poisoned = False
        machine.daemon.publish_hook = self.track_publication

    @staticmethod
    def connections(daemon):
        return ((daemon.store.connection, daemon.store.path),
                (daemon.conversations.store._db, daemon.conversations.store.path))

    def track_publication(self, role, path):
        path = Path(path).absolute()
        # Compare resolved folders: macOS spells the same temporary directory
        # `/var/...` and `/private/var/...`, and the Harness and Daemon resolve
        # theirs (review r9 P2; CI at e4a45640). The file's own name is kept
        # unresolved, so a published symlink is judged by where it sits.
        assert (path.parent.resolve() / path.name).is_relative_to(Path(self.directory.name).resolve()), \
            "pooled world publication outside its fixture"
        if path not in self.published:
            self.published[path] = path.read_bytes() if path.exists() else None

    def restore_databases(self):
        assert self.active is None, "pooled world overlap"
        self.log_path.write_bytes(self.log_baseline)
        for snapshot, path, _ in self.databases:
            connection = sqlite3.connect(path, isolation_level=None)
            try:
                connection.execute("PRAGMA synchronous=NORMAL")
                snapshot.backup(connection)
                assert not connection.in_transaction
            finally:
                connection.close()

    def check_reset(self, daemon):
        for (connection, _), (_, _, dump) in zip(self.connections(daemon), self.databases):
            assert not connection.in_transaction
            assert tuple(connection.iterdump()) == dump, "pooled world's SQL snapshot was not fully restored"
        assert not self.published, "pooled receipt ledger was not restored"

    def attach(self, machine):
        assert self.active is None
        self.active = machine
        machine.daemon.publish_hook = self.track_publication

    def finish(self, machine):
        assert self.active is machine
        try:
            assert machine.daemon.store.query(
                "SELECT * FROM artifacts WHERE role='salvage' ORDER BY artifact_id") == self.artifacts, (
                    "kernel world unexpectedly changed the real workspace")
            machine.daemon.close()  # Includes every reader, worker and conversation connection.
            self.harness.check_notices()
            for path, contents in self.files.items():
                assert path.is_file() and path.read_bytes() == contents, (
                    "pooled protected workspace or Git metadata changed", str(path))
            for workspace, paths in self.workspace_paths.items():
                assert tree_paths(workspace, prune_git=True) == paths, "pooled workspace paths changed"
            for refs, paths in self.ref_paths.items():
                assert tree_paths(refs) == paths, "pooled Git refs changed"
        except BaseException:
            self.poisoned = True
            raise
        finally:
            try:
                machine.daemon.close()
                for path, contents in self.published.items():
                    if contents is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(contents)
                self.published.clear()
            except BaseException:
                self.poisoned = True
                raise
            finally:
                machine.patch.undo()
                self.active = None
                self.completed += 1

    def close(self):
        try:
            if self.active is not None:
                self.finish(self.active)
            self.fixture.close()
        finally:
            for snapshot, _, _ in self.databases:
                snapshot.close()
            self.directory.cleanup()
