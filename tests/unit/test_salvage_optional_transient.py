"""Optional Git probes must distinguish a missing object from host pressure."""

import os
import subprocess

import pytest

from subfleet import salvage


def nested_head(path):
    return salvage._head_status(os.fsencode(path / ".git"), {}, None)


@pytest.mark.parametrize("probe", [salvage.git_head, salvage.git_toplevel, nested_head],
                         ids=["head", "toplevel", "nested-head"])
@pytest.mark.parametrize("stderr", [
    b"fatal: cannot read object: No space left on device\n",
    b"fatal: Out of memory, malloc failed (tried to allocate 123 bytes)\n",
])
def test_optional_probe_raises_on_transient_git_failure(tmp_path, monkeypatch, probe, stderr):
    """A full disk or OOM has not established that the checkout or its HEAD is absent.
    Treating it as absent can admit a writable attempt with no baseline snapshot."""
    monkeypatch.setattr(salvage.subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 128, b"", stderr))
    with pytest.raises(salvage.SalvageError) as caught:
        probe(tmp_path)
    assert caught.value.transient


@pytest.mark.parametrize("probe,missing", [(salvage.git_head, None), (salvage.git_toplevel, None),
                                          (nested_head, 128)], ids=["head", "toplevel", "nested-head"])
def test_optional_probe_still_reports_a_missing_repository(tmp_path, monkeypatch, probe, missing):
    monkeypatch.setattr(salvage.subprocess, "run", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 128, b"", b"fatal: not a git repository\n"))
    assert probe(tmp_path) == missing
