"""Round-4 P3-3: each checkout look parses each bounded config only once."""
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import tempfile

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import host_push
from subfleet.adapters.base import AdapterError
from subfleet.policy import PUSH_DEFAULTS
from test_host_push import worlds  # noqa: F401


NEW_CONFIG_CAP = 256 * 1024
HEAD = "1" * 40
REMOTE = "file:///config-cost-origin.git"


def checkout(root: Path, *, linked=False, sections=0):
    """Metadata as Git writes it; these data-only checks never invoke Git."""
    work = root / "work"
    work.mkdir(exist_ok=True)
    common = root / "common" if linked else work / ".git"
    common.mkdir(exist_ok=True)
    gitdir = common / "worktrees/linked" if linked else common
    gitdir.mkdir(parents=True, exist_ok=True)
    if linked:
        (work / ".git").write_text(f"gitdir: {gitdir}\n")
        (gitdir / "commondir").write_text("../..\n")
        (gitdir / "config.worktree").write_text('[remote "origin"]\nurl = ' + REMOTE + "\n")
    (common / "objects/info").mkdir(parents=True, exist_ok=True)
    (common / "config").write_text(
        '[remote "origin"]\nurl = ' + REMOTE + "\n"
        + ("[extensions]\nworktreeConfig = true\n" if linked else "")
        + "".join(f"[extra{n}]\nvalue = yes\n" for n in range(sections)))
    (gitdir / "HEAD").write_text("ref: refs/heads/feature/config\n")
    (common / "refs/heads/feature").mkdir(parents=True, exist_ok=True)
    (common / "refs/heads/feature/config").write_text(HEAD + "\n")
    return work, common


def stub_remote(monkeypatch, root):
    @contextmanager
    def quarantine(*args):
        yield root, ()
    monkeypatch.setattr(host_push, "quarantine", quarantine)
    monkeypatch.setattr(host_push, "remote_refs", lambda *args: ("trunk", {}))


def look(work, operation, root):
    if operation == "metadata":
        host_push.checkout_metadata(work)
    elif operation == "write":
        host_push.validate_write_location(work)
    else:
        policy = {"push": {**PUSH_DEFAULTS, "enabled": True, "allowed_remotes": [REMOTE]}}
        host_push.validate_submit(work, "jobs/config", policy, root)


def count_parses(monkeypatch):
    counts = Counter()
    parse = host_push._config

    def counted(text):
        counts[text] += 1
        return parse(text)
    monkeypatch.setattr(host_push, "_config", counted)
    return counts


def assert_one_parse(counts, linked):
    assert len(counts) == 1 + int(linked), counts
    assert all(count == 1 for count in counts.values()), counts


def test_a_config_over_256_kib_is_refused_before_parsing(tmp_path, monkeypatch):
    work, common = checkout(tmp_path)
    config = common / "config"
    data = config.read_text()
    config.write_text(data + "#" + "x" * (NEW_CONFIG_CAP - len(data)))
    counts = count_parses(monkeypatch)
    with pytest.raises(AdapterError, match="larger than 262144 bytes") as error:
        host_push.validate_write_location(work)
    assert error.value.code == 7
    assert not counts


def test_submit_parses_config_once_per_look(tmp_path, monkeypatch):
    work, _ = checkout(tmp_path)
    stub_remote(monkeypatch, tmp_path)
    counts = count_parses(monkeypatch)
    look(work, "submit", tmp_path)
    assert_one_parse(counts, False)


def test_prepare_workspace_parses_config_once_per_look(worlds, monkeypatch):
    with worlds() as world:
        counts = count_parses(monkeypatch)
        host_push.prepare_workspace(world.core.root, world.repo, world.base,
                                    world.core.root / "prepared")
        assert_one_parse(counts, False)


def test_next_look_reloads_config(tmp_path, monkeypatch):
    work, common = checkout(tmp_path)
    stub_remote(monkeypatch, tmp_path)
    counts = count_parses(monkeypatch)
    look(work, "submit", tmp_path)
    assert_one_parse(counts, False)
    with (common / "config").open("a") as config:
        config.write('[remote "promisor"]\npromisor = true\n')
    with pytest.raises(AdapterError, match="partial clone"):
        look(work, "submit", tmp_path)
    assert len(counts) == 2
    assert all(count == 1 for count in counts.values())


def test_duplicate_parse_mutation_is_caught(tmp_path, monkeypatch):
    """Restore the previous second read: the one-parse assertion rejects it."""
    work, _ = checkout(tmp_path)
    stub_remote(monkeypatch, tmp_path)
    counts = count_parses(monkeypatch)
    check = host_push.check_object_store

    def independent_check(workdir, **kwargs):
        kwargs.pop("_look", None)
        check(workdir, **kwargs)
    monkeypatch.setattr(host_push, "check_object_store", independent_check)
    look(work, "submit", tmp_path)
    with pytest.raises(AssertionError):
        assert_one_parse(counts, False)


def test_old_config_cap_mutation_is_caught(tmp_path, monkeypatch):
    work, common = checkout(tmp_path)
    config = common / "config"
    data = config.read_text()
    config.write_text(data + "#" + "x" * (NEW_CONFIG_CAP - len(data)))
    monkeypatch.setattr(host_push, "CONFIG_CAP", 4 * 1024 * 1024)
    refused = False
    try:
        host_push.validate_write_location(work)
    except AdapterError:
        refused = True
    with pytest.raises(AssertionError, match="over-budget config was accepted"):
        assert refused, "over-budget config was accepted"


@settings(max_examples=30, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(linked=st.booleans(), sections=st.integers(min_value=0, max_value=80),
       operation=st.sampled_from(["metadata", "write", "submit"]))
def test_one_parse_per_config_per_look(tmp_path, monkeypatch, linked, sections, operation):
    # Isolate generated examples: a linked gitfile and a .git directory occupy
    # the same name and must not leak from one generated layout into the next.
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory, monkeypatch.context() as patch:
        root = Path(directory)
        work, _ = checkout(root, linked=linked, sections=sections)
        stub_remote(patch, root)
        counts = count_parses(patch)
        look(work, operation, root)
        assert_one_parse(counts, linked)
