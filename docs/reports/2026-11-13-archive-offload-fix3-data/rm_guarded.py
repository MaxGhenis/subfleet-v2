"""Guarded removal for fix round 3: every path must resolve under a folder named sf-ao-fix3."""
import os, shutil, stat, sys, time

def guarded_rmtree(target: str) -> None:
    target = os.path.realpath(target)
    assert "sf-ao-fix3" in target, target

    def onexc(func, path, exc):
        real = os.path.realpath(path)
        assert "sf-ao-fix3" in real and real.startswith(target), real
        parent = os.path.dirname(real)
        os.chmod(parent, stat.S_IMODE(os.lstat(parent).st_mode) | stat.S_IRWXU)
        if os.path.isdir(real) and not os.path.islink(real):
            os.chmod(real, stat.S_IMODE(os.lstat(real).st_mode) | stat.S_IRWXU)
        func(path)

    shutil.rmtree(target, onexc=onexc)

for arg in sys.argv[1:]:
    t = time.time()
    guarded_rmtree(arg)
    print(f"removed {os.path.realpath(arg) if os.path.exists(arg) else arg} in {time.time() - t:.1f}s; exists now: {os.path.exists(arg)}")
