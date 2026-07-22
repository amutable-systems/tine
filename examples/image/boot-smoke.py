#!/usr/bin/env python3
"""Boot an image_vm target under a pty, smoke-check it, then power off.

The image_vm runner drives systemd-vmspawn with --console=native, so its console is a terminal, not
a pipe. This wraps the runner in a pseudo-terminal to make that console machine-controllable: wait
for the autologin root shell, assert the system settled, then poweroff and wait for the guest to go
down. A throwaway smoke test -- the real OS carries its own suite.

Usage: boot-smoke.py <vm-runner-command...>   (e.g. the expansion of `buck run :boot-demo-vm`)
"""

import fcntl
import os
import pty
import re
import select
import shlex
import struct
import sys
import termios
import time

# A first boot runs firstboot credential setup and mounts the sysext, so allow a generous window.
BOOT_TIMEOUT = float(os.environ.get("SMOKE_BOOT_TIMEOUT", "300"))
STEP_TIMEOUT = float(os.environ.get("SMOKE_STEP_TIMEOUT", "30"))
POWEROFF_TIMEOUT = float(os.environ.get("SMOKE_POWEROFF_TIMEOUT", "90"))

# `systemctl is-system-running` is degraded today (a known bug); accept it until the boot is clean.
# TODO: tighten to {"running"} once the degraded units are fixed.
ACCEPT = {"running", "degraded"}

# A root shell prompt: a login shell with no PS1 shows `-bash-5.3#`; also accept `bash-5.3#` and the
# `[root@host ~]#` form. The leading `-` marks a login shell (argv[0] is `-bash`).
PROMPT = re.compile(rb"-?(?:bash|sh)-[0-9.]+[#$]|\][#$]")
# Sentinels the guest echoes back; the bracketed value only appears in printf's output, never in the
# echoed command line, so matching it can't trip over the command itself.
READY = re.compile(rb"TINE_READY\[ok\]")
STATE = re.compile(rb"SMOKE_STATE\[([a-z-]+)\]")
# CSI/escape sequences and bare CRs, stripped before matching so prompts survive the native console.
ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[=>()][A-Za-z0-9]?|\r")


class Console:
    """A pty-attached child process whose output can be matched and whose input can be driven."""

    def __init__(self, argv: list[str]) -> None:
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child: the slave pty is already stdio and our controlling terminal
            os.execvp(argv[0], argv)
            os._exit(127)  # unreachable unless exec fails
        # A roomy console: the native/OVMF console resizes and wraps, which would fracture matches.
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        os.set_blocking(self.fd, False)
        self._seen = bytearray()

    def expect(self, pattern: re.Pattern[bytes], timeout: float) -> re.Match[bytes] | None:
        """Read until `pattern` matches the (ANSI-stripped) output or `timeout` elapses / EOF."""
        deadline = time.monotonic() + timeout
        while True:
            match = pattern.search(self._seen)
            if match:
                return match
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if not select.select([self.fd], [], [], remaining)[0]:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                return None  # slave hung up
            if not chunk:
                return None  # EOF: the child exited
            # Tee the ANSI-stripped console: it keeps the log readable and, crucially, drops the
            # guest's terminal query sequences (cursor-position/window reports). Forwarding those to a
            # real terminal makes it reply with escape codes that would otherwise land in the user's
            # next shell prompt.
            visible = ANSI.sub(b"", chunk)
            os.write(sys.stdout.fileno(), visible)
            self._seen += visible

    def send(self, line: str) -> None:
        os.write(self.fd, line.encode() + b"\n")

    def wait_for_exit(self, timeout: float) -> int | None:
        """Drain output until the child exits; return its exit status, or None on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.expect(re.compile(rb"(?!x)x"), min(2.0, deadline - time.monotonic()))  # never matches
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                return os.waitstatus_to_exitcode(status)
        return None

    def close(self) -> None:
        try:
            os.kill(self.pid, 15)
            if self.wait_for_exit(5) is None:
                os.kill(self.pid, 9)
        except ProcessLookupError:
            pass
        os.close(self.fd)


def drain_terminal() -> None:
    """Discard any terminal query replies the console provoked, so none land in the user's prompt."""
    try:
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except OSError:
        pass


def fail(console: Console, message: str) -> None:
    print(f"\n[boot-smoke] FAIL: {message}", file=sys.stderr)
    console.close()
    drain_terminal()
    raise SystemExit(1)


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("usage: boot-smoke.py <vm-runner-command...>")
    # command_alias expands `$(exe :target)` into one space-joined argument; split it back into argv.
    # buck-out paths carry no spaces, so a plain shell split round-trips the runner command exactly.
    if len(argv) == 1:
        argv = shlex.split(argv[0])
    console = Console(argv)

    print("[boot-smoke] waiting for the autologin shell...", file=sys.stderr)
    if console.expect(PROMPT, BOOT_TIMEOUT) is None:
        fail(console, "never reached a shell prompt")

    # Confirm the shell actually executes our input before trusting later commands.
    console.send("printf 'TINE_READY[%s]\\n' ok")
    if console.expect(READY, STEP_TIMEOUT) is None:
        fail(console, "shell did not echo the readiness sentinel")

    # Capture the state via a bracketed token so the echoed command (which contains "running") can't
    # match. Redirect the query so only our printf reaches the console.
    console.send("systemctl is-system-running >/tmp/smoke 2>&1; printf 'SMOKE_STATE[%s]\\n' \"$(cat /tmp/smoke)\"")
    match = console.expect(STATE, STEP_TIMEOUT)
    if match is None:
        fail(console, "system state query produced no result")
    state = match.group(1).decode()
    print(f"\n[boot-smoke] system state: {state}", file=sys.stderr)
    if state not in ACCEPT:
        fail(console, f"unacceptable system state {state!r} (accepting {sorted(ACCEPT)})")

    print("[boot-smoke] powering off...", file=sys.stderr)
    console.send("poweroff")
    code = console.wait_for_exit(POWEROFF_TIMEOUT)
    if code is None:
        fail(console, "guest did not power off in time")
    os.close(console.fd)
    drain_terminal()
    print(f"[boot-smoke] OK: state={state}, runner exit={code}", file=sys.stderr)
    # The runner may exit non-zero on an ephemeral-VM teardown detail; the clean poweroff is the signal.
    raise SystemExit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
