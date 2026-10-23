"""The frontend probe cache never shows a partial probe under its final name (D-KR1, #238 review r1 finding 3).

`tests/frontend/swift.py` caches each compiled probe in `build/frontend-probes/<digest>`, and a reader takes any
file there that exists. r1 measured `is_file()` true at 0 bytes during a 64 MiB `copy2`, so a hold reading the
cache while another in the same checkout filled it could copy a truncated probe. Here the compiler is a stand-in,
and every copy is made in chunks: after each chunk, every file under a final (digest) name must already be
the whole probe.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest

from tests.frontend import swift

CHUNK = 4096
FINAL = re.compile(r"[0-9a-f]{64}")


class Copies:
    """`shutil` for compile_probe, with copies made a chunk at a time and the cache checked between chunks."""

    def __init__(self, cache: Path, probe: bytes, fail_after: int | None = None):
        self.cache, self.probe, self.fail_after = cache, probe, fail_after
        self.seen: list[tuple[str, int]] = []      # (final name, its size) at every check
        self.chunks = 0

    def __getattr__(self, name):
        return getattr(shutil, name)

    def check(self):
        for path in self.cache.glob("*") if self.cache.is_dir() else ():
            if FINAL.fullmatch(path.name):
                self.seen.append((path.name, path.stat().st_size))
                assert path.read_bytes() == self.probe, f"{path.name} holds {path.stat().st_size} of {len(self.probe)} bytes"

    def copyfile(self, src, dst, *, follow_symlinks=True):
        with open(src, "rb") as source, open(dst, "wb") as target:
            while chunk := source.read(CHUNK):
                target.write(chunk)
                target.flush()
                self.chunks += 1
                if self.fail_after is not None and self.chunks > self.fail_after:
                    raise OSError(28, "No space left on device (stand-in)")
                self.check()
        self.check()
        return dst

    def copy2(self, src, dst, *, follow_symlinks=True):
        if os.path.isdir(dst):
            dst = os.path.join(dst, os.path.basename(src))
        self.copyfile(src, dst)
        shutil.copystat(src, dst)
        return dst

    copy = copy2


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A checkout with one app source and one probe; its compiler writes `checkout.probe` and counts runs."""
    sources = tmp_path / "app" / "Sources"
    sources.mkdir(parents=True)
    (sources / "App.swift").write_text("// the app\n")
    probe_source = tmp_path / "Probe.swift"
    probe_source.write_text("// the probe\n")
    monkeypatch.setattr(swift, "ROOT", tmp_path)
    monkeypatch.setattr(swift, "SOURCES", sources)
    state = SimpleNamespace(root=tmp_path, source=probe_source, cache=tmp_path / "build" / "frontend-probes",
                            probe=b"", compiles=0)

    def swiftc(command, **kwargs):
        state.compiles += 1
        Path(command[-1]).write_bytes(state.probe)
        Path(command[-1]).chmod(0o755)
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(swift, "subprocess", SimpleNamespace(run=swiftc))
    return state


def compile_with(checkout, monkeypatch, probe: bytes, binary: Path, fail_after: int | None = None) -> Copies:
    checkout.probe = probe
    copies = Copies(checkout.cache, probe, fail_after)
    monkeypatch.setattr(swift, "shutil", copies)
    swift.compile_probe(binary, checkout.source, "SUBFLEET_MODEL_TEST")
    return copies


def test_filling_the_cache_never_shows_a_partial_probe(checkout, monkeypatch, tmp_path):
    probe = os.urandom(64 * CHUNK + 17)
    copies = compile_with(checkout, monkeypatch, probe, tmp_path / "first")
    assert checkout.compiles == 1 and (tmp_path / "first").read_bytes() == probe
    assert copies.chunks > 1
    # The cache now holds exactly one whole probe under its final name, and nothing else.
    assert whole(checkout.cache, probe)


def whole(cache: Path, probe: bytes) -> bool:
    """The cache folder holds one file, under a final name, and it is the whole probe."""
    files = list(cache.iterdir())
    return len(files) == 1 and FINAL.fullmatch(files[0].name) is not None and files[0].read_bytes() == probe


def test_a_reader_takes_the_cached_probe_without_compiling(checkout, monkeypatch, tmp_path):
    probe = os.urandom(8 * CHUNK)
    compile_with(checkout, monkeypatch, probe, tmp_path / "first")
    second = compile_with(checkout, monkeypatch, probe, tmp_path / "second")
    assert checkout.compiles == 1
    assert (tmp_path / "second").read_bytes() == probe
    assert os.access(tmp_path / "second", os.X_OK)
    assert second.seen and all(size == len(probe) for _, size in second.seen)


def test_a_fill_that_fails_leaves_no_file_behind(checkout, monkeypatch, tmp_path):
    probe = os.urandom(8 * CHUNK)
    with pytest.raises(OSError):
        compile_with(checkout, monkeypatch, probe, tmp_path / "first", fail_after=3)
    assert list(checkout.cache.iterdir()) == []
    # The next hold compiles again and fills the cache.
    compile_with(checkout, monkeypatch, probe, tmp_path / "again")
    assert checkout.compiles == 2 and len(list(checkout.cache.iterdir())) == 1


@settings(max_examples=40, derandomize=True, database=None, deadline=None)
@given(size=st.integers(0, 6 * CHUNK), fail_after=st.none() | st.integers(0, 6))
def test_no_fill_ever_shows_a_partial_probe(tmp_path_factory, size, fail_after):
    """For every probe size and any copy failure point: a final name holds the whole probe or nothing."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        root = tmp_path_factory.mktemp("probe-cache")
        sources = root / "app" / "Sources"
        sources.mkdir(parents=True)
        (sources / "App.swift").write_text("// the app\n")
        (root / "Probe.swift").write_text("// the probe\n")
        monkeypatch.setattr(swift, "ROOT", root)
        monkeypatch.setattr(swift, "SOURCES", sources)
        probe = bytes(index % 251 for index in range(size))

        def swiftc(command, **kwargs):
            Path(command[-1]).write_bytes(probe)
            return subprocess.CompletedProcess(command, 0, "", "")
        monkeypatch.setattr(swift, "subprocess", SimpleNamespace(run=swiftc))
        copies = Copies(root / "build" / "frontend-probes", probe, fail_after)
        monkeypatch.setattr(swift, "shutil", copies)
        try:
            swift.compile_probe(root / "binary", root / "Probe.swift", "SUBFLEET_MODEL_TEST")
            failed = False
        except OSError:
            failed = True
        cache = root / "build" / "frontend-probes"
        assert (list(cache.iterdir()) == []) if failed else whole(cache, probe)
        assert failed == (fail_after is not None and copies.chunks > fail_after)
