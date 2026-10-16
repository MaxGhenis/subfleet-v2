"""A mutation kill requires a passing control and restored production bytes."""

import subprocess

import pytest

from tools import quarantine_mutations as mutations


@pytest.mark.parametrize("control,mutant,label,exit_code,calls", [
    ((1, "AssertionError\n1 failed"), None, "NO CONTROL", 1, 1),
    ((0, "1 skipped"), None, "NO CONTROL", 1, 1),
    ((0, "1 passed"), (1, "AssertionError\n1 failed"), "KILLED", 0, 2),
    ((0, "1 passed"), (0, "1 passed"), "SURVIVED", 1, 2),
    ((0, "1 passed"), (2, "collection error"), "INCONCLUSIVE", 1, 2),
    ((0, "1 passed"), None, "INCONCLUSIVE", 1, 2),
])
def test_control_precedes_mutation_and_source_is_restored(
        tmp_path, monkeypatch, capsys, control, mutant, label, exit_code, calls):
    source = tmp_path / "production.py"
    original = b"value = True\r\n"
    source.write_bytes(original)
    monkeypatch.setattr(mutations, "ROOT", tmp_path)
    monkeypatch.setattr(mutations, "MUTATIONS", (
        ("example", "production.py", "True", "False", "test.py::test_example"),))
    monkeypatch.setattr(mutations.sys, "argv", ["quarantine_mutations.py"])
    seen = []

    def run(argv, **kwargs):
        seen.append(source.read_bytes())
        answer = control if len(seen) == 1 else mutant
        if answer is None:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, answer[0], answer[1], "")

    monkeypatch.setattr(mutations.subprocess, "run", run)
    assert mutations.main() == exit_code
    assert len(seen) == calls and seen[0] == original
    if calls == 2:
        assert seen[1] == b"value = False\r\n"
    assert source.read_bytes() == original
    output = capsys.readouterr().out
    assert f"{label}: example" in output
    assert ("KILLED: example" in output) == (label == "KILLED")
