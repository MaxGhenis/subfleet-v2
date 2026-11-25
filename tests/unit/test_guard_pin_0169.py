"""D-CODEX-0169's install kit (tools/guard_pin_0169.py), on temporary folders only.

The kit derives the installed TRUST's 0.169.1 pin and refuses unless it equals the TRUST of the
checkout it runs from, but for hook_sha256. Its docstring says to run it from #333's checkout
(024b23f80), whose TRUST is 9fb9bcf7.... Since release/217 merged into the guard sync (#303), this
repository's TRUST also carries the guard sync's sentences and its hook hash, so the kit refuses it
(test_the_kit_refuses_this_repositorys_trust). Every other test rebuilds #333's TRUST from this
repository's (the guard sync's sentences removed, its hook hash put back) and hands it to the kit
through its repo_trust parameter: that default is bound when the kit loads, so patching
kit.REPO_TRUST would not redirect it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("guard_pin_0169", ROOT / "tools/guard_pin_0169.py")
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)

PR333_HOOK_SHA256 = "af5e723943b1f7462c19a4cbc5007e70b3c228c050c30f470f204d2f81c1398f"
PR333_TRUST_SHA256 = "9fb9bcf7beecb09d79e2ce0729c807ef08577fa827121d8281a5ce14017cb7d2"
GUARD_SYNC_START = " 2026-11-11, guard sync ("
WHEN = datetime(2026, 11, 18, 3, 0, 0, tzinfo=UTC)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pr333_text() -> str:
    """#333's TRUST, rebuilt from the repository's: the guard sync's sentences removed (its four before
    D-CODEX-0169's sentence, and any after it), and the hook hash put back to #333's."""
    text = kit.REPO_TRUST.read_text(encoding="utf-8")
    assert text.count(GUARD_SYNC_START) == 1 and text.count(kit.SENTENCE_START) == 1 and text.endswith(kit.TAIL)
    start, d0169 = text.index(GUARD_SYNC_START), text.index(kit.SENTENCE_START)
    assert start < d0169
    later = re.search(r" \d{4}-\d{2}-\d{2}, guard sync ", text[d0169:])
    end = d0169 + later.start() if later else len(text) - len(kit.TAIL)
    text = text[:start] + text[d0169:end] + kit.TAIL
    hook = json.loads(text)["hook_sha256"]
    assert text.count(hook) == 1
    return text.replace(hook, PR333_HOOK_SHA256)


@pytest.fixture(scope="module")
def pr333(tmp_path_factory) -> Path:
    """#333's TRUST as a file, for the kit's repo_trust parameter."""
    text = pr333_text()
    assert sha(text.encode()) == PR333_TRUST_SHA256
    path = tmp_path_factory.mktemp("pr333") / "TRUST"
    path.write_text(text, encoding="utf-8")
    return path


def installed_text(pr333: Path) -> str:
    """The installed TRUST, rebuilt from #333's: the D-CODEX-0169 edits undone, the installed hook's hash."""
    repo, sentence = kit.repo_trust_and_sentence(pr333)
    text = repo.replace(sentence, "").replace(kit.NEW_VERSION_LINE, kit.OLD_VERSION_LINE)
    return text.replace(PR333_HOOK_SHA256, kit.LIVE_HOOK_SHA256)


def verified(*_args, **_kwargs):
    return SimpleNamespace(ok=True, kind="verified", version="codex-cli 0.169.1", message="")


def refused(*_args, **_kwargs):
    return SimpleNamespace(ok=False, kind="version", version="codex-cli 0.159.0", message="pin differs")


def staged(live: Path, stage: Path, run=verified) -> int:
    return kit.stage(live, stage, codex="codex", lane_homes=["lane-a", "lane-b"], preflight_function=run,
                     repo_trust=live.parent / "pr333-TRUST")


def swapped(live: Path, stage: Path) -> int:
    return kit.swap(live, stage, repo_trust=live.parent / "pr333-TRUST")


