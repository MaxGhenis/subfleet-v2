"""C-8.4: retain each repository spelling until discovery can revalidate it."""
from contextlib import contextmanager

import pytest

from subfleet import dependencies


@pytest.mark.parametrize("alias", ["gitfile", "gitdir", "commondir", "common-target", "alternate", "folder", "directory", "vanished", "stable"])
def test_repository_pointer_changes_are_incomplete(tmp_path, monkeypatch, alias):
    outside = tmp_path / "outside"
    outside.mkdir()
    old, new = tmp_path / "old", tmp_path / "new"
    for repository in (old, new):
        (repository / "objects" / "info").mkdir(parents=True)
        (repository / "objects" / "info" / "alternates").write_bytes(b"# first\n# second\n")
    dotgit = outside / ".git"
    dotgit.write_text(f"gitdir: {old}\n")
    filename = old / "objects" / "info" / "alternates"
    named = dotgit
    workdir = outside
    if alias == "gitdir":
        named = tmp_path / "gitdir"
        named.symlink_to(old)
        dotgit.write_text(f"gitdir: {named}\n")
    elif alias in ("commondir", "common-target"):
        named = old / "commondir"
        named.write_text(str(new))
        filename = new / "objects" / "info" / "alternates"
        if alias == "common-target":
            named = tmp_path / "common"
            named.symlink_to(new)
            (old / "commondir").write_text(str(named))
    elif alias == "alternate":
        named = tmp_path / "alternate"
        named.symlink_to(new / "objects")
        filename.write_text(str(named) + "\n")
        filename = new / "objects" / "info" / "alternates"
    elif alias == "folder":
        named = tmp_path / "folder"
        named.symlink_to(outside)
        workdir = named
    elif alias == "directory":
        dotgit.unlink()
        dotgit.mkdir()
        (dotgit / "objects" / "info").mkdir(parents=True)
        filename = dotgit / "objects" / "info" / "alternates"
        filename.write_bytes(b"# first\n# second\n")
    changed = []
    real = dependencies.open_regular

    @contextmanager
    def opening(path, *args, **kwargs):
        with real(path, *args, **kwargs) as stream:
            if str(path) != str(filename):
                yield stream
                return

            class Changing:
                def readline(self, size=-1):
                    line = stream.readline(size)
                    if not changed:
                        changed.append(True)
                        if alias in ("gitfile", "commondir"):
                            named.write_text(f"gitdir: {new}\n" if alias == "gitfile" else str(old))
                        elif alias in ("gitdir", "common-target", "alternate", "folder"):
                            named.unlink()
                            named.symlink_to(old if alias == "common-target" else new)
                        elif alias == "directory":
                            dotgit.rename(outside / "old-git")
                            dotgit.symlink_to(new)
                        elif alias == "vanished":
                            dotgit.unlink()
                    return line

                def __getattr__(self, name):
                    return getattr(stream, name)

            yield Changing()

    monkeypatch.setattr(dependencies, "open_regular", opening)
    recorded = dependencies.record(str(workdir))
    assert changed
    assert bool(recorded.get("incomplete")) == (alias != "stable"), recorded
    if alias != "stable":
        assert any(error["file"] == str(named) for error in recorded["incomplete"])
