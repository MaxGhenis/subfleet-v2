#!/usr/bin/env python3
"""Await one Swift frontend and retain completed objects for resumable builds.

Used only by the verification xcrun wrapper. Exact driver flags, source
contents and compiler identity key the cache. No process is detached.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
COMPILER = Path('/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/swift-frontend')
CACHE = ROOT / 'build/app-cutover-evidence/objects'
RECORDS = ROOT / 'build/app-cutover-evidence/compiler-processes.jsonl'
OUTPUT_FLAGS = {'-o', '-emit-module-path', '-emit-module-doc-path', '-emit-module-source-info-path',
                '-serialize-diagnostics-path', '-emit-dependencies-path', '-emit-reference-dependencies-path'}


def record(value):
    with RECORDS.open('a') as stream:
        stream.write(json.dumps(value) + '\n')


def main():
    args = sys.argv[1:]
    normalized, outputs = [], []
    i = 0
    compiler_stat = COMPILER.stat()
    digest = hashlib.sha256(repr((str(COMPILER), compiler_stat.st_size, compiler_stat.st_mtime_ns)).encode())
    while i < len(args):
        arg = args[i]
        normalized.append(arg)
        if arg in OUTPUT_FLAGS:
            outputs.append(Path(args[i + 1]))
            normalized.append('<output>')
            i += 2
            continue
        if arg.endswith('.swift') and Path(arg).is_file():
            digest.update(Path(arg).read_bytes())
        i += 1
    digest.update(json.dumps(normalized).encode())
    slot = CACHE / digest.hexdigest()
    artifacts = [slot / str(n) for n in range(len(outputs))]
    if outputs and all(p.is_file() for p in artifacts):
        for artifact, output in zip(artifacts, outputs):
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact, output)
        record({'event': 'resumed', 'wrapper': os.getpid(), 'key': slot.name})
        return 0
    primary = args[args.index('-primary-file') + 1] if '-primary-file' in args else ''
    child = subprocess.Popen([str(COMPILER), *args])
    record({'event': 'started', 'pid': child.pid, 'wrapper': os.getpid(), 'primary': primary, 'at': time.time()})

    def forward(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGTERM, forward)
    try:
        status = child.wait(timeout=float(os.environ.get('SF_CUTOVER_FRONTEND_SECONDS', '300')))
    except subprocess.TimeoutExpired:
        child.send_signal(signal.SIGINT)
        try:
            status = child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            status = child.wait()
        print('Swift frontend reached its foreground limit: ' + primary, file=sys.stderr)
        status = 124
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    record({'event': 'finished', 'pid': child.pid, 'status': status, 'at': time.time()})
    if status == 0 and outputs and all(p.is_file() for p in outputs):
        slot.mkdir(parents=True, exist_ok=True)
        for output, artifact in zip(outputs, artifacts):
            temporary = artifact.with_name(artifact.name + '.' + str(os.getpid()) + '.tmp')
            shutil.copyfile(output, temporary)
            os.replace(temporary, artifact)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
