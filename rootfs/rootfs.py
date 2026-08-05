"""Mount target roots and translate stored OCI markers to native overlayfs state.

Buck cannot store device nodes, xattrs, or backslashes in paths, so deltas encode them as
regular marker files. Sidecar layers reconstruct markers on entry; persisted uppers are
captured back to the storage form after unmounting.
"""

import os
import shutil
import stat
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from enum import Enum, auto
from pathlib import Path

from mkosi.sandbox import (
    MNT_DETACH,
    BindOperation,
    DevOperation,
    OverlayOperation,
    TmpfsOperation,
    umount2,
)
from mkosi.sandbox import chroot as _chroot  # the `chroot=` kwarg below shadows the name

_WH = ".wh."  # `.wh.<name>`: a whiteout hiding <name> from lower layers
_OPAQUE = ".wh..wh..opq"  # marks its parent dir opaque (hide everything below it)
_ESC = ".esc."  # `.esc.<escaped>`: a name buck can't store, percent-escaped
_OPAQUE_XATTR = b"user.overlay.opaque"  # an opaque dir's native form (userxattr namespace)


# OCI delta to native overlay translation.


def _escape(name: str) -> str:
    """Escape a path component Buck cannot store, preserving injectivity."""
    if "\\" not in name and not name.startswith(_ESC):
        return name
    return _ESC + name.replace("%", "%25").replace("\\", "%5C")


def _unescape(name: str) -> str:
    return name.removeprefix(_ESC).replace("%5C", "\\").replace("%25", "%")


class _Kind(Enum):
    """What a stored delta entry means once decoded."""

    NORMAL = auto()  # a plain entry, carried through under its true name
    WHITEOUT = auto()  # deletes the payload name from lower layers
    OPAQUE = auto()  # marks its parent dir opaque


def _classify(name: str) -> tuple[_Kind, str]:
    """Decode a stored name into its marker kind and payload."""
    true = _unescape(name) if name.startswith(_ESC) else name
    if true == _OPAQUE:
        return _Kind.OPAQUE, ""
    if true.startswith(_WH):
        return _Kind.WHITEOUT, true[len(_WH) :]
    return _Kind.NORMAL, true


def _whiteout_marker(name: str) -> str:
    """Encode a possibly unstorable whiteout target."""
    return _escape(_WH + name)