def test_the_rebuilt_installed_trust_is_the_installed_one_and_derives_to_the_repository_pin(pr333):
    text = installed_text(pr333)
    assert sha(text.encode()) == kit.LIVE_TRUST_SHA256
    derived = kit.derive(text, *kit.repo_trust_and_sentence(pr333))
    trust = json.loads(derived)
    assert trust["codex_version"] == "codex-cli 0.169.1" and trust["hook_sha256"] == kit.LIVE_HOOK_SHA256
    assert derived.replace(kit.LIVE_HOOK_SHA256, PR333_HOOK_SHA256) == pr333.read_text(encoding="utf-8")
    assert sha(derived.encode()) == kit.INSTALLED_TRUST_SHA256
    assert sha(kit.derived_bytes(text.encode(), pr333)) == kit.INSTALLED_TRUST_SHA256


def test_the_kit_refuses_this_repositorys_trust(pr333):
    """At this head the repository's TRUST carries the guard sync's sentences, so the kit fails closed."""
    with pytest.raises(kit.Refusal, match="more than hook_sha256"):
        kit.derived_bytes(installed_text(pr333).encode(), kit.REPO_TRUST)


@pytest.mark.parametrize("edit", [
    lambda t: t.replace(kit.OLD_VERSION_LINE, kit.NEW_VERSION_LINE),             # already moved
    lambda t: t.replace("installed only after", "installed after"),             # other provenance
    lambda t: t.replace('"override"', '"override "'),                           # other field
])
def test_derive_refuses_any_other_installed_trust(edit, pr333):
    with pytest.raises(kit.Refusal):
        kit.derive(edit(installed_text(pr333)), *kit.repo_trust_and_sentence(pr333))


def test_derive_refuses_a_repository_trust_without_one_hook_hash_and_bytes_that_are_not_utf8(pr333):
    repo, sentence = kit.repo_trust_and_sentence(pr333)
    with pytest.raises(kit.Refusal):
        kit.derive(installed_text(pr333), repo.replace('"hook_sha256"', '"hook"'), sentence)
    with pytest.raises(kit.Refusal):
        kit.derived_bytes(b"\xff" + installed_text(pr333).encode(), pr333)


@pytest.fixture
def live(tmp_path, monkeypatch, pr333):
    hook = b"#!/bin/sh\nexit 0\n"
    monkeypatch.setattr(kit, "LIVE_HOOK_SHA256", sha(hook))
    text = installed_text(pr333)                # carries the patched hook hash
    monkeypatch.setattr(kit, "LIVE_TRUST_SHA256", sha(text.encode()))
    monkeypatch.setattr(kit, "INSTALLED_TRUST_SHA256", sha(kit.derived_bytes(text.encode(), pr333)))
    (tmp_path / "pr333-TRUST").write_bytes(pr333.read_bytes())   # staged() and swapped() read it here
    directory = tmp_path / "guard"
    directory.mkdir(mode=0o700)
    (directory / "never-rules-hook.sh").write_bytes(hook)
    (directory / "TRUST").write_text(text)
    return directory


def test_stage_swap_and_rollback(live, tmp_path):
    before = (live / "TRUST").read_bytes()
    stage = tmp_path / "stage"
    assert staged(live, stage) == 0
    assert (live / "TRUST").read_bytes() == before and not (live / kit.BACKUP).exists()
    new = (stage / "guard/TRUST").read_bytes()
    assert json.loads(new)["codex_version"] == "codex-cli 0.169.1"
    record = json.loads((stage / kit.PREFLIGHT).read_text())
    assert record["trust_sha256"] == sha(new) and record["lanes"] == ["lane-a", "lane-b"]
    assert swapped(live, stage) == 0
    assert (live / "TRUST").read_bytes() == new and (live / kit.BACKUP).read_bytes() == before
    assert oct((live / "TRUST").stat().st_mode & 0o777) == "0o600"
    with pytest.raises(kit.Refusal):            # the live pair moved: a second swap refuses
        swapped(live, stage)
    assert kit.rollback(live, now=WHEN) == 0
    assert (live / "TRUST").read_bytes() == before and not (live / kit.BACKUP).exists()
    assert (live / f"{kit.BACKUP}.rolled-back-20261118T030000Z").read_bytes() == before
    assert staged(live, tmp_path / "stage-2") == 0 and swapped(live, tmp_path / "stage-2") == 0   # a retry runs


