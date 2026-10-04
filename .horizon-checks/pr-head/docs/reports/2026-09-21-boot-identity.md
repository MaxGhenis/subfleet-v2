# Boot identity after macOS clock correction

On September 21 the installed CLI refused every request as daemon unavailable.
The daemon lock recorded boot seconds 1789915546, but `kern.boottime` now
reported 1789915544. The daemon PID and UTC process start still matched, its
snapshot was fresh, and its existing Unix socket answered a protocol ping.
Treating a mutable wall-clock boot timestamp as proof of reboot was wrong.

New identities use `kern.bootsessionuuid`, which identifies the boot session
independently of wall-clock time. Older timestamps remain readable: matching
seconds plus matching process start still establish the recorded identity;
different legacy seconds establish only uncertainty. Unknown identities cannot
authorize signals, release containment, or prove a process dead. A PID that is
absent, has a different start time, or carries a different boot-session UUID
still fails identity checks. Old receipts and attempts are never rewritten.

The client permits socket requests when its legacy lock metadata is uncertain;
the protocol response establishes availability. It still refuses a lock whose
process is provably gone. Legacy numeric fallback is not cached, since the
observed clock correction is exactly why it cannot be treated as immutable.

Regression tests cover the live two-second drift, legacy receipt compatibility,
real reboot, PID reuse, unavailable UUID fallback, and refusal to signal an
uncertain process.
