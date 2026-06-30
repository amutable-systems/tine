"""sandbox — the exec-environment primitive every build action goes through.

A thin CLI over the vendored mkosi-sandbox (mkosi/sandbox.py). It assembles the namespace
the way mkosi's sandbox_cmd does: start from an empty root and bind the `--tools` tree's
top-level entries onto `/` read-only (so the binary and its runtime come from a pinned
chroot, never the host), add /proc + /dev + tmpfs /run,/tmp,/var/tmp, and become-root in the
user namespace. It sets up *only* the exec environment — a driver that needs a target root to
install into or run against sets that up itself (see rootfs.py, which drives mkosi's
FSOperation primitives from inside this namespace).

It's a leaf: `main()` parses argv, assembles the mkosi-sandbox argv, replaces the host
environment with a clean base (so it can't leak into the build), and execs. Callers that need
to nest a sandbox (build_rpm's rpmbuild) fork it off as a subprocess rather than importing it.
"""

import argparse
import os
from pathlib import Path
from typing import NoReturn

import mkosi.sandbox

# Top-level entries the sandbox provides itself (kernel APIs + ephemerals), so
# we never bind them from the tools tree.
_PROVIDED = frozenset({"proc", "sys", "dev", "run", "tmp", "boot"})

# The clean base environment the sandboxed command runs with. mkosi-sandbox execs
# via os.execvp, which inherits the *current* environment — so without this the
# host env (TMPDIR, PATH, HOME, XDG, SSH, …) would leak in. We wipe it and set only
# deterministic values (fixed PATH, UTC, C.UTF-8) so builds don't vary with the
# host; callers add specifics via --setenv. TMPDIR points at the tmpfs at /tmp.
_BASE_ENV = {
    "PATH": "/usr/bin:/usr/sbin:/bin:/sbin",
    "HOME": "/root",
    "TMPDIR": "/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
}


def _abs(p: str) -> str:
    return str(Path(p).resolve())


def _kv(pairs: list[str], sep: str) -> list[tuple[str, str]]:
    out = []
    for p in pairs:
        if sep not in p:
            raise SystemExit(f"expected SRC{sep}DST, got {p!r}")
        a, b = p.split(sep, 1)
        out.append((a, b))
    return out


def main(argv: list[str] | None = None) -> NoReturn:
    p = argparse.ArgumentParser(prog="sandbox")
    p.add_argument("--tools", required=True, help="ro exec-env chroot bound onto / (the pinned tools tree)")
    p.add_argument("--bind", action="append", default=[], help="SRC:DST rw bind")
    p.add_argument("--ro-bind", dest="ro_bind", action="append", default=[], help="SRC:DST ro bind")
    p.add_argument("--scratch", action="append", default=[], help="NAME:DST tmpfs (NAME=_ → empty dir)")
    p.add_argument("--setenv", action="append", default=[], help="K=V environment")
    p.add_argument("--source-date-epoch", type=int, default=None)
    p.add_argument("--chdir", default=None)
    p.add_argument("--bind-cwd", dest="bind_cwd", action="store_true", help="bind+chdir the project root")
    p.add_argument("--network", action="store_true", help="grant network (default: unshared)")
    p.add_argument("cmd", nargs="*", help="the command to run (after `--`)")
    args = p.parse_args(argv)
    if not args.cmd:
        raise SystemExit("no command given (expected `-- cmd ...`)")

    # Assemble the mkosi-sandbox argv (everything after `mkosi.sandbox`).
    out: list[str] = []

    # In bind_cwd mode the project tree is bound rw under its real path; don't
    # ro-bind the tools tree's top-level dir it lives under (e.g. an empty /home),
    # or that ro mount would block creating the project path beneath it.
    cwd = os.getcwd() if args.bind_cwd else None
    cwd_parts = Path(cwd).parts if cwd else ()
    cwd_top = cwd_parts[1] if len(cwd_parts) > 1 else None

    # The exec environment bound onto /: the read-only --tools tree. Bind each top-level
    # entry onto /, replicating usr-merge symlinks (bin/lib/lib64/sbin -> usr/*) rather than
    # binding through them.
    for entry in sorted(Path(args.tools).resolve().iterdir()):
        if entry.name in _PROVIDED:
            continue
        if entry.name == cwd_top:
            # The project tree is bound rw beneath /{cwd_top}; we can't also
            # ro-bind the tools' /{cwd_top} (the rw mount can't be created under a
            # ro parent). Safe only if there's nothing there to hide — otherwise
            # the repo must move off a tools-tree path (e.g. clone under /home).
            if entry.is_dir() and any(entry.iterdir()):
                raise SystemExit(
                    f"project root /{cwd_top}/… collides with non-empty tools dir "
                    f"/{cwd_top}; check out the repo under a path whose first "
                    f"component isn't a tools-tree entry (e.g. /home, /tmp, /srv)"
                )
            continue
        dest = "/" + entry.name
        if entry.is_symlink():
            out += ["--symlink", str(entry.readlink()), dest]
        elif entry.is_dir():
            out += ["--ro-bind", str(entry), dest]

    for name, dest in _kv(args.scratch, ":"):
        out += ["--dir", dest] if name == "_" else ["--tmpfs", dest]
    for src, dest in _kv(args.bind, ":"):
        out += ["--bind", _abs(src), dest]
    for src, dest in _kv(args.ro_bind, ":"):
        out += ["--ro-bind", _abs(src), dest]

    chdir = args.chdir
    if cwd:
        out += ["--bind", cwd, cwd]
        chdir = chdir or cwd

    # Kernel API filesystems + writable ephemerals every rpm scriptlet expects.
    # /var/tmp must be writable (over the ro /var bind) for rpm's scriptlet staging
    # (rpm-tmp.*, %sysusers); mkosi binds a writable /var/tmp likewise.
    out += ["--bind", "/proc", "/proc", "--dev", "/dev"]
    out += ["--tmpfs", "/run", "--tmpfs", "/tmp", "--tmpfs", "/var/tmp"]

    # Reproducibility + isolation, centralized so no action can forget them.
    if args.source_date_epoch is not None:
        out += ["--setenv", "SOURCE_DATE_EPOCH", str(args.source_date_epoch)]
    for k, v in _kv(args.setenv, "="):
        out += ["--setenv", k, v]
    if args.network:
        # A networked tool (e.g. the lock generator) needs DNS. Keep the engine root's
        # /etc (its *Fedora* CA trust — the host's may live at other paths) and overlay
        # only the host's resolver. /etc/resolv.conf is a symlink (typically into
        # /run/systemd/resolve), so bind it *nofollow* — as the symlink itself — and
        # bind /run so the target resolves. The engine root ships a resolv.conf symlink
        # as the mountpoint (its /etc is ro). Builds stay hermetic (network unshared).
        out += ["--ro-bind-nofollow", "/etc/resolv.conf", "/etc/resolv.conf", "--ro-bind", "/run", "/run"]
    else:
        out += ["--unshare-net"]
    if chdir:
        out += ["--chdir", chdir]

    # fakeroot-equivalent: let rpmbuild/dnf run unprivileged.
    out += ["--suppress-chown", "--suppress-sync", "--become-root"]
    out += ["--", *args.cmd]

    # Replace the inherited host environment with our clean base: mkosi-sandbox's
    # os.execvp passes the current os.environ to the command (only --setenv layers on
    # top), so this is what stops the host env leaking in.
    os.environ.clear()
    os.environ.update(_BASE_ENV)
    mkosi.sandbox.main(out)  # calls enter() then os.execvp; never returns
    raise SystemExit(127)  # unreachable; for the type checker


if __name__ == "__main__":
    main()