def test_stage_refuses_an_existing_folder_a_folder_inside_live_and_a_moved_live_pair(live, tmp_path, pr333):
    (tmp_path / "stage").mkdir()
    with pytest.raises(kit.Refusal):
        staged(live, tmp_path / "stage")
    with pytest.raises(kit.Refusal):
        staged(live, live / "stage")
    assert not (live / "stage").exists()
    with pytest.raises(kit.Refusal):
        kit.stage(live, tmp_path / "no-lanes", codex="codex", lane_homes=[], preflight_function=verified,
                  repo_trust=pr333)
    (live / "TRUST").write_text((live / "TRUST").read_text() + " ")
    with pytest.raises(kit.Refusal):
        staged(live, tmp_path / "other")
    assert not (tmp_path / "other").exists() and not (tmp_path / "no-lanes").exists()


def test_stage_refuses_a_moved_hook(live, tmp_path):
    (live / "never-rules-hook.sh").write_bytes(b"#!/bin/sh\nexit 1\n")
    with pytest.raises(kit.Refusal):
        staged(live, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_stage_imports_the_preflight_before_it_writes(live, tmp_path, monkeypatch, pr333):
    def missing():
        raise ModuleNotFoundError("No module named 'subfleet'")
    monkeypatch.setattr(kit, "_preflight_function", missing)
    with pytest.raises(ModuleNotFoundError):
        kit.stage(live, tmp_path / "stage", codex="codex", lane_homes=["lane-a"], repo_trust=pr333)
    assert not (tmp_path / "stage").exists()


def test_a_lane_that_does_not_verify_leaves_no_preflight_record_and_swap_refuses(live, tmp_path):
    calls = []

    def one_refuses(codex, *, home, **_kwargs):
        calls.append(home)
        return refused() if home == "lane-b" else verified()
    stage = tmp_path / "stage"
    assert staged(live, stage, one_refuses) == 1 and calls == ["lane-a", "lane-b"]
    assert not (stage / kit.PREFLIGHT).exists()
    with pytest.raises(kit.Refusal):
        swapped(live, stage)
    assert not (live / kit.BACKUP).exists()


def test_swap_refuses_a_stage_without_a_preflight(live, tmp_path, pr333):
    stage = tmp_path / "stage"
    assert kit.stage(live, stage, codex=None, lane_homes=[], repo_trust=pr333) == 0
    with pytest.raises(kit.Refusal):
        swapped(live, stage)
    assert not (live / kit.BACKUP).exists()


def test_swap_refuses_a_changed_stage_and_an_existing_backup(live, tmp_path):
    stage = tmp_path / "stage"
    staged(live, stage)
    (stage / "guard/TRUST").write_bytes((stage / "guard/TRUST").read_bytes() + b" ")
    with pytest.raises(kit.Refusal):            # the recorded digest no longer matches
        swapped(live, stage)
    changed = sha((stage / "guard/TRUST").read_bytes())
    (stage / "TRUST.sha256").write_text(changed + "\n")
    record = json.loads((stage / kit.PREFLIGHT).read_text())
    (stage / kit.PREFLIGHT).write_text(json.dumps({**record, "trust_sha256": changed}))
    with pytest.raises(kit.Refusal, match="derivation"):   # digest and record match; the bytes are not the derivation
        swapped(live, stage)
    assert not (live / kit.BACKUP).exists()
    (live / kit.BACKUP).write_text("x")
    other = tmp_path / "other"
    staged(live, other)
    with pytest.raises(kit.Refusal):
        swapped(live, other)
    with pytest.raises(kit.Refusal):            # a backup that is not the pre-move TRUST is never restored
        kit.rollback(live)


def test_swap_refuses_a_moved_hook(live, tmp_path):
    stage = tmp_path / "stage"
    staged(live, stage)
    (live / "never-rules-hook.sh").write_bytes(b"#!/bin/sh\nexit 1\n")
    with pytest.raises(kit.Refusal):
        swapped(live, stage)
    assert not (live / kit.BACKUP).exists()


def test_rollback_after_a_swap_that_stopped_before_its_rename_frees_the_retry(live, tmp_path):
    before = (live / "TRUST").read_bytes()
    (live / kit.BACKUP).write_bytes(before)      # what swap leaves when _replace fails
    assert kit.rollback(live, now=WHEN) == 0
    assert (live / "TRUST").read_bytes() == before and not (live / kit.BACKUP).exists()
    assert staged(live, tmp_path / "stage") == 0 and swapped(live, tmp_path / "stage") == 0


def test_rollback_refuses_once_a_later_guard_change_owns_the_pair(live, tmp_path):
    staged(live, tmp_path / "stage")
    swapped(live, tmp_path / "stage")
    installed = (live / "TRUST").read_bytes()
    later = installed.replace(b"installed only after", b"installed later after")
    (live / "TRUST").write_bytes(later)          # a later TRUST, same hook
    with pytest.raises(kit.Refusal):
        kit.rollback(live)
    assert (live / "TRUST").read_bytes() == later and (live / kit.BACKUP).exists()
    (live / "TRUST").write_bytes(installed)
    (live / "never-rules-hook.sh").write_bytes(b"#!/bin/sh\nexit 1\n")   # a later hook
    with pytest.raises(kit.Refusal):
        kit.rollback(live)
    assert (live / "TRUST").read_bytes() == installed and (live / kit.BACKUP).exists()


def test_rollback_refuses_without_a_backup(live):
    with pytest.raises(kit.Refusal):
        kit.rollback(live)


def test_main_maps_a_refusal_to_2_and_an_os_error_to_3(live, tmp_path, capsys):
    assert kit.main(["rollback", "--live", str(live)]) == 2
    assert "refused:" in capsys.readouterr().err
    assert kit.main(["swap", "--live", str(live), "--stage", str(tmp_path / "missing")]) == 3
    assert "error:" in capsys.readouterr().err


def test_stage_refuses_a_derivation_other_than_the_pinned_one(live, tmp_path, monkeypatch):
    monkeypatch.setattr(kit, "INSTALLED_TRUST_SHA256", "0" * 64)
    with pytest.raises(kit.Refusal, match="pinned"):
        staged(live, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_swap_refuses_a_stage_whose_digest_is_not_the_pin(live, tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    staged(live, stage)
    monkeypatch.setattr(kit, "INSTALLED_TRUST_SHA256", "0" * 64)
    with pytest.raises(kit.Refusal, match="pinned"):
        swapped(live, stage)
    assert not (live / kit.BACKUP).exists()


def test_rollback_reads_nothing_from_the_checkout(live, tmp_path, monkeypatch):
    before = (live / "TRUST").read_bytes()
    staged(live, tmp_path / "stage")
    swapped(live, tmp_path / "stage")

    def checkout_moved(*_args, **_kwargs):
        raise AssertionError("rollback read the checkout's TRUST")
    monkeypatch.setattr(kit, "repo_trust_and_sentence", checkout_moved)
    monkeypatch.setattr(kit, "REPO_TRUST", tmp_path / "missing")
    assert kit.rollback(live, now=WHEN) == 0
    assert (live / "TRUST").read_bytes() == before


def test_a_failed_backup_write_leaves_no_backup_and_the_retry_runs(live, tmp_path, monkeypatch):
    before = (live / "TRUST").read_bytes()
    stage = tmp_path / "stage"
    staged(live, stage)
    real_fsync = kit.os.fsync

    def full_disk(_fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(kit.os, "fsync", full_disk)
    with pytest.raises(OSError):
        swapped(live, stage)
    assert sorted(p.name for p in live.iterdir()) == ["TRUST", "never-rules-hook.sh"]
    assert (live / "TRUST").read_bytes() == before
    monkeypatch.setattr(kit.os, "fsync", real_fsync)
    assert swapped(live, stage) == 0 and (live / kit.BACKUP).read_bytes() == before


def test_a_preflight_that_raises_leaves_its_lane_unverified(live, tmp_path):
    def b_raises(codex, *, home, **_kwargs):
        if home == "lane-b":
            raise RuntimeError("app-server exited")
        return verified()
    stage = tmp_path / "stage"
    assert staged(live, stage, b_raises) == 1
    assert not (stage / kit.PREFLIGHT).exists()


def test_stage_hands_the_preflight_arguments_its_signature_accepts(live, tmp_path):
    """Binds against the preflight this test run imports (the checkout's); the 22:16Z dry run used the installed one."""
    preflight = pytest.importorskip("subfleet.guard.preflight").preflight
    calls = []

    def record(codex, **kwargs):
        calls.append((codex, kwargs))
        return verified()
    stage = tmp_path / "stage"
    assert staged(live, stage, record) == 0 and [kwargs["home"] for _codex, kwargs in calls] == ["lane-a", "lane-b"]
    for codex, kwargs in calls:
        inspect.signature(preflight).bind(codex, **kwargs)
        assert kwargs["hook_path"] == live / "never-rules-hook.sh" and kwargs["trust_path"] == stage / "guard/TRUST"
        assert kwargs["use_cache"] is False and kwargs["state_root"] == stage
        assert kwargs["workdir"].parent == stage and kwargs["cache_directory"].parent == stage


def test_main_wires_stage_and_maps_a_missing_runtime_to_3(live, tmp_path, monkeypatch, capsys, pr333):
    # main passes no repo_trust, so stage uses its keyword default, bound when the kit loaded
    monkeypatch.setitem(kit.stage.__kwdefaults__, "repo_trust", pr333)
    monkeypatch.setattr(kit, "_preflight_function", lambda: verified)
    stage = tmp_path / "stage"
    assert kit.main(["stage", "--live", str(live), "--stage", str(stage), "--codex", "codex",
                     "--lane-home", "lane-a", "--lane-home", "lane-b"]) == 0
    assert json.loads((stage / kit.PREFLIGHT).read_text())["lanes"] == ["lane-a", "lane-b"]

    def missing():
        raise ModuleNotFoundError("No module named 'subfleet'")
    monkeypatch.setattr(kit, "_preflight_function", missing)
    assert kit.main(["stage", "--live", str(live), "--stage", str(tmp_path / "stage-2"),
                     "--codex", "codex", "--lane-home", "lane-a"]) == 3
    assert "installed runtime" in capsys.readouterr().err and not (tmp_path / "stage-2").exists()


def test_inside_matches_the_live_folder_by_identity(live, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(live)
    assert kit._inside(alias / "stage", live) and not kit._inside(tmp_path / "stage", live)
    other_case = live.parent / live.name.upper()
    if other_case.exists():                     # a case-insensitive volume, as APFS is by default
        assert kit._inside(other_case / "stage", live)
        with pytest.raises(kit.Refusal):
            staged(live, other_case / "stage")
        assert not (live / "stage").exists()


def test_rollback_checks_the_aside_name_before_it_writes(live, tmp_path):
    before = (live / "TRUST").read_bytes()
    staged(live, tmp_path / "stage")
    swapped(live, tmp_path / "stage")
    installed = (live / "TRUST").read_bytes()
    (live / f"{kit.BACKUP}.rolled-back-20261118T030000Z").write_bytes(b"an earlier rollback")
    with pytest.raises(kit.Refusal, match="exists"):
        kit.rollback(live, now=WHEN)
    assert (live / "TRUST").read_bytes() == installed and (live / kit.BACKUP).read_bytes() == before
    assert kit.rollback(live, now=WHEN + timedelta(seconds=1)) == 0 and (live / "TRUST").read_bytes() == before
