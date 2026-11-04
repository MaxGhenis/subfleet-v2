"""C-31.17: the credit key's frame, from the daemon through the guardian to the harness.

Properties (Hypothesis) and examples:

- Round trip: for any key of 1 to 4096 printable ASCII characters, `key_frame.encode`,
  a pipe, `read_frame` and `decode` give the key back, and `Daemon._open_gate` writes
  exactly the release byte and that frame.
- The real chain: the daemon's own gate writer (`Daemon._open_gate`) opens a real
  guardian's launch gate; the guardian relays the key on a pipe of its own, and a
  provider that reads it with the harness's own reader (`harness.read_key`) gets it
  unchanged, with the descriptor closed after the read. The guardian holds no pipe
  while its provider runs.
- Refusal: a frame cut anywhere short, an empty frame, an oversized one, a missing
  release, or `--key-frame` without a launch gate ends the guardian with 127 before it
  creates the attempt folder, writes any file or starts its provider.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import key_frame
from subfleet.daemon import Daemon
from tests.fake import proc_blocks

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the guardian's process checks are macOS's")

REPO = Path(__file__).resolve().parents[2]
keys = st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), min_size=1,
               max_size=key_frame.MAX_KEY_BYTES)
PROPERTY = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
                    max_examples=50)
CHAIN = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
                 max_examples=12)

#: The provider: reads its key with the harness's reader, records what it got (a digest,
#: never the key) and whether the descriptor is closed, then waits for `release` if named.
PROVIDER = """import hashlib, json, os, sys, time
from subfleet.api_agent.harness import read_key
fd = int(sys.argv[sys.argv.index("--key-fd") + 1]) if "--key-fd" in sys.argv else None
key, problem = read_key(fd)
try:
    os.fstat(fd)
    closed = False
except (OSError, TypeError):
    closed = True
out = {"sha256": hashlib.sha256(key.encode()).hexdigest() if key else None,
       "length": len(key) if key else None, "problem": problem, "closed": closed}
open(sys.argv[1], "w").write(json.dumps(out))
release = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "--key-fd" else None
deadline = time.monotonic() + 60
while release and not os.path.exists(release) and time.monotonic() < deadline:
    time.sleep(0.02)
