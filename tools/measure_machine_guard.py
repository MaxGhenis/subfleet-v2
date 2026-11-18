"""Observe C-6.13 on its real 50 ms tick / one-second native-read cadence.

Run through the suite-hold wrapper. No stress workload is created. JSON output
belongs in the job's scratch folder, never the repository. --state-root selects
an owned directory on the volume being measured, never a live state root.
"""
from __future__ import annotations
import argparse
import json
import statistics
import sys
import time
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from subfleet import machine  # noqa: E402


def distribution(values):
    values=sorted(v for v in values if v is not None)
    return ({'n':len(values),'min':values[0],'median':statistics.median(values),
             'p99':values[min(len(values)-1,int(len(values)*.99))],'max':values[-1]} if values else {'n':0})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root',type=Path,required=True)
    parser.add_argument('--seconds',type=int,default=90)
    parser.add_argument('--tick',type=float,default=.05)
    parser.add_argument('--daemon-point',action='store_true',help='Include policy checks and bounded minute writes on an owned temporary store; starts no daemon.')
    args=parser.parse_args()
    if not 2<=args.seconds<=180 or not .05<=args.tick<=1:
        parser.error('seconds 2..180, tick .05..1')
    sampler=machine.Sampler(args.state_root)
    sampler.prime()
    temporary=None; store=None; reader=sampler.read
    if args.daemon_point:
        from subfleet.daemon import Daemon
        from subfleet.policy import MACHINE_GUARD_PROPOSAL
        from subfleet.store import Store
        temporary=tempfile.TemporaryDirectory(prefix='read-point-',dir=args.state_root)
        store=Store(Path(temporary.name)/'probe.sqlite3')
        daemon=object.__new__(Daemon)  # only the reader; no lock, workers, live store or running daemon
        daemon.root=args.state_root
        daemon.policy={'admission':{'machine_guard':MACHINE_GUARD_PROPOSAL}}
        daemon.store=store
        daemon._machine_sampler=sampler
        daemon._last_machine_sample_wall=0.0
        reader=daemon._machine_reading
    rows=[]; ticks=[]; reads=[]; native_ticks=[]; legacy=[]; previous=None
    end=time.monotonic()+args.seconds
    while time.monotonic()<end:
        tick_start=time.perf_counter(); row=reader(); elapsed=(time.perf_counter()-tick_start)*1e6; ticks.append(elapsed)
        if row['sampled_at']!=previous:
            rows.append(row); reads.append(row['read_cost_s']*1e6); native_ticks.append(elapsed); previous=row['sampled_at']
        start=time.perf_counter(); machine.read(); legacy.append((time.perf_counter()-start)*1e6)
        time.sleep(max(0,args.tick-(time.perf_counter()-tick_start)))
    for row in rows: print('sample '+json.dumps(row),flush=True)
    summary={key:distribution([r.get(key) for r in rows]) for key in
             ('cpu_idle','disk_busy','disk_service_ms_raw','disk_in_flight','load1')}
    summary['cost_us']={'tick':distribution(ticks),'native_read':distribution(reads),'native_tick':distribution(native_ticks),'legacy':distribution(legacy)}
    summary['device_id']=sampler.device_id
    print('summary '+json.dumps(summary),flush=True)
    if store is not None: store.close()
    if temporary is not None: temporary.cleanup()


if __name__=='__main__': main()
