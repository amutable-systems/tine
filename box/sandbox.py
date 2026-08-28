"""Run commands with pinned userspace through Tine's Linux sandbox.

The default mode provides a clean, isolated build environment. `--relaxed` retains the
pinned userspace but exposes host devices, services, environment, cwd, and network.
Target-root setup belongs to rootfs.py rather than this launcher.
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from isolation import Bind, Devices, Filesystem, Sandbox, SandboxOSError, Symlink, Tmpfs, enter

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

# Deterministic environment replacing the sandbox's inherited host environment.
_BASE_ENV = {
    "PATH": "/usr/bin:/usr/sbin:/bin:/sbin",
    "HOME": "/root",
    # Large staging trees, so the disk-backed /var/tmp rather than the /tmp tmpfs.
    "TMPDIR": "/var/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
}


@dataclass(frozen=True)
class Launch:
    """Pair one sandbox description with the process it launches."""

    sandbox: Sandbox
    command: tuple[str, ...]
    environment: dict[str, str]


def _abs(p: str) -> Path:
    # Bind sources are mounted after the sandbox has changed root, so they cannot stay relative.
    path = Path(p)
    return path if path.is_absolute() else Path.cwd() / path


def _relocate(value: str, cwd: Path) -> str:
    """Point an absolute path into the project at where the sandbox mounts the project."""
    current = str(cwd)
    if value == current:
        return _PROJECT
    return value.replace(current + "/", _PROJECT + "/")


def _kv(pairs: list[str], sep: str) -> list[tuple[str, str]]:
    out = []
    for p in pairs:
        if sep not in p:
            raise SystemExit(f"expected SRC{sep}DST, got {p!r}")
        a, b = p.split(sep, 1)
        out.append((a, b))
    return out


def _relaxed(tools: Path) -> list[Filesystem]:
    """Mount pinned userspace over a host-integrated root."""
    out: list[Filesystem] = []
    for name in _TOOLS_DIRS:
        if (tools / name).is_dir():
            out.append(Bind(tools / name, Path("/") / name, readonly=True))
    for name in _TOOLS_LINKS:
        entry = tools / name
        if entry.is_symlink():
            out.append(Symlink(entry.readlink(), Path("/") / name))
        elif entry.is_dir():
            out.append(Bind(entry, Path("/") / name, readonly=True))
    for entry in sorted(Path("/").iterdir()):
        if entry.name in _HOST_SKIP:
            continue
        if entry.is_symlink():
            out.append(Symlink(entry.readlink(), entry))
        else:
            out.append(Bind(entry, entry))
    if (tools / "etc").is_dir():
        out.append(Bind(tools / "etc", Path("/etc"), readonly=True))
    for f in _HOST_ETC:
        if Path("/etc", f).exists() and (tools / "etc" / f).exists():
            out.append(Bind(Path("/etc") / f, Path("/etc") / f, readonly=True))
    return out + _identity(tools)


def _identity(tools: Path) -> list[Filesystem]:
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
    out: list[Filesystem] = []
    for base, entry in tables.items():
        source = tools / "etc" / base
        content = source.read_text(encoding="utf-8") if source.exists() else ""
        # Deterministic per-uid path: overwritten each run rather than accumulated, and O_NOFOLLOW so
        # a pre-planted symlink can't redirect the write.
        path = Path("/var/tmp", f".tine-sandbox-{base}-{uid}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content + entry)
        out.append(Bind(path, Path("/etc") / base, readonly=True))
    return out


def _box(name: str) -> dict[str, str]:
    """Announce the box in the environment, counting depth when boxes nest."""
    previous = os.environ.get("TINE_BOX", "") if os.environ.get("TINE_IN_BOX") else ""
    if previous:
        level = _BOX_LEVEL.search(previous)
        name = f"{name}:{int(level.group('level')) + 1 if level else 2}"
    out = {"TINE_BOX": name, "TINE_IN_BOX": "1"}

    # Starship owns the prompt layout; TINE_BOX is rendered through its env_var module instead.
    if os.environ.get("STARSHIP_SHELL"):
        return out
    prefix = os.environ.get("SHELL_PROMPT_PREFIX", "")
    marker = f"({previous})"
    if previous and marker in prefix:
        prefix = prefix.replace(marker, f"({name})", 1)
    else:
        prefix = f"({name}){prefix}"
    out["SHELL_PROMPT_PREFIX"] = prefix
    return out


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


def _tty() -> Path | None:
    try:
        return Path(os.ttyname(2)) if os.isatty(2) else None
    except FileNotFoundError:
        return None


def _launch(args: argparse.Namespace) -> Launch:
    """Translate the command-line request into the Python sandbox interface."""
    filesystems: list[Filesystem] = []
    cwd = Path.cwd() if args.bind_cwd else None

    # Recreate usr-merge symlinks instead of binding through them.
    tools = Path(args.tools).resolve()
    if args.relaxed:
        filesystems += _relaxed(tools)
    else:
        for entry in sorted(tools.iterdir()):
            if entry.name in _PROVIDED:
                continue
            dest = Path("/") / entry.name
            if entry.is_symlink():
                filesystems.append(Symlink(entry.readlink(), dest))
            elif entry.is_dir():
                filesystems.append(Bind(entry, dest, readonly=True))

    for src, dest in _kv(args.ro_bind, ":"):
        filesystems.append(Bind(_abs(src), Path(dest), readonly=True))

    chdir: Path | None = None
    command = tuple(args.cmd)
    if cwd:
        filesystems.append(Bind(cwd, Path(_PROJECT)))
        chdir = Path(_PROJECT)

        # A build action names its artifacts project-relative, but `buck run` calls the same command with
        # an absolute path, so translate it for our PROJECT mount. Only the command needs it: a bind
        # source is resolved on the host, and a setenv value carries a path inside the sandbox already.
        command = tuple(_relocate(argument, cwd) for argument in command)

    # Package scripts require writable API and temporary filesystems.
    filesystems.append(Bind(Path("/proc"), Path("/proc")))
    if not args.relaxed:
        filesystems.append(Devices(Path("/dev"), _tty()))
        filesystems.append(Tmpfs(Path("/run")))
        filesystems.append(Tmpfs(Path("/tmp")))

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
            filesystems.append(Tmpfs(Path("/var/tmp")))
        else:
            backing = staging / "var-tmp"
            backing.mkdir(parents=True, exist_ok=True)
            filesystems.append(Bind(backing, Path("/var/tmp")))

    environment = dict(os.environ) if args.relaxed else dict(_BASE_ENV)
    if not args.relaxed and args.bind_cwd and "BUCK_SCRATCH_PATH" in os.environ:
        environment["BUCK_SCRATCH_PATH"] = os.environ["BUCK_SCRATCH_PATH"]
    if args.source_date_epoch is not None:
        environment["SOURCE_DATE_EPOCH"] = str(args.source_date_epoch)
    for k, v in _kv(args.setenv, "="):
        environment[k] = v
    if args.box:
        environment.update(_box(args.box))
    if args.relaxed:
        # Resolve through the host /run while keeping the tools tree's /etc.
        if Path("/etc/resolv.conf").exists():
            filesystems.append(
                Bind(
                    Path("/etc/resolv.conf"),
                    Path("/etc/resolv.conf"),
                    readonly=True,
                    nofollow=True,
                )
            )
        chdir = chdir or Path.cwd()
    elif args.network:
        # Preserve box CA trust but use the host resolver and its /run target.
        filesystems.append(
            Bind(
                Path("/etc/resolv.conf"),
                Path("/etc/resolv.conf"),
                readonly=True,
                nofollow=True,
            )
        )
        filesystems.append(Bind(Path("/run"), Path("/run"), readonly=True))

    return Launch(
        sandbox=Sandbox(
            filesystems=tuple(filesystems),
            chdir=chdir,
            become_root=not args.relaxed,
            isolate_network=not args.relaxed and not args.network,
            suppress_chown=not args.relaxed,
            suppress_sync=not args.relaxed,
        ),
        command=command,
        environment=environment,
    )


def main(argv: list[str] | None = None) -> NoReturn:
    args = _parse(argv)
    launch = _launch(args)
    try:
        enter(launch.sandbox)
    except SandboxOSError as error:
        print(error.message, file=sys.stderr)
        raise
    try:
        os.execvpe(launch.command[0], launch.command, launch.environment)
    except FileNotFoundError:
        raise SystemExit(127) from None
    raise SystemExit(127)


if __name__ == "__main__":
    main()
