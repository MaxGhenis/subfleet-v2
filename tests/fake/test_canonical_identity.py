"""L1/L2/L3: real submit/reservation with independent alias expectations.

Providers and process census are injected; SQLite ownership, admission and
export are production methods. No provider or daemon process is started.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unicodedata
import uuid

import pytest
from hypothesis import example, given, settings, strategies as st

from subfleet import daemon as daemon_module, folders, resource_leases
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential
from subfleet.conversations.store import canonical_native
from subfleet.daemon import native_session_lease_key, revive_lease_key
from tests.fake.test_admission_latency import fleet_daemon, measure, submit, submit_turn

MINIMAL = "00000000-0000-4000-8000-00000000000a"
MASKED = "aabbccdd-1122-4eef-8abc-abcdefabcdef"
PREFIX_MASK = sum(1 << i for i, (a, b) in enumerate(zip(MASKED, "AaBbCcDd-1122-4eEf-8aBc-abcdefabcdef")) if a != b)
SUFFIX_MASK = sum(1 << i for i, (a, b) in enumerate(zip(MASKED, "aabbccdd-1122-4eef-8abc-AbCdEfAbCdEf")) if a != b)
SETTINGS = {"model": "haiku", "effort": None, "fast": False,
            "permission": "read-only", "auto_continue": False}


def configure(service, patch):
    from dataclasses import replace
    service.desktop_prober = lambda: None
    patch.setattr(daemon_module.procs, "same_process", lambda *args: False)
    original = service.store.get_lane("codex-1")
    for number in (1, 2):
        lane = f"claude-{number}"
        service.store.put_lane(replace(original, lane_id=lane, provider="claude",
                                      account_key=f"claude:fixture-{number}",
                                      credential=Credential("claude", original.home, "home")))
        measure(service, lane)
    for lane in ("codex-1", "codex-2", "codex-3"):
        measure(service, lane)
    patch.setattr(service, "_prepare_route", lambda *_:
                  ({("claude-1", "haiku"), ("claude-2", "haiku"),
                    ("codex-1", "astra"), ("codex-2", "astra"), ("codex-3", "astra")},
                   service._desktop_identity()))
    patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))


def revive(service, harness, session, lane="claude-1"):
    return submit(service, harness, kind="revive", caller_session=session,
                  pinned_model="haiku", pinned_lane=lane)


@settings(max_examples=12, deadline=None, derandomize=True, database=None)
@given(value=st.integers(min_value=0, max_value=2**128 - 2), mask=st.integers(0, 2**36 - 1))
@example(value=10, mask=2**36 - 1)
@example(value=uuid.UUID(MASKED).int, mask=PREFIX_MASK)
@example(value=uuid.UUID(MASKED).int, mask=SUFFIX_MASK)
def test_native_aliases_collide_and_distinct_uuids_admit(value, mask):
    lower = str(uuid.UUID(int=value, version=4))
    alias = "".join(c.upper() if mask & (1 << i) else c for i, c in enumerate(lower))
    distinct = str(uuid.UUID(int=value + 1, version=4))
    assert canonical_native(alias) == lower
    assert canonical_native(canonical_native(alias)) == canonical_native(alias)
    assert canonical_native(alias) == canonical_native(alias)
    with tempfile.TemporaryDirectory(prefix="identity-native-") as directory:
        with fleet_daemon(Path(directory) / "state") as (service, harness, patch):
            configure(service, patch)
            first = revive(service, harness, alias)
            with pytest.raises(AdapterError, match="live revive"):
                revive(service, harness, lower, "claude-2")
            second = revive(service, harness, distinct, "claude-2")
            service._admit()
            assert all(len(service.store.list_attempts(job)) == 1 for job in (first, second))
            keys = {row["lease_key"] for row in service.store.list_leases()}
            assert resource_leases.native_key("claude", lower) in keys
            assert f"native:claude:{alias}" not in keys or alias == lower
            assert native_session_lease_key("claude-1", lower) in keys
            assert revive_lease_key(lower) in keys
            assert service.store.get_job(first)["caller_session"] == alias


@pytest.mark.parametrize("legacy", ["native", "native-session", "session"])
def test_old_uppercase_native_leases_still_exclude_new_owner(tmp_path, legacy):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        job = revive(service, harness, MINIMAL)
        key = {"native": f"native:claude:{MINIMAL.upper()}",
               "native-session": f"native-session:claude-1:{MINIMAL.upper()}",
               "session": f"session:{MINIMAL.upper()}:revive"}[legacy]
        service.store.acquire_lease(key, "legacy-holder")
        service._admit()
        assert not service.store.list_attempts(job)
        assert service.store.get_job(job)["state"] == ("failed" if legacy == "session" else "waiting")
        assert any(row["holder"] == "legacy-holder" for row in service.store.list_leases())


def test_old_lane_scoped_native_guard_blocks_a_turn_on_another_lane(tmp_path):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        job = submit_turn(service, harness, 1)
        manifest = service._read_json(service.root / "jobs" / job / "manifest.json")
        manifest["turn"].update(native_session_id=MINIMAL, provider="codex")
        service._publish("manifest", service.root / "jobs" / job / "manifest.json",
                         daemon_module.json_bytes(manifest))
        service.store.acquire_lease(f"native-session:codex-9:{MINIMAL.upper()}", "legacy-holder")
        service._admit_turns()
        assert not service.store.list_attempts(job)


@settings(max_examples=6, deadline=None, derandomize=True, database=None)
@given(pair=st.sampled_from([("a", "b"), ("café", "cafe"), ("A", "a"),
                            ("café", "cafe\u0301"), ("a", "a/"), ("a", "link/../a")]))
def test_distinct_opaque_native_names_admit_independently(pair):
    with tempfile.TemporaryDirectory(prefix="identity-opaque-") as directory:
        with fleet_daemon(Path(directory) / "state") as (service, harness, patch):
            configure(service, patch)
            jobs = [revive(service, harness, value, f"claude-{i + 1}") for i, value in enumerate(pair)]
            service._admit()
            assert all(len(service.store.list_attempts(job)) == 1 for job in jobs)


NAMES = st.sampled_from(["Result", "café", "A-café"])
VIA = st.sampled_from(["case", "nfd", "slash", "dots", "symlink"])


def require_case_aliases(base):
    if folders._case_sensitive(base):
        pytest.skip("physical case aliases require a case-insensitive volume")


def path_pair(base, name, via, parent, present):
    directory = base / "Output-café"
    directory.mkdir()
    first = directory / f"{name}.md"
    alias_name = "Output-café" if parent else f"{name}.md"
    if via == "case":
        alias_name = alias_name.swapcase()
    if via == "nfd":
        alias_name = unicodedata.normalize("NFD", alias_name)
    alias_dir = base / alias_name if parent else directory
    second = alias_dir / first.name if parent else alias_dir / alias_name
    if via == "dots":
        second = alias_dir / ".." / directory.name / second.name
    if via == "symlink":
        link = base / "Linked-parent"
        link.symlink_to(directory, target_is_directory=True)
        second = link / first.name
    if present:
        first.write_bytes(b"initial\n")
        assert first.samefile(second)
    return str(first), str(second) + ("/" if via == "slash" else "")


@settings(max_examples=12, deadline=None, derandomize=True, database=None)
@given(name=NAMES, via=VIA, parent=st.booleans(), present=st.booleans())
@example(name="Result", via="case", parent=False, present=False)
@example(name="café", via="nfd", parent=False, present=False)
@example(name="Result", via="case", parent=True, present=True)
@example(name="café", via="nfd", parent=True, present=True)
@example(name="Result", via="slash", parent=True, present=False)
@example(name="Result", via="dots", parent=True, present=False)
@example(name="Result", via="symlink", parent=True, present=False)
@example(name="Result", via="slash", parent=False, present=False)
@example(name="Result", via="dots", parent=False, present=False)
@example(name="Result", via="symlink", parent=False, present=False)
def test_output_and_parent_aliases_collide_through_submit_and_admission(name, via, parent, present):
    with tempfile.TemporaryDirectory(prefix="identity-output-") as directory:
        base = Path(directory)
        require_case_aliases(base)
        left, right = path_pair(base, name, via, parent, present)
        want = folders.identity(left)
        assert want == folders.identity(right)
        assert folders.identity(want) == want == folders.identity(left)
        with fleet_daemon(base / "state") as (service, harness, patch):
            configure(service, patch)
            first = submit(service, harness, out_path=left, caller_session=None)
            service._admit()
            with pytest.raises(AdapterError, match="output|out_path"):
                submit(service, harness, out_path=right, caller_session=None)
            assert len(service.store.list_attempts()) == 1
            assert {row["lease_key"] for row in service.store.list_leases() if row["lease_key"].startswith("out:")} == {f"out:{want}"}
            assert service.store.get_job(first)["out_path"] == str(Path(left).resolve())


@pytest.mark.parametrize("name,alias", [("Result.md", "result.md"), ("café.md", "cafe\u0301.md")])
@pytest.mark.parametrize("admit_first", [True, False])
def test_minimized_absent_output_alias_has_one_owner(tmp_path, name, alias, admit_first):
    require_case_aliases(tmp_path)
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        submit(service, harness, out_path=str(tmp_path / name), caller_session=None)
        if admit_first:
            service._admit()
        with pytest.raises(AdapterError, match="output|out_path"):
            submit(service, harness, out_path=str(tmp_path / alias), caller_session=None)
        service._admit()
        assert len(service.store.list_attempts()) == 1


@pytest.mark.parametrize("parent", [False, True])
@settings(max_examples=8, deadline=None, derandomize=True, database=None)
@given(pair=st.sampled_from([("a", "b"), ("café", "cafe"), ("Result", "Results")]))
def test_distinct_outputs_and_parents_never_collide(parent, pair):
    with tempfile.TemporaryDirectory(prefix="identity-distinct-") as directory:
        base = Path(directory)
        paths = []
        for name in pair:
            folder = base / name if parent else base
            folder.mkdir(exist_ok=True)
            paths.append(folder / ("same.md" if parent else f"{name}.md"))
        assert folders.identity(paths[0]) != folders.identity(paths[1])
        with fleet_daemon(base / "state") as (service, harness, patch):
            configure(service, patch)
            jobs = [submit(service, harness, out_path=str(path), caller_session=None) for path in paths]
            service._admit()
            assert all(len(service.store.list_attempts(job)) == 1 for job in jobs)
            assert len([row for row in service.store.list_leases() if row["lease_key"].startswith("out:")]) == 2


@pytest.mark.parametrize("alias", ["result.md", "cafe\u0301.md", "OUTPUT/Result.md"])
def test_output_reservation_rechecks_an_old_alias_inserted_during_preparation(tmp_path, alias):
    require_case_aliases(tmp_path)
    base = tmp_path / "Output"
    base.mkdir()
    left = base / ("café.md" if "cafe" in alias else "Result.md")
    right = tmp_path / alias if "/" in alias else base / alias
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        job = submit(service, harness, out_path=str(left), caller_session=None)
        prepare = service._prepare_route
        def insert(*args):
            service.store.acquire_lease(f"out:{right}", "legacy-holder")
            return prepare(*args)
        patch.setattr(service, "_prepare_route", insert)
        service._admit()
        assert not service.store.list_attempts(job)
        assert service.store.get_job(job)["state"] == "waiting"


@pytest.mark.parametrize("kind", ["revive", "resume"])
@pytest.mark.parametrize("binding", ["public-open", "turn-attempt"])
def test_binding_during_preparation_is_refused_inside_reservation(tmp_path, kind, binding):
    from subfleet.conversations import catalog
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        job = revive(service, harness, MINIMAL)
        if kind == "resume":
            service.store.update_job(job, kind="resume")
            manifest = service._read_json(service.root / "jobs" / job / "manifest.json")
            manifest["resume"] = {"native_session_id": MINIMAL}
            service._publish("manifest", service.root / "jobs" / job / "manifest.json", daemon_module.json_bytes(manifest))
        patch.setattr(catalog, "native_session", lambda *_args, **_kw: {
            "continuable": True, "model_value": "haiku", "permission": "read-only",
            "cwd": str(harness.workdir), "title": "Native session", "lane_id": None})
        prepare = service._prepare_route
        bound = []
        def bind(*args):
            assert not service.store._holds_writer()
            if not bound:
                if binding == "public-open":
                    bound.append(service.conversations.handle("conversation.open", {
                        "native": {"provider": "claude", "session_id": MINIMAL.upper()}}, None))
                else:
                    turn = submit_turn(service, harness, 99)
                    service.store.add_attempt(attempt_id=turn + "/a1", job_id=turn, seq=1,
                                              lane_id="claude-1", model_requested="haiku", state="succeeded",
                                              native_session_id=MINIMAL.upper())
                    bound.append(turn)
            return prepare(*args)
        patch.setattr(service, "_prepare_route", bind)
        service._admit()
        assert bound
        assert service.store.get_job(job)["state"] == "failed"
        assert service.store.get_job(job)["rc"] == 7
        assert not service.store.list_attempts(job)
        assert not any(row["holder"] == job for row in service.store.list_leases())


def accept_for_export(service, job, contents):
    attempt = service.store.list_attempts(job)[0]
    path = service.root / f"accepted-{job}.md"
    path.write_bytes(contents)
    service.store.add_artifact(attempt["attempt_id"], role="deliverable", path=str(path),
                               sha256=hashlib.sha256(contents).hexdigest(), bytes=len(contents))
    service.store.update_attempt(attempt["attempt_id"], state="succeeded")
    service.store.update_job(job, state="succeeded", accepted_attempt_id=attempt["attempt_id"])
    return attempt


@pytest.mark.parametrize("old_owner", [True, False])
def test_export_and_recovery_use_legacy_output_alias_ownership(tmp_path, old_owner):
    from subfleet.store import Store
    require_case_aliases(tmp_path)
    left, right = tmp_path / "Result.md", tmp_path / "result.md"
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        job = submit(service, harness, out_path=str(left), caller_session=None)
        service._admit()
        attempt = accept_for_export(service, job, b"OLDER\n")
        service.store.release_leases(job)
        service.store.acquire_lease(f"out:{right}", job if old_owner else "newer-job")
        right.write_bytes(b"NEWER\n")
        path = service.store.path
        service.store.close()
        service.store = Store(path)
        patch.setattr(service, "_notify", lambda: None)
        service._export(job)
        service._export(job)  # restart/replay must make the same ownership decision
        assert right.read_bytes() == (b"OLDER\n" if old_owner else b"NEWER\n")
        exports = service.store.query("SELECT * FROM artifacts WHERE attempt_id=? AND role='export'", (attempt["attempt_id"],))
        assert len(exports) == int(old_owner)


def test_two_legacy_alias_holders_oldest_exports_and_releases(tmp_path):
    require_case_aliases(tmp_path)
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        left, right = tmp_path / "Result.md", tmp_path / "result.md"
        job = submit(service, harness, out_path=str(left), caller_session=None)
        service._admit()
        accept_for_export(service, job, b"OLDER\n")
        service.store.acquire_lease(f"out:{right}", "newer-job")
        right.write_bytes(b"NEWER\n")
        service._export(job)
        assert right.read_bytes() == b"OLDER\n"
        assert not service.store.list_leases(job)
        assert {holder for _, holder in resource_leases.OutputClaim.prepare(service.store.query, str(left)).holds(service.store.query)} == {"newer-job"}


def test_output_identity_is_never_resolved_under_the_store_writer_lock(tmp_path):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        identity = folders.identity
        calls = []
        def checked(path):
            assert not service.store._holds_writer()
            calls.append(path)
            return identity(path)
        patch.setattr(folders, "identity", checked)
        job = submit(service, harness, out_path=str(tmp_path / "Result.md"), caller_session=None)
        service._admit()
        accept_for_export(service, job, b"done\n")
        service._export(job)
        assert calls and (tmp_path / "Result.md").read_bytes() == b"done\n"
