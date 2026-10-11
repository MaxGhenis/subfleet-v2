"""A stand-in for the development app on the daemon's socket (C-25.6, design D-21).

Person-only operations are the app's alone since D-PB1 (2026-10-11): a terminal
no longer counts. A daemon on any state root but ~/.subfleet also accepts the
development build that `SUBFLEET_DEV_APP_EXECUTABLE` named when it started, and
it knows that build by the kernel's path for the caller's image
(`proc_pidpath`), never by a script's name. So the tests' person is a small
native client, compiled once, that does what the app does on the socket: one
request, one reply line. It sits at a path with spaces, as the development
build's does (`Subfleet Dev.app`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ENV = "SUBFLEET_DEV_APP_EXECUTABLE"

SOURCE = r'''
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    struct sockaddr_un address;
    memset(&address, 0, sizeof address);
    address.sun_family = AF_UNIX;
    if (strlen(argv[1]) >= sizeof address.sun_path) return 2;
    strcpy(address.sun_path, argv[1]);
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0 || connect(fd, (struct sockaddr *)&address, sizeof address) != 0) { perror("connect"); return 1; }
    struct timeval wait = {60, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &wait, sizeof wait);
    const char *parts[2] = {argv[2], "\n"};
    for (int i = 0; i < 2; i++) {
        size_t left = strlen(parts[i]);
        const char *at = parts[i];
        while (left > 0) {
            ssize_t sent = write(fd, at, left);
            if (sent <= 0) { perror("write"); return 1; }
            at += sent;
            left -= (size_t)sent;
        }
    }
    fputs("RESPONSE ", stdout);
    char buffer[65536];
    for (;;) {
        ssize_t got = read(fd, buffer, sizeof buffer);
        if (got <= 0) { perror("read"); return 1; }
        fwrite(buffer, 1, (size_t)got, stdout);
        if (memchr(buffer, '\n', (size_t)got)) break;
    }
    close(fd);
    return 0;
}
'''


def build(directory: Path) -> Path | None:
    """Compile the stand-in under `directory`; None when there is no C compiler."""
    compiler = shutil.which("cc") or shutil.which("clang")
    if not compiler:
        return None
    target = directory / "Subfleet Dev.app" / "Contents" / "MacOS" / "Subfleet Dev"
    target.parent.mkdir(parents=True, exist_ok=True)
    built = subprocess.run([compiler, "-O1", "-Wall", "-Werror", "-x", "c", "-", "-o", str(target)],
                           input=SOURCE, capture_output=True, text=True, timeout=120)
    if built.returncode:
        raise RuntimeError(f"the development app's stand-in did not compile: {built.stderr}")
    return target.resolve()


def request(app: Path | str, socket_path: Path | str, payload: dict, *, timeout: float = 60) -> dict:
    """Send one request as the app, from a session of its own (no controlling
    terminal: only the app's image can admit it), and return the reply."""
    out = subprocess.run([str(app), str(socket_path), json.dumps(payload)], capture_output=True, text=True,
                         timeout=timeout, stdin=subprocess.DEVNULL, start_new_session=True)
    if not out.stdout.startswith("RESPONSE "):
        raise AssertionError((out.returncode, out.stdout, out.stderr))
    return json.loads(out.stdout[len("RESPONSE "):])
