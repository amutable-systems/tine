"""sandbox — the chroot primitive every build action goes through.

A thin CLI over the vendored mkosi-sandbox (mkosi/sandbox.py). It assembles the
namespace the way mkosi's sandbox_cmd does: start from an empty root and bind the
exec environment onto `/` — each top-level entry of the `--tools` tree read-only (so
the binary and its runtime come from a pinned chroot, never the host), or, with no
`--tools`, the `--root` tree *read-write* (run-in-tree: the tree provides its own
binaries and is mutated in place). With both, `--root` is instead a target mounted
read-write at /buildroot to install into (with apivfs underneath if `--apivfs`). Then
add /proc + /dev + tmpfs /run,/tmp,/var/tmp at / and become-root in the user namespace.

It's a leaf: `main()` parses argv, assembles the mkosi-sandbox argv, replaces the host
environment with a clean base (so it can't leak into the build), and execs. Callers
that need to nest a sandbox (build_rpm's rpmbuild, the image step driver's `run`) fork
it off as a subprocess rather than importing it.
"""

import argparse
import os
from pathlib import Path
from typing import NoReturn

import mkosi.sandbox

# Top-level entries the sandbox provides itself (kernel APIs + ephemerals), so
# we never bind them from the tools tree.
_PROVIDED = frozenset({"proc", "sys", "dev", "run", "tmp", "boot", "buildroot"})

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
    p.add_argument("--tools", help="ro exec-env chroot bound onto / (omit to run in --root)")
    p.add_argument("--root", help="rw target tree: mounted at / (no --tools) or /buildroot (with --tools)")
    p.add_argument(
        "--apivfs",
        action="store_true",
        help="mount apivfs under /buildroot (for installs whose scriptlets chroot in)",
    )
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

    # The exec environment bound onto /: the read-only --tools tree, or — with no
    # --tools — the --root tree itself, bound read-write (run-in-tree, mutated in place).
    exec_root = args.tools or args.root
    if not exec_root:
        raise SystemExit("sandbox: need --tools or --root")
    exec_bind = "--ro-bind" if args.tools else "--bind"

    # Bind each top-level entry of the exec tree onto /. Replicate usr-merge symlinks
    # (bin/lib/lib64/sbin -> usr/*) rather than binding through them.
    for entry in sorted(Path(exec_root).resolve().iterdir()):
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
            out += [exec_bind, str(entry), dest]

    # With a --tools exec-env, --root is a separate target mounted rw at /buildroot to
    # install into (a bare --root is the exec tree above). With --apivfs, also mount
    # apivfs under it (mkosi's apivfs_options): a package install chroots into the target
    # to run scriptlets, so it needs a working /dev/null (--dev bind-mounts the real
    # one), /proc, and writable /run + /tmp + /var/tmp there. Without it rpm leaves a
    # bogus regular-file /dev/null that *captures* scriptlet output (random rpm-tmp.*
    # names → non-reproducible trees).
    if args.tools and args.root:
        # buck2 doesn't pre-create a declared-output dir, but mkosi-sandbox's bind
        # needs the source to exist.
        Path(args.root).mkdir(parents=True, exist_ok=True)
        out += ["--bind", _abs(args.root), "/buildroot"]
        if args.apivfs:
            out += [
                "--dev",
                "/buildroot/dev",
                "--bind",
                "/proc",
                "/buildroot/proc",
                "--tmpfs",
                "/buildroot/run",
                "--tmpfs",
                "/buildroot/tmp",
                "--tmpfs",
                "/buildroot/var/tmp",
            ]

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