def _whiteout(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    os.mknod(str(directory / name), stat.S_IFCHR | 0o600, os.makedev(0, 0))


def _thaw(src: Path, dst: Path) -> None:
    """Copy an escaped entry under its true name, recursively decoding markers."""
    if src.is_symlink() or not src.is_dir():
        shutil.copy2(src, dst, follow_symlinks=False)
        return
    dst.mkdir(mode=stat.S_IMODE(src.lstat().st_mode))
    for child in src.iterdir():
        kind, payload = _classify(child.name)
        if kind is _Kind.OPAQUE:
            os.setxattr(str(dst), _OPAQUE_XATTR, b"y")
        elif kind is _Kind.WHITEOUT:
            _whiteout(dst, payload)
        else:
            _thaw(child, dst / payload)
    shutil.copystat(src, dst, follow_symlinks=False)


def _markers(delta: Path, dest: Path) -> tuple[Path | None, Path | None]:
    """Express stored markers as sparse sidecar layers above and below `delta`."""
    above, below = dest / "above", dest / "below"
    used_above = used_below = False
    for p in delta.rglob("*"):
        rel = p.parent.relative_to(delta)
        if any(part.startswith(_ESC) for part in rel.parts):
            continue  # inside an escaped dir: thawed wholesale when its ancestor was hit
        kind, payload = _classify(p.name)
        if kind is _Kind.OPAQUE:
            opaque = below / rel
            opaque.mkdir(parents=True, exist_ok=True)
            os.setxattr(str(opaque), _OPAQUE_XATTR, b"y")
            _whiteout(above / rel, p.name)  # hide the marker file itself
            used_above = used_below = True
        elif kind is _Kind.WHITEOUT:
            _whiteout(above / rel, payload)  # whiteout the deleted target
            _whiteout(above / rel, p.name)  # hide the literal marker file (`.wh.` or `.esc.`)
            used_above = True
        elif p.name.startswith(_ESC):  # an escaped normal entry
            (above / rel).mkdir(parents=True, exist_ok=True)
            _thaw(p, above / rel / payload)  # materialize it under its true name
            _whiteout(above / rel, p.name)  # hide the `.esc.` entry itself
            used_above = True
    return (above if used_above else None, below if used_below else None)


def _reconstruct(lowers: list[Path], scratch: Path) -> list[Path]:
    """Frame stored deltas as an overlayfs lowerdir list in top-to-bottom order."""
    components: list[Path] = []
    for i, lo in enumerate(reversed(lowers)):
        above, below = _markers(lo, scratch / f"m{i}")
        if above is not None:
            components.append(above)
        components.append(lo)
        if below is not None:
            components.append(below)
    return components


def _is_opaque(d: Path) -> bool:
    try:
        return os.getxattr(str(d), _OPAQUE_XATTR) == b"y"
    except OSError:
        return False


def capture(tree: Path) -> None:
    """Rewrite native overlay state and unstorable names into regular marker files."""
    # Buck must be able to delete and rename through package-supplied read-only directories.
    for p in tree.rglob("*"):
        st = p.lstat()
        if stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) & 0o700 != 0o700:
            p.chmod(stat.S_IMODE(st.st_mode) | 0o700)
    for p in sorted(tree.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        st = p.lstat()
        if stat.S_ISCHR(st.st_mode) and st.st_rdev == 0:
            p.unlink()
            (p.parent / _whiteout_marker(p.name)).write_bytes(b"")
            continue
        if stat.S_ISDIR(st.st_mode) and _is_opaque(p):
            os.removexattr(str(p), _OPAQUE_XATTR)
            (p / _OPAQUE).write_bytes(b"")
        escaped = _escape(p.name)
        if escaped != p.name:
            p.rename(p.parent / escaped)


# Root setup.


def _bind(src: str | Path, dst: str | Path) -> None:
    BindOperation(
        str(src), str(dst), readonly=False, required=True, foreign=False, relative=False, nofollow=False
    ).execute()


def _apivfs(stack: ExitStack, target: Path) -> None:
    """Mount the API and temporary filesystems expected by package scripts."""
    ttyname = os.ttyname(2) if os.isatty(2) else ""
    DevOperation(ttyname, str(target / "dev")).execute()
    stack.callback(umount2, str(target / "dev"), MNT_DETACH)
    _bind("/proc", target / "proc")
    stack.callback(umount2, str(target / "proc"), MNT_DETACH)
    TmpfsOperation(str(target / "run")).execute()
    stack.callback(umount2, str(target / "run"), MNT_DETACH)

    # The same split the sandbox makes for its own (engine/sandbox.py): /tmp is a tmpfs, for small
    # and short-lived files, and everything large belongs under /var/tmp, which gets Buck's on-disk
    # per-action scratch directory rather than RAM -- package scripts stage gigabytes there. Buck
    # clears the scratch path before each execution, so the backing neither accumulates nor collides
    # with an earlier run's leftovers. Outside a run action there is no scratch directory (`buck run`
    # on an engine, and `buck test`), and a tmpfs is all that is available.
    TmpfsOperation(str(target / "tmp")).execute()
    stack.callback(umount2, str(target / "tmp"), MNT_DETACH)

    staging = os.environ.get("BUCK_SCRATCH_PATH")
    if staging is None:
        TmpfsOperation(str(target / "var/tmp")).execute()
    else:
        Path(staging).mkdir(parents=True, exist_ok=True)
        # A unique name per call: one action can mount several roots.
        backing = Path(tempfile.mkdtemp(dir=staging, prefix="var-tmp."))
        backing.chmod(0o1777)  # what a tmpfs mounted on /var/tmp defaults to
        _bind(backing, target / "var/tmp")
    stack.callback(umount2, str(target / "var/tmp"), MNT_DETACH)


@contextmanager
def chroot(target: str | Path) -> Iterator[Path]:
    """Temporarily enter an already-mounted target root."""
    with _chroot(str(target)):
        yield Path("/")


@contextmanager
def rootfs(
    target: str | Path,
    *,
    bind: str | Path | None = None,
    lowers: Sequence[str | Path] | None = None,
    upperdir: str | Path | None = None,
    workdir: str | Path | None = None,
    apivfs: bool = False,
    binds: Sequence[tuple[str | Path, str | Path]] | None = None,
    chroot: bool = False,
) -> Iterator[Path]:
    """Mount a bind or overlay root, optionally chrooting and persisting an upper delta."""
    target = Path(target)
    with ExitStack() as stack:
        if lowers is not None:
            # Resolve Buck-relative paths before entering a possible chroot.
            resolved = [Path(lo).resolve() for lo in lowers]
            scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="rootfs.")))
            components = _reconstruct(resolved, scratch)
            if not components:
                empty = scratch / "empty"
                empty.mkdir()
                components.append(empty)
            if upperdir is not None:
                if workdir is None:
                    raise ValueError("rootfs(upperdir=...) needs a matching workdir=")
                upper, work = Path(upperdir), Path(workdir)
                stack.callback(capture, upper)  # runs after the overlay unmount below
            else:
                # A writable ephemeral upper also permits a single-component stack.
                upper, work = scratch / "upper", scratch / "work"
            upper.mkdir(parents=True, exist_ok=True)
            work.mkdir(parents=True, exist_ok=True)
            OverlayOperation(tuple(str(p) for p in components), str(upper), str(work), str(target)).execute()
            stack.callback(umount2, str(target), 0)
        elif bind is not None:
            _bind(bind, target)
            stack.callback(umount2, str(target), MNT_DETACH)
        else:
            raise ValueError("rootfs needs bind= or lowers=")
        if apivfs:
            _apivfs(stack, target)
        for src, dst in binds or []:
            dest = target / str(dst).lstrip("/")
            _bind(src, dest)  # BindOperation creates the mountpoint itself
            stack.callback(umount2, str(dest), MNT_DETACH)
        if chroot:
            # Enter last so callbacks resolve paths after the chroot unwinds.
            stack.enter_context(_chroot(str(target)))
        yield Path("/") if chroot else target
