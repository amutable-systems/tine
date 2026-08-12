#!/usr/bin/python3
"""List everything a logical image ships, as a UAPI.16 File Manifest.

The format is an RFC7464 JSON-SEQ sequence: a root object naming the media type, then one object
per inode, each directory immediately followed by its own contents. Two consumers want this: a
comparison of the paths two images ship (a path in both a system extension and the /usr it merges
onto silently shadows an OS file), and per-image size accounting. Both need every inode type an OS
tree holds, so a type that cannot be listed is an error rather than an omission.
"""

import base64
import hashlib
import json
import os
import stat
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import specs

import finalize


class Spec(finalize.ImageSpec):
    out: str


MEDIA_TYPE = "application/vnd.uapi.16.manifest"

# RFC7464 frames every JSON text with a leading record separator and a trailing line feed.
_RS = b"\x1e"
_LF = b"\n"

# A directory holding a manifest cannot also list it, so the format reserves the name at the top
# level; deeper in the tree it is an ordinary file name.
_RESERVED = "Uapi16Manifest"

_INODE_TYPES = (
    (stat.S_ISREG, "reg"),
    (stat.S_ISDIR, "dir"),
    (stat.S_ISLNK, "lnk"),
    (stat.S_ISFIFO, "fifo"),
    (stat.S_ISCHR, "chr"),
    (stat.S_ISBLK, "blk"),
    (stat.S_ISSOCK, "sock"),
)

_NANOSECONDS = 10**9


@dataclass(frozen=True)
class Entry:
    """One inode below the image root, under the name the manifest gives it."""

    name: str
    path: Path
    st: os.stat_result

    @property
    def inode(self) -> tuple[int, int]:
        return (self.st.st_dev, self.st.st_ino)


def inode_type(mode: int) -> str:
    for predicate, name in _INODE_TYPES:
        if predicate(mode):
            return name
    raise SystemExit(f"manifest: inode type {stat.S_IFMT(mode):#o} has no manifest representation")


def check_name(name: str) -> str:
    """Reject a path the format cannot encode, rather than listing the image incompletely.

    Nothing normalizes here: scandir yields neither "." nor ".." nor an empty component, so what
    is left is the format's own restrictions on the bytes and on the reserved file name.
    """
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise SystemExit(f"manifest: {name!r} is not valid UTF-8, which a manifest name must be") from None
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise SystemExit(f"manifest: {name!r} contains a control character")
    top = name.split("/", 1)[0]
    if top == _RESERVED or top.startswith(_RESERVED + "."):
        raise SystemExit(f"manifest: {name!r} is the reserved manifest file name")
    return name


def entries(tree: Path) -> Iterator[Entry]:
    """Yield every inode below `tree` in manifest order.

    Pre-order, because the format wants a directory's contents to follow it immediately, and sorted
    on the raw name bytes, which is an order neither the locale nor the filesystem can shift.
    """
    yield from _below(tree, "")


def _below(directory: Path, prefix: str) -> Iterator[Entry]:
    with os.scandir(directory) as scan:
        children = sorted(scan, key=lambda child: os.fsencode(child.name))
    for child in children:
        name = check_name(prefix + child.name)
        st = child.stat(follow_symlinks=False)
        yield Entry(name=name, path=Path(child.path), st=st)
        if stat.S_ISDIR(st.st_mode):
            yield from _below(Path(child.path), name + "/")


def hardlink_tokens(found: Sequence[Entry]) -> dict[tuple[int, int], int]:
    """Number the inodes the manifest reaches by more than one name, in the order it reaches them.

    A link count above one does not make a file hardlinked *here*: the other names may sit outside
    the image, and a directory's count is its subdirectories. Only a second name in the manifest
    does, so the tokens count reachable names rather than trusting st_nlink.
    """
    reachable = Counter(
        entry.inode for entry in found if entry.st.st_nlink > 1 and not stat.S_ISDIR(entry.st.st_mode)
    )
    tokens: dict[tuple[int, int], int] = {}
    for entry in found:
        if reachable[entry.inode] > 1 and entry.inode not in tokens:
            tokens[entry.inode] = len(tokens) + 1
    return tokens


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_object(entry: Entry, *, epoch: int, token: int | None = None) -> dict[str, object]:
    """The record for one inode, with its fields in the order the specification lists them."""
    kind = inode_type(entry.st.st_mode)
    obj: dict[str, object] = {"name": entry.name, "type": kind}
    target = os.fsencode(os.readlink(entry.path)) if kind == "lnk" else b""

    if kind == "reg":
        obj["size"] = entry.st.st_size
    elif kind == "lnk":
        obj["size"] = len(target)
    elif kind in ("chr", "blk"):
        obj["major"] = os.major(entry.st.st_rdev)
        obj["minor"] = os.minor(entry.st.st_rdev)
    if kind != "lnk":  # the format gives a symlink no mode
        obj["mode"] = stat.S_IMODE(entry.st.st_mode)
    obj["uid"] = entry.st.st_uid
    obj["gid"] = entry.st.st_gid
    # Clamped exactly as every other reproducible output here clamps: what a package stamped
    # survives, what this build stamped collapses onto the epoch.
    obj["mTime"] = min(entry.st.st_mtime_ns, epoch * _NANOSECONDS)
    if token is not None:
        obj["inodeToken"] = token
    if kind == "lnk":
        # A symlink target has no file of its own to point at, and the format recommends carrying
        # it inline; base64 also spares it the UTF-8 rule a name has to satisfy.
        obj["contents"] = [{"literal": base64.b64encode(target).decode("ascii")}]
    if kind == "reg":
        # No `contents`: for a regular file the format then implies the file of that name beside
        # the manifest, which is what this manifest describes.
        obj["sha256"] = _digest(entry.path)
    return obj


def _record(obj: dict[str, object]) -> bytes:
    return _RS + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + _LF


def records(tree: Path, epoch: int) -> Iterator[bytes]:
    """The whole sequence for `tree`: the root object, then one object per inode below it."""
    # The root object carries the media type alone, which is what marks the sequence a manifest.
    # Its mode and mtime would be the ephemeral overlay upper's rather than the image's, and the
    # format defaults a nameless first object to a directory anyway.
    yield _record({"mediaType": MEDIA_TYPE})
    found = list(entries(tree))
    tokens = hardlink_tokens(found)
    for entry in found:
        yield _record(file_object(entry, epoch=epoch, token=tokens.get(entry.inode)))


def write(tree: Path, out: Path, epoch: int) -> int:
    written = 0
    with out.open("wb") as stream:
        for record in records(tree, epoch):
            stream.write(record)
            written += 1
    return written


def main(argv: list[str] | None = None) -> None:
    spec: Spec = specs.parse("manifest", argv)

    out = Path(spec["out"])
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    with finalize.image(spec, program="manifest") as tree:
        written = write(tree, out, epoch)
    print(f"manifest: wrote {written} objects -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