"""


def read_back(data: bytes | bytearray) -> bytearray:
    read_end, write_end = os.pipe()
    try:
        key_frame.write_all(write_end, data)
    finally:
        os.close(write_end)
    try:
        return key_frame.read_frame(read_end)
    finally:
        os.close(read_end)


@PROPERTY
@given(keys)
@example("k")
@example("~" * key_frame.MAX_KEY_BYTES)
def test_any_printable_key_round_trips_through_a_frame(key):
    framed = key_frame.encode(key)
    assert bytes(framed[:4]) == len(key).to_bytes(4, "big") and bytes(framed[4:]) == key.encode()
    assert key_frame.decode(read_back(framed)) == key
    read_end, write_end = os.pipe()
    try:
        Daemon._open_gate(write_end, key)
    finally:
        os.close(write_end)
    try:
        assert os.read(read_end, 1 + 4 + len(key) + 1) == b"1" + len(key).to_bytes(4, "big") + key.encode()
    finally:
        os.close(read_end)


@pytest.mark.parametrize("key", ["", "x" * (key_frame.MAX_KEY_BYTES + 1), "café", "a\nb", "\x00", "tab\there"])
def test_a_key_that_is_not_printable_ascii_of_1_to_4096_bytes_has_no_frame(key):
    with pytest.raises(key_frame.FrameError) as caught:
        key_frame.encode(key)
    assert key == "" or key not in str(caught.value)


@PROPERTY
@given(st.data())
def test_a_frame_cut_short_never_reads_as_a_key(data):
    framed = bytes(key_frame.encode(data.draw(keys)))
    cut = data.draw(st.integers(min_value=0, max_value=len(framed) - 1))
    with pytest.raises(key_frame.FrameError) as caught:
        read_back(framed[:cut])
    assert "ended inside" in str(caught.value)


@pytest.mark.parametrize("header", [0, key_frame.MAX_KEY_BYTES + 1, 2 ** 32 - 1])
def test_an_empty_or_oversized_length_is_refused_before_any_key_byte_is_read(header):
    with pytest.raises(key_frame.FrameError):
        read_back(header.to_bytes(4, "big") + b"k" * 32)


class Chain:
    """One real guardian, its launch gate and a provider that reads the key."""

    def __init__(self, root: Path, *, key_frame_flag: bool = True, gate: bool = True,
                 release: Path | None = None) -> None:
        self.root = root
        self.attempt = root / "attempt"
        self.result = root / "provider.json"
        provider = root / "provider.py"
        provider.write_text(PROVIDER)
        self.read_fd, self.write_fd = (os.pipe() if gate else (None, None))
        command = [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(self.attempt), "--cwd", str(root),
                   "--stdout-path", str(root / "stdout"), "--stderr-path", str(root / "stderr")]
        if gate:
            command += ["--launch-fd", str(self.read_fd)]
        if key_frame_flag:
            command.append("--key-frame")
        command += ["--", sys.executable, "-I", str(provider), str(self.result),
                    *([str(release)] if release else [])]
        environment = {name: value for name, value in os.environ.items() if name != "ANTHROPIC_API_KEY"}
        self.process = subprocess.Popen(command, cwd=REPO, env=environment, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        pass_fds=(self.read_fd,) if gate else ())
        if gate:
            os.close(self.read_fd)

    def send(self, data: bytes | None = None, *, key: str | None = None) -> None:
        """Open the gate as the daemon does (`key`), or write raw bytes (`data`)."""
        try:
            if key is not None:
                Daemon._open_gate(self.write_fd, key)
            elif data:
                key_frame.write_all(self.write_fd, data)
        finally:
            os.close(self.write_fd)

    def wait(self) -> int:
        try:
            return self.process.wait(timeout=120)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()

    def provider_ran(self) -> dict | None:
        return json.loads(self.result.read_text()) if self.result.exists() else None

    def files(self) -> list[str]:
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")
                      if path.name not in ("provider.py",))


@CHAIN
@given(keys)
@example("x")
@example("Z" * key_frame.MAX_KEY_BYTES)
@example("sk-ant-api03-" + "A1b2C3d4" * 10 + "-_Zz")
def test_any_key_reaches_the_harness_reader_unchanged_through_a_real_guardian(tmp_path_factory, key):
    """Daemon (`_open_gate`) -> guardian (`--key-frame`) -> provider (`--key-fd`,
    `harness.read_key`): the provider gets exactly the key, and its descriptor closes."""
    chain = Chain(tmp_path_factory.mktemp("chain"))
    chain.send(key=key)
    assert chain.wait() == 0
    got = chain.provider_ran()
    assert got == {"sha256": hashlib.sha256(key.encode()).hexdigest(), "length": len(key), "problem": None,
                   "closed": True}


def test_the_guardian_holds_no_pipe_while_its_provider_runs(tmp_path):
    """C-31.17, mutant (d): the guardian closes its end of the key's pipe, and its gate,
    once the provider is spawned, so while the provider runs it holds no pipe at all."""
    release = tmp_path / "release"
    chain = Chain(tmp_path, release=release)
    key = "sk-ant-api03-" + "Q" * 60
    chain.send(key=key)
    deadline = time.monotonic() + 60
    while chain.provider_ran() is None:
        assert time.monotonic() < deadline and chain.process.poll() is None, "the provider never ran"
        time.sleep(0.02)
    held = proc_blocks.descriptors(chain.process.pid)
    release.touch()
    assert chain.wait() == 0
    assert held, "the guardian's descriptors could not be read"
    assert [fd for fd, kind in held if kind == proc_blocks.PIPE] == [], held
    assert chain.provider_ran()["problem"] is None


TRUNCATIONS = [b"", b"1", b"1\x00\x00", b"1" + (40).to_bytes(4, "big"),
               b"1" + (40).to_bytes(4, "big") + b"sk-ant-api03-short"]


@pytest.mark.parametrize("data", [*TRUNCATIONS, b"1" + bytes(4), b"1" + (4097).to_bytes(4, "big") + b"k" * 64,
                                  b"0" + (3).to_bytes(4, "big") + b"abc"],
                         ids=["nothing", "release-only", "half-length", "length-only", "short-key", "zero-length",
                              "oversized", "no-release"])
def test_a_bad_gate_or_frame_refuses_before_anything_starts(tmp_path, data):
    """C-31.17: on EOF, a short read, a zero or oversized length, or no release, the
    guardian exits 127 and leaves nothing: no attempt folder, no output file, no
    provider run."""
    chain = Chain(tmp_path)
    chain.send(data)
    assert chain.wait() == 127
    assert chain.provider_ran() is None
    assert chain.files() == []


@CHAIN
@given(st.data())
def test_any_truncated_frame_refuses_before_anything_starts(tmp_path_factory, data):
    key = data.draw(st.text(alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E), min_size=1,
                            max_size=200))
    framed = b"1" + bytes(key_frame.encode(key))
    cut = data.draw(st.integers(min_value=1, max_value=len(framed) - 1))
    chain = Chain(tmp_path_factory.mktemp("cut"))
    chain.send(framed[:cut])
    assert chain.wait() == 127
    assert chain.provider_ran() is None and chain.files() == []


def test_a_key_frame_without_a_launch_gate_refuses(tmp_path):
    chain = Chain(tmp_path, gate=False)
    assert chain.wait() == 127
    assert chain.provider_ran() is None and chain.files() == []
