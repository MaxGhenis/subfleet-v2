"""Read-only, alternating device-read comparison; run under suite_hold.sh.

--before-source is an owned scratch copy of machine.py from the base commit.
The state root must also be an owned directory on the volume being measured.
"""
import argparse
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subfleet import machine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before-source', required=True, type=Path)
    parser.add_argument('--state-root', required=True, type=Path)
    args = parser.parse_args()
    before = ModuleType('machine_before')
    exec(compile(args.before_source.read_text(), str(args.before_source), 'exec'), before.__dict__)
    old, current = before._Storage(), machine._Storage()
    samples = {'registry_id_lookup': [], 'retained_handle': []}
    load_before = os.getloadavg()
    power = subprocess.run(['/usr/bin/pmset', '-g', 'batt'], capture_output=True,
                           text=True, check=True, timeout=10).stdout.strip()
    try:
        identity = current.device_id(args.state_root)
        if identity is None:
            raise RuntimeError('owned state root did not resolve to a physical device')
        methods = [('registry_id_lookup', old), ('retained_handle', current)]
        for _, storage in methods:
            if set(storage.read(identity) or {}) != {identity}:
                raise RuntimeError('warmup did not read the selected device')
        for index in range(300):
            for name, storage in methods[::1 if index % 2 == 0 else -1]:
                started = time.perf_counter_ns()
                row = storage.read(identity)
                elapsed_us = (time.perf_counter_ns() - started) / 1000
                if set(row or {}) != {identity}:
                    raise RuntimeError(f'{name} lost or substituted the selected device')
                samples[name].append(elapsed_us)
    finally:
        current.close()
    costs = {}
    for name, values in samples.items():
        ordered = sorted(values)
        costs[name] = {'n': len(values), 'median_us': statistics.median(values),
                       'p90_us': ordered[math.ceil(.9 * len(values)) - 1]}
    print(json.dumps({'device_id': identity, 'root': str(args.state_root),
                      'python': platform.python_version(), 'power': power,
                      'load_before': load_before, 'load_after': os.getloadavg(),
                      'costs': costs}, indent=2))


if __name__ == '__main__':
    main()
