"""Automounts are rejected before offload asks them for identity or encryption."""

from __future__ import annotations

import os

import pytest

from subfleet import retention_volume as rvol


@pytest.mark.parametrize("encryption", [False, True], ids=["enumeration", "target"])
@pytest.mark.parametrize(
    ("fstype", "flags"),
    [
        (b"apfs", rvol.MNT_LOCAL | 0x00400000),  # Darwin MNT_AUTOMOUNTED
        (b"autofs", rvol.MNT_LOCAL),
    ],
    ids=["automounted-flag", "autofs-filesystem"],
)
def test_automounts_are_refused_without_identity_or_encryption_reads(
    tmp_path, monkeypatch, encryption, fstype, flags
):
    fs = rvol._Statfs()
    fs.f_fstypename = fstype
    fs.f_flags = flags
    fs.f_mntonname = os.fsencode(tmp_path)
    reads = []

    def identity(mount):
        reads.append(("identity", mount))
        return "SYNTHETIC-AUTOMOUNT-UUID", "Synthetic automount"

    def encrypted(mount):
        reads.append(("encryption", mount))
        return True

    monkeypatch.setattr(rvol, "_uuid_and_name", identity)
    monkeypatch.setattr(rvol, "_encrypted", encrypted)

    volume = rvol._volume(fs, encryption=encryption)

    assert reads == [], "classification must not trigger an automount"
    assert volume.local is False
    assert volume.uuid is None
    assert volume.encrypted is None
    assert any("not a local volume" in why for why in rvol.refusals(volume, allow_unencrypted=True))
