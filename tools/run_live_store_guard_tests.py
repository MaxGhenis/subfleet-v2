"""Run a bounded guard batch in the foreground under the shared test lock.

Native-reader metadata is emulated with a temporary file: probes can compare
the string /usr/bin/security without reading, statting, or starting that file.
Shell probes use the separate recording executables in the test fixtures.
Use UV_PROJECT_ENVIRONMENT to select a workspace-local Python environment.
"""

from __future__ import annotations

import collections
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

METADATA_BOOTSTRAP = '''\
import os, unicodedata
_fixture = os.environ.get('SF_R7_NATIVE_FIXTURE', '')
_stat, _lstat, _readlink, _access = os.stat, os.lstat, os.readlink, os.access
_meta = _stat(_fixture) if _fixture else _stat(__file__)
_volfs = f'/.vol/{_meta.st_dev}/{_meta.st_ino}'
def _native(path):
    if isinstance(path, int):
        return False
    try:
        spelled = os.path.abspath(os.fsdecode(path))
        folded = unicodedata.normalize('NFD', '/' + os.path.normpath(spelled).lstrip('/')).casefold()
        return folded == '/usr/bin/security' or folded == _volfs
    except (TypeError, ValueError):
        return False
def _wrap(call):
    def guarded(path, *args, **kwargs):
        return call(_fixture if _fixture and _native(path) else path, *args, **kwargs)
    return guarded
os.stat, os.lstat, os.readlink, os.access = map(_wrap, (_stat, _lstat, _readlink, _access))
'''


def file_counts(report):
    counts = collections.defaultdict(collections.Counter)
    for case in ET.parse(report).iter("testcase"):
        parts = case.attrib.get("classname", "").split(".")
        end = next((i + 1 for i, part in enumerate(parts) if part.startswith("test_")), len(parts))
        outcome = next((kind for kind in ("failure", "error", "skipped")
                        if case.find(kind) is not None), "passed")
        counts[".".join(parts[:end])][outcome] += 1
    for module, count in sorted(counts.items()):
        print("FILE_COUNTS", module, dict(count), flush=True)


def main():
    directory = subprocess.check_output(["getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()
    base = Path(tempfile.mkdtemp(prefix="sf-r198f7-", dir=directory))
    saved = {}
    try:
        bootstrap = base / "bootstrap"
        bootstrap.mkdir()
        (bootstrap / "sitecustomize.py").write_text(METADATA_BOOTSTRAP)
        native = base / "security"
        native.write_text("#!/bin/sh\nexit 99\n")
        native.chmod(0o700)
        workspace = Path.cwd().resolve()
        environment = Path(os.environ.get("UV_PROJECT_ENVIRONMENT", ".venv"))
        # Children which replace PYTHONPATH still need the metadata stand-in.
        for packages in (environment / "lib").glob("python*/site-packages"):
            target = packages / "sitecustomize.py"
            if not target.resolve().is_relative_to(workspace):
                raise ValueError("test bootstrap must remain in the workspace")
            saved[target] = target.read_bytes() if target.exists() else None
            target.write_text(METADATA_BOOTSTRAP)
        env = {**os.environ, "TMPDIR": str(base) + "/",
               "HYPOTHESIS_STORAGE_DIRECTORY": str(base / "hyp"), "HYPOTHESIS_PROFILE": "ci",
               "SF_R7_NATIVE_FIXTURE": str(native),
               "PYTHONPATH": str(bootstrap) + os.pathsep + str(workspace),
               "UV_CACHE_DIR": str(workspace / ".uv-cache"), "UV_OFFLINE": "1"}
        print("TMPDIR=" + str(base), flush=True)
        report = base / "results.xml"
        result = subprocess.run([
            "/usr/bin/lockf", "-k", "/private/tmp/claude-501/subfleet-suites.lock",
            "uv", "run", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short",
            "--junitxml=" + str(report), *sys.argv[1:],
        ], env=env)
        if report.exists():
            file_counts(report)
        return result.returncode
    finally:
        for target, original in saved.items():
            if original is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(original)
        subprocess.run(["python3", "-c",
                        "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)",
                        str(base)], check=True)


if __name__ == "__main__":
    sys.exit(main())
