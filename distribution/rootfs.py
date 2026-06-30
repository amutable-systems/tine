"""rootfs — set up a target root inside the sandbox (and translate layer deltas).

`sandbox.py` provides only the exec environment (the engine's `/usr` + the kernel API mounts)
and become-root in a user+mount namespace. A driver that needs to install into, or run
commands against, a *target* tree sets that tree up itself here — reusing mkosi's own
`FSOperation` primitives — instead of asking the launcher to bind a fixed `/buildroot`.

`rootfs()` mounts the target and yields it: a plain bind, or an overlayfs over a stack of
layer *deltas*. It owns the whole overlay dance so callers don't. Each delta is stored as an
overlay upperdir with deletions encoded as OCI-changeset regular files — `.wh.<name>` (a
whiteout) and `.wh..wh..opq` (an opaque dir) — because buck's artifact model carries only
content + an exec bit (no device nodes, no xattrs). On the way in, `rootfs()` reconstructs
those markers into native overlay form *without copying any delta* (tiny sidecar marker
layers — see `_markers`); on the way out, with a writable `upperdir`, it captures the freshly
built upper back into OCI form so buck can store it. mkosi's `chroot` is re-exported (as
`rootfs.chroot`) for callers that then step into the mounted root.

A whiteout is a char 0:0 device node, which the kernel exempts from CAP_MKNOD (`vfs_mknod`'s
is_whiteout case); an opaque dir is `user.overlay.opaque=y` under an `-o userxattr` mount,
settable by the file owner — so both directions work from the sandbox's unprivileged userns.
Everything lives in the sandbox's private mount namespace, torn down on exit; we still unwind
in order (submounts → overlay unmount → capture) so the upper is consistent before buck reads it.
"""

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from mkosi.sandbox import (
    MNT_DETACH,
    BindOperation,
    DevOperation,
    OverlayOperation,
    TmpfsOperation,
    umount2,
)
from mkosi.sandbox import chroot as chroot  # re-exported so callers use `rootfs.chroot`

_WH = ".wh."  # `.wh.<name>`: a whiteout hiding <name> from lower layers
_OPAQUE = ".wh..wh..opq"  # marks its parent dir opaque (hide everything below it)


# ---- OCI delta <-> native overlay translation ----------------------------------------------


