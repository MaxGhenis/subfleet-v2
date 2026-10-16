"""Fake signal delivery with the production authorization context contract."""
from contextlib import nullcontext


def authorized_signal_stub(deliver):
    def signal_process(recorded, sig, *, authorize=None):
        with authorize() if authorize is not None else nullcontext(True) as allowed:
            if not allowed:
                return False
            return deliver(recorded, sig)

    return signal_process
