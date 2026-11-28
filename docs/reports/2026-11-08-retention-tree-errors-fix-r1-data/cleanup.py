"""Remove only this fresh checkout's generated caches, after all tests exit."""
import os
from pathlib import Path
import shutil
import subprocess

root = Path.cwd().resolve()
assert root.name == "20261115-180648-rte-fix-r1b"
staged = root / "sf-rte-fix1-generated-caches"
assert not staged.exists()
paths = [root / name for name in (".venv", ".pytest_cache", ".hypothesis")
         if (root / name).is_dir()]
for directory, names, _ in os.walk(root):
    names[:] = [name for name in names
                if name not in {".git", ".agents", ".codex", ".aws", ".venv", ".pytest_cache", ".hypothesis"}]
    if "__pycache__" in names:
        paths.append(Path(directory) / "__pycache__")
        names.remove("__pycache__")

tracked = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
relative = sorted(str(path.relative_to(root)) for path in paths)
for name in relative:
    assert not any(item == name or item.startswith(name + "/") for item in tracked)
    assert not (root / name).is_symlink()

inventory = root / "docs/reports/2026-11-08-retention-tree-errors-fix-r1-data/cleanup-paths.txt"
inventory.write_text("\n".join(relative) + "\n")
staged.mkdir()
for index, path in enumerate(paths):
    path.rename(staged / str(index))
assert "sf-rte-fix1" in str(staged.resolve())
assert staged.parent == root and not staged.is_symlink()
shutil.rmtree(staged)
assert not staged.exists()
assert all(not (root / name).exists() for name in relative)
print(f"Removed {len(relative)} generated folders; inventory: {inventory.relative_to(root)}")
