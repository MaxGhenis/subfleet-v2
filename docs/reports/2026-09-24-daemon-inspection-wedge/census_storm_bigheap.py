import resource, sys, time, threading
ballast = [{"k": i, "s": "x" * 40} for i in range(4_000_000)]   # ~1 GB of small Python objects, like the daemon's heap
idle = [threading.Thread(target=threading.Event().wait, daemon=True) for _ in range(70)]  # ~80 threads, like the daemon
[t.start() for t in idle]
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9
print(f"heap ballast ready: max RSS {rss:.2f} GB, threads {threading.active_count()}", flush=True)
exec(open("census_storm.py").read().replace("run(0); run(1); run(3); run(7)", "run(0); run(3); run(7)"))
