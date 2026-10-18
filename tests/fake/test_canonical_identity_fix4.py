"""Output-loop refusal is independent of pathlib's Python-version behavior."""
import errno
from pathlib import Path

import pytest

from subfleet import protocol
from subfleet.adapters.base import AdapterError
from tests.fake.test_admission_latency import fleet_daemon, submit
from tests.fake.test_canonical_identity_fix2 import configure

FIX = "remove the symlink loop or choose a different -o path"


@pytest.mark.parametrize("spelling", [
    "loop/result.md", "loop", "Missing/../loop/result.md", "Missing/../loop",
    "Missing/../loop/absent/result.md", "Missing/../loop/../result.md",
], ids=["parent", "final", "missing-parent", "missing-final", "missing-child", "missing-dotdot"])
def test_submit_refuses_loop_at_every_output_position(tmp_path, spelling):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        with pytest.raises(AdapterError) as refused:
            submit(service, harness, out_path=str(tmp_path / spelling), caller_session=None)
        assert refused.value.code == 7
        assert refused.value.fix == FIX
        assert not service.store.list_jobs()
        assert not service.store.list_leases()


def test_submit_refuses_loop_when_pathlib_suppresses_it(tmp_path):
    """Model 3.14's suppression through later digest resolution on 3.12 too."""
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    requested = tmp_path / "Missing" / ".." / "loop"
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        real = Path.resolve

        def modern_resolve(path, strict=False):
            if path == requested:
                if strict:
                    raise FileNotFoundError(errno.ENOENT, "missing component", str(tmp_path / "Missing"))
                return loop
            if path == loop and not strict:
                return loop
            return real(path, strict=strict)

        patch.setattr(Path, "resolve", modern_resolve)
        with pytest.raises(AdapterError) as refused:
            submit(service, harness, out_path=str(requested), caller_session=None)
        assert refused.value.code == 7
        assert refused.value.fix == FIX
        assert not service.store.list_jobs()


def test_submit_resolves_output_without_pathlib_fallback(tmp_path):
    requested = tmp_path / "alias"
    requested.symlink_to("result.md")
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        real = Path.resolve

        def old_resolve(path, strict=False):
            if path == requested:
                raise AssertionError("output must use the component resolver")
            return real(path, strict=strict)

        patch.setattr(Path, "resolve", old_resolve)
        job = submit(service, harness, out_path=str(requested), caller_session=None)
        assert service.store.get_job(job)["out_path"] == str(tmp_path / "result.md")


def test_submit_checks_resolved_final_link_after_raw_missing_prefix(tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "Missing" / ".." / "loop")
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        with pytest.raises(AdapterError) as refused:
            submit(service, harness, out_path=str(alias), caller_session=None)
        assert refused.value.code == 7
        assert refused.value.fix == FIX
        assert not service.store.list_jobs()


def test_submit_keeps_symlink_parent_dotdot_and_dangling_output_semantics(tmp_path):
    target = tmp_path / "real" / "nested"
    target.mkdir(parents=True)
    (tmp_path / "alias").symlink_to(target)
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    requested = tmp_path / "alias" / ".." / "loop"
    (tmp_path / "dangling").symlink_to(tmp_path / "Future.md")
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        job = submit(service, harness, out_path=str(requested), caller_session=None)
        assert service.store.get_job(job)["out_path"] == str(tmp_path / "real" / "loop")
        job = submit(service, harness, out_path=str(tmp_path / "dangling"), caller_session=None)
        assert service.store.get_job(job)["out_path"] == str(tmp_path / "Future.md")


def test_submit_validation_does_not_leak_workdir_loop_runtime_error(tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        with pytest.raises(protocol.ProtocolError):
            submit(service, harness, workdir=str(loop), caller_session=None)
        assert not service.store.list_jobs()
