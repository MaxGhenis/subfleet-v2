"""Review r4: the new owner must be prepared before acquiring its descriptor."""
from __future__ import annotations

import dis
import os

import pytest

from subfleet import retention_fs as rfs
from tests.unit.test_retention_descriptor_review import raise_at


@pytest.mark.xfail(
    strict=True,
    reason="P2-6 (review r4): owner construction/entry after open leaks the descriptor",
)
@pytest.mark.parametrize("phase", ["construction", "entry"])
@pytest.mark.parametrize("fault", ["memory", "keyboard"])
def test_owner_setup_failure_closes_acquired_file(tmp_path, monkeypatch, phase, fault):
    path = tmp_path / "file"
    path.write_bytes(b"scratch")
    injected = MemoryError("owner setup") if fault == "memory" else KeyboardInterrupt("owner setup")
    method = rfs._cleanup.__init__ if phase == "construction" else rfs._cleanup.__enter__
    offset = next(instruction.offset for instruction in dis.get_instructions(method)
                  if instruction.opname != "RESUME")
    original_open, original_close = os.open, os.close
    opened, attempted = [], []

    def open_file(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def close(fd):
        attempted.append(fd)
        original_close(fd)

    def still_open():
        remaining = []
        for fd in opened:
            try:
                os.fstat(fd)
            except OSError:
                continue
            remaining.append(fd)
        return remaining

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", open_file)
            patch.setattr(os, "close", close)
            with raise_at(method.__code__, offset, injected) as fired:
                with pytest.raises(type(injected)) as caught:
                    rfs.read_regular(path)
        assert fired and caught.value is injected
        assert not still_open(), ("acquired descriptor has no owner", opened, attempted)
    finally:
        for fd in still_open():
            original_close(fd)