def _whiteout(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    os.mknod(str(directory / name), stat.S_IFCHR | 0o600, os.makedev(0, 0))


def _markers(delta: Path, dest: Path) -> tuple[Path | None, Path | None]:
    """Express `delta`'s OCI markers as sidecar overlay layers *without copying the delta*.

    Returns `(above, below)` — layers to stack directly above and below the delta — either
    `None` when unused. The delta itself stays a read-only lowerdir. `above` carries native
    char 0:0 whiteouts (both for each deleted target and to hide the literal `.wh.`/opaque
    marker files, which would otherwise leak into the merge); `below` carries empty opaque
    dirs, which must sit *below* the delta so they mask lower layers without hiding the
    delta's own contents. Both hold only device nodes and empty dirs, so they cost
    O(markers), not O(delta).
    """
    above, below = dest / "above", dest / "below"
    used_above = used_below = False
    for p in delta.rglob("*"):
        rel = p.parent.relative_to(delta)
        if p.name == _OPAQUE:
            opaque = below / rel
            opaque.mkdir(parents=True, exist_ok=True)
            os.setxattr(str(opaque), b"user.overlay.opaque", b"y")
            _whiteout(above / rel, _OPAQUE)  # hide the marker file itself
            used_above = used_below = True
        elif p.name.startswith(_WH):
            _whiteout(above / rel, p.name[len(_WH) :])  # whiteout the deleted target
            _whiteout(above / rel, p.name)  # hide the `.wh.` marker file itself
            used_above = True
    return (above if used_above else None, below if used_below else None)


def _reconstruct(lowers: list[Path], scratch: Path) -> list[Path]:
    """Turn the delta stack `lowers` (bottom..top) into an overlayfs lowerdir list (top..
    bottom), framing each delta with the sidecar marker layers `_markers` builds in
    `scratch`. No delta is copied. The bottom is always a full install root (no markers)."""
    components: list[Path] = []
    for i, lo in enumerate(reversed(lowers)):
        if i == len(lowers) - 1:
            components.append(lo)
            continue
        above, below = _markers(lo, scratch / f"m{i}")
        if above is not None:
            components.append(above)
        components.append(lo)
        if below is not None:
            components.append(below)
    return components


def _is_opaque(d: Path) -> bool:
    try:
        return os.getxattr(str(d), b"user.overlay.opaque") == b"y"
    except OSError:
        return False


def _capture(upper: Path) -> None:
    """Rewrite a freshly built overlay upper in place into OCI-changeset form so buck can
    store it as plain files: char 0:0 whiteouts become `.wh.<name>` files and
    `user.overlay.opaque` dirs get a `.wh..wh..opq` file."""
    for p in sorted(upper.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        st = p.lstat()
        if stat.S_ISCHR(st.st_mode) and st.st_rdev == 0:
            p.unlink()
            (p.parent / (_WH + p.name)).write_bytes(b"")
        elif stat.S_ISDIR(st.st_mode) and _is_opaque(p):
            os.removexattr(str(p), b"user.overlay.opaque")
            (p / _OPAQUE).write_bytes(b"")


# ---- root setup ----------------------------------------------------------------------------


def _bind(src: str | Path, dst: str | Path, *, readonly: bool = False) -> None:
    BindOperation(
        str(src), str(dst), readonly=readonly, required=True, foreign=False, relative=False, nofollow=False
    ).execute()


def _apivfs(stack: ExitStack, target: Path) -> None:
    """Mount the API filesystems a chrooted install/scriptlet expects under `target` (mkosi's
    apivfs set): /dev (+devpts), /proc, and writable /run, /tmp, /var/tmp."""
    ttyname = os.ttyname(2) if os.isatty(2) else ""
    DevOperation(ttyname, str(target / "dev")).execute()
    stack.callback(umount2, str(target / "dev"), MNT_DETACH)
    _bind("/proc", target / "proc")
    stack.callback(umount2, str(target / "proc"), MNT_DETACH)
    for sub in ("run", "tmp", "var/tmp"):
        TmpfsOperation(str(target / sub)).execute()
        stack.callback(umount2, str(target / sub), MNT_DETACH)


@contextmanager
def rootfs(
    target: str | Path,
    *,
    bind: str | Path | None = None,
    lowers: list[Path] | None = None,
    upperdir: str | Path | None = None,
    workdir: str | Path | None = None,
    apivfs: bool = False,
) -> Iterator[Path]:
    """Mount `target` as a root and yield it. Either `bind` (a tree bound rw) or `lowers` (a
    stack of stored deltas, overlay-merged). With `lowers`, an `upperdir`/`workdir` captures a
    persisted layer delta (`image.py`); without one, the overlay still gets an **ephemeral**
    upper so the merge is writable but throwaway — a caller (e.g. `pack.py`) can mutate the
    tree without copying it, and nothing is captured. Delta markers are reconstructed on the
    way in. Teardown unwinds in order (apivfs submounts → overlay unmount → capture)."""
    target = Path(target)
    with ExitStack() as stack:
        if lowers is not None:
            scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="rootfs.")))
            components = _reconstruct(lowers, scratch)
            if upperdir is not None:
                # Persisted delta (building a layer): capture the built upper to OCI on exit.
                if workdir is None:
                    raise ValueError("rootfs(upperdir=...) needs a matching workdir=")
                upper, work = Path(upperdir), Path(workdir)
                stack.callback(_capture, upper)  # runs after the overlay unmount below
            else:
                # Transient writable merge (e.g. pack dropping the rpmdb + running tmpfiles):
                # an ephemeral upper discarded with the scratch dir — never captured. Also lets
                # a single-component stack mount (a lone lowerdir with no upper is rejected).
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
        yield target
