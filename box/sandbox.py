"""Run commands with pinned userspace through the vendored mkosi sandbox.

The default mode provides a clean, isolated build environment. `--relaxed` retains the
pinned userspace but exposes host devices, services, environment, cwd, and network.
Target-root setup belongs to rootfs.py rather than this launcher.
"""

import argparse
import os
import re
import tempfile
from pathlib import Path
from typing import NoReturn

import mkosi.sandbox

# Hermetic sandbox mount point of the project (host cwd). A path of tine's own rather than just keeping
# the host path: a project under /var/lib or /root would otherwise have to be mounted inside one of the
# box's own read-only directories. No distribution ships a /tine, so nothing can be in the way.
# A chrooted operation cannot use this path (would otherwise leak into the image). That binds the project
# under /run instead; see PROJECT in image/layer.py.
_PROJECT = "/tine/project"

_BOX_LEVEL = re.compile(r":(?P<level>[1-9][0-9]*)$")

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
    # Large staging trees, so the disk-backed /var/tmp rather than the /tmp tmpfs.
    "TMPDIR": "/var/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
}


def _abs(p: str) -> str:
    # Bind sources are mounted after the sandbox has changed root, so they cannot stay relative.
    return str(Path(p).absolute())


def _relocate(value: str, cwd: str) -> str:
    """Point an absolute path into the project at where the sandbox mounts the project."""
    if value == cwd:
        return _PROJECT
    return value.replace(cwd + "/", _PROJECT + "/")


def _kv(pairs: list[str], sep: str) -> list[tuple[str, str]]:
    out = []
    for p in pairs:
        if sep not in p:
            raise SystemExit(f"expected SRC{sep}DST, got {p!r}")
        a, b = p.split(sep, 1)
        out.append((a, b))
    return out


def _relaxed(tools: Path) -> list[str]:
    """Mount pinned userspace over a host-integrated root."""
    out = []
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
    return out + _identity(tools)


def _identity(tools: Path) -> list[str]:
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
    out = []
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
    return out


def _box(name: str) -> list[str]:
    """Announce the box in the environment, counting depth when boxes nest."""
    previous = os.environ.get("TINE_BOX", "") if os.environ.get("TINE_IN_BOX") else ""
    if previous:
        level = _BOX_LEVEL.search(previous)
        name = f"{name}:{int(level.group('level')) + 1 if level else 2}"
    out = ["--setenv", "TINE_BOX", name, "--setenv", "TINE_IN_BOX", "1"]

    # Starship owns the prompt layout; TINE_BOX is rendered through its env_var module instead.
    if os.environ.get("STARSHIP_SHELL"):
        return out
    prefix = os.environ.get("SHELL_PROMPT_PREFIX", "")
    marker = f"({previous})"
    if previous and marker in prefix:
        prefix = prefix.replace(marker, f"({name})", 1)
    else:
        prefix = f"({name}){prefix}"
    return out + ["--setenv", "SHELL_PROMPT_PREFIX", prefix]


def _parse(argv: list[str] | None) -> argparse.Namespace:
    """Read the request, rejecting the flag combinations that describe no sandbox."""
    p = argparse.ArgumentParser(prog="sandbox")
    p.add_argument("--tools", required=True, help="ro exec-env chroot bound onto / (the pinned tools tree)")
    p.add_argument("--ro-bind", dest="ro_bind", action="append", default=[], help="SRC:DST ro bind")
    p.add_argument("--setenv", action="append", default=[], help="K=V environment")
    p.add_argument("--source-date-epoch", type=int, default=None)
    p.add_argument("--bind-cwd", dest="bind_cwd", action="store_true", help="bind+chdir the project root")
    p.add_argument("--network", action="store_true", help="grant network (default: unshared)")
    p.add_argument("--box", default=None, help="enter as the named development box (prompt and TINE_BOX)")
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
    if args.box and not args.relaxed:
        raise SystemExit("--box describes an interactive host-integrated shell; it requires --relaxed")
    return args


def _argv(args: argparse.Namespace) -> list[str]:
    """Translate the request into the vendored sandbox's own command line."""
    out: list[str] = []

    cwd = os.getcwd() if args.bind_cwd else None

    # Recreate usr-merge symlinks instead of binding through them.
    tools = Path(args.tools).resolve()
    if args.relaxed:
        out += _relaxed(tools)
    else:
        for entry in sorted(tools.iterdir()):
            if entry.name in _PROVIDED:
                continue
            dest = "/" + entry.name
            if entry.is_symlink():
                out += ["--symlink", str(entry.readlink()), dest]
            elif entry.is_dir():
                out += ["--ro-bind", str(entry), dest]

    for src, dest in _kv(args.ro_bind, ":"):
        out += ["--ro-bind", _abs(src), dest]

    chdir = None
    command = args.cmd
    if cwd:
        out += ["--bind", cwd, _PROJECT]
        chdir = _PROJECT

        # A build action names its artifacts project-relative, but `buck run` calls the same command with
        # an absolute path, so translate it for our PROJECT mount. Only the command needs it: a bind
        # source is resolved on the host, and a setenv value carries a path inside the sandbox already.
        command = [_relocate(argument, cwd) for argument in command]

    # Package scripts require writable API and temporary filesystems.
    out += ["--bind", "/proc", "/proc"]
    if not args.relaxed:
        out += ["--dev", "/dev"]
        out += ["--tmpfs", "/run"]
        out += ["--tmpfs", "/tmp"]

        # Everything large stages under /var/tmp, which TMPDIR points at: back it with Buck's
        # on-disk per-action scratch directory rather than a tmpfs, since staging trees run into
        # gigabytes and should not go into RAM. /tmp keeps the tmpfs above, for small and
        # short-lived files only.
        #
        # Entered outside a run action there is no scratch directory: `buck run` on a box or
        # its [resolve] subtarget, and every `buck test`. Those keep a tmpfs here too, which dies
        # with the mount namespace.
        staging = None
        if cwd and "BUCK_SCRATCH_PATH" in os.environ:
            staging = Path(cwd, os.environ["BUCK_SCRATCH_PATH"])
        if staging is None:
            out += ["--tmpfs", "/var/tmp"]
        else:
            backing = staging / "var-tmp"
            backing.mkdir(parents=True, exist_ok=True)
            out += ["--bind", str(backing), "/var/tmp"]

    if args.source_date_epoch is not None:
        out += ["--setenv", "SOURCE_DATE_EPOCH", str(args.source_date_epoch)]
    for k, v in _kv(args.setenv, "="):
        out += ["--setenv", k, v]
    if args.box:
        out += _box(args.box)
    if args.relaxed:
        # Resolve through the host /run while keeping the tools tree's /etc.
        if Path("/etc/resolv.conf").exists():
            out += ["--ro-bind-nofollow", "/etc/resolv.conf", "/etc/resolv.conf"]
        chdir = chdir or os.getcwd()
    elif args.network:
        # Preserve box CA trust but use the host resolver and its /run target.
        out += ["--ro-bind-nofollow", "/etc/resolv.conf", "/etc/resolv.conf", "--ro-bind", "/run", "/run"]
    else:
        out += ["--unshare-net"]
    if chdir:
        out += ["--chdir", chdir]

    # Builds need fakeroot semantics; relaxed tools must remain the invoking user.
    if not args.relaxed:
        out += ["--suppress-chown", "--suppress-sync", "--become-root"]
    out += ["--", *command]
    return out


def main(argv: list[str] | None = None) -> NoReturn:
    args = _parse(argv)
    # Composing reads the environment, so compose before replacing it below.
    out = _argv(args)

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
