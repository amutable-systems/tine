"""Run commands with pinned userspace through the vendored mkosi sandbox.

The default mode provides a clean, isolated build environment. `--relaxed` retains the
pinned userspace but exposes host devices, services, environment, cwd, and network.
Target-root setup belongs to rootfs.py rather than this launcher.
"""

import argparse
import os
import tempfile
from pathlib import Path
from typing import NoReturn

import mkosi.sandbox

# Kernel APIs and ephemeral trees supplied by the sandbox.
_PROVIDED = frozenset({"proc", "sys", "dev", "run", "tmp", "boot"})

# Relaxed mode takes userspace from tools and everything else from the host.
_TOOLS_DIRS = ("usr", "opt")
_TOOLS_LINKS = ("bin", "sbin", "lib", "lib32", "lib64")
_HOST_SKIP = frozenset({"proc", "nix", "etc", *_TOOLS_DIRS, *_TOOLS_LINKS})
_HOST_ETC = ("machine-id",)

# Deterministic environment replacing mkosi-sandbox's inherited host environment.
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


def _relaxed(out: list[str], tools: Path) -> None:
    """Mount pinned userspace over a host-integrated root."""
    for name in _TOOLS_DIRS:
        if (tools / name).is_dir():
            out += ["--ro-bind", str(tools / name), "/" + name]
    for name in _TOOLS_LINKS:
        entry = tools / name
        if entry.is_symlink():
            out += ["--symlink", str(entry.readlink()), "/" + name]
        elif entry.is_dir():
            out += ["--ro-bind", str(entry), "/" + name]
    for entry in sorted(Path("/").iterdir()):
        if entry.name in _HOST_SKIP:
            continue
        if entry.is_symlink():
            out += ["--symlink", str(entry.readlink()), str(entry)]
        else:
            out += ["--bind", str(entry), str(entry)]
    if (tools / "etc").is_dir():
        out += ["--ro-bind", str(tools / "etc"), "/etc"]
    for f in _HOST_ETC:
        if Path("/etc", f).exists() and (tools / "etc" / f).exists():
            out += ["--ro-bind", f"/etc/{f}", f"/etc/{f}"]
    _identity(out, tools)


def _identity(out: list[str], tools: Path) -> None:
    """Make the invoking uid/gid resolvable inside the sandbox.

    Relaxed /etc comes from the tools tree, which lists only system users. On a host the caller's uid
    is resolved by nss-systemd via the bound /run, but where that is unavailable (e.g. a CI runner
    whose uid is served by neither files nor userdb) getpwuid() fails and callers like ssh-keygen
    abort. Append an entry for the caller to the passwd/group tables and bind them over /etc; a file
    bind stacks over the read-only /etc mount, which a plain write could not.
    """
    uid, gid = os.getuid(), os.getgid()
    name = os.environ.get("USER") or ""
    home = os.environ.get("HOME") or ""
    if not name or not name.isascii() or ":" in name or "\n" in name:
        name = f"u{uid}"
    if not home or ":" in home or "\n" in home:
        home = "/root"
    tables = {
        "passwd": f"{name}:x:{uid}:{gid}::{home}:/bin/sh\n",
        "group": f"{name}:x:{gid}:\n",
    }
    for base, entry in tables.items():
        source = tools / "etc" / base
        content = source.read_text(encoding="utf-8") if source.exists() else ""
        # Deterministic per-uid path: overwritten each run rather than accumulated, and O_NOFOLLOW so
        # a pre-planted symlink can't redirect the write.
        path = Path(tempfile.gettempdir(), f".tine-sandbox-{base}-{uid}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content + entry)
        out += ["--ro-bind", str(path), f"/etc/{base}"]


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
    p.add_argument(
        "--relaxed",
        action="store_true",
        help="host-integrated mode: tools supply the userspace, the host supplies the rest "
        "(devices, /run, network, env, cwd) — for interactive leaves (vmspawn), never builds",
    )
    p.add_argument("cmd", nargs="*", help="the command to run (after `--`)")
    args = p.parse_args(argv)
    if not args.cmd:
        raise SystemExit("no command given (expected `-- cmd ...`)")
    if args.relaxed and args.bind_cwd:
        raise SystemExit("--bind-cwd is for hermetic builds; --relaxed sees the host cwd already")

    out: list[str] = []

    # Leave the cwd's top-level directory writable for the project bind.
    cwd = os.getcwd() if args.bind_cwd else None
    cwd_parts = Path(cwd).parts if cwd else ()
    cwd_top = cwd_parts[1] if len(cwd_parts) > 1 else None

    # Recreate usr-merge symlinks instead of binding through them.
    tools = Path(args.tools).resolve()
    if args.relaxed:
        _relaxed(out, tools)
    else:
        for entry in sorted(tools.iterdir()):
            if entry.name in _PROVIDED:
                continue
            if entry.name == cwd_top:
                # A non-empty tools directory cannot be hidden by the writable project bind.
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

    # Package scripts require writable API and temporary filesystems.
    out += ["--bind", "/proc", "/proc"]
    if not args.relaxed:
        out += ["--dev", "/dev"]
        out += ["--tmpfs", "/run", "--tmpfs", "/tmp", "--tmpfs", "/var/tmp"]

    if args.source_date_epoch is not None:
        out += ["--setenv", "SOURCE_DATE_EPOCH", str(args.source_date_epoch)]
    for k, v in _kv(args.setenv, "="):
        out += ["--setenv", k, v]
    if args.relaxed:
        # Resolve through the host /run while keeping the tools tree's /etc.
        if Path("/etc/resolv.conf").exists():
            out += ["--ro-bind-nofollow", "/etc/resolv.conf", "/etc/resolv.conf"]
        chdir = chdir or os.getcwd()
    elif args.network:
        # Preserve engine CA trust but use the host resolver and its /run target.
        out += ["--ro-bind-nofollow", "/etc/resolv.conf", "/etc/resolv.conf", "--ro-bind", "/run", "/run"]
    else:
        out += ["--unshare-net"]
    if chdir:
        out += ["--chdir", chdir]

    # Builds need fakeroot semantics; relaxed tools must remain the invoking user.
    if not args.relaxed:
        out += ["--suppress-chown", "--suppress-sync", "--become-root"]
    out += ["--", *args.cmd]

    # Keep only Buck's on-disk scratch path when replacing the host environment.
    if not args.relaxed:
        scratch = os.environ.get("BUCK_SCRATCH_PATH") if args.bind_cwd else None
        os.environ.clear()
        os.environ.update(_BASE_ENV)
        if scratch:
            os.environ["BUCK_SCRATCH_PATH"] = scratch
    mkosi.sandbox.main(out)  # calls enter() then os.execvp; never returns
    raise SystemExit(127)  # unreachable; for the type checker


if __name__ == "__main__":
    main()
