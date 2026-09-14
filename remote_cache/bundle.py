"""The container holding everything one action result names.

A bundle is the unit which the bucket stores and a hit fetches: its members one after another, each a
`HEADER` followed by its bytes.
"""

import hashlib
import struct
from collections.abc import Iterable, Iterator, Mapping

import reapi

# The member's hash as 64 hex characters, then its size as a big-endian 64-bit number. No other
# metadata: a member is verified by hashing its bytes, so nothing else could be trusted anyway.
HEADER = struct.Struct(">64sQ")


def bundle_name(members: Iterable[reapi.Digest]) -> str:
    """The name of the bundle holding exactly the blobs with these digests.

    A digest of the member set rather than of the packed bytes, so the container format can change
    without renaming anything, and two actions whose outputs are identical share one object.
    """
    digest = hashlib.sha256()
    for member in sorted(set(members), key=lambda one: (one.hash, one.size_bytes)):
        digest.update(f"{member.hash}/{member.size_bytes}\n".encode())
    return digest.hexdigest()


def pack(blobs: Mapping[str, bytes]) -> bytes:
    """Pack blobs, each named by its own hash, into one bundle.

    Sorted, so the same member set always packs to the same bytes. Not relied on for anything, but
    a build cache that is itself reproducible is easier to validate.
    """
    # Uncompressed: compression is a decision we postponed, and it belongs to the bucket layer.
    return b"".join(HEADER.pack(hash_.encode(), len(blobs[hash_])) + blobs[hash_] for hash_ in sorted(blobs))


def unpack(data: bytes) -> Iterator[tuple[str, bytes]]:
    """Read a bundle, yielding each member only if its bytes hash to the name it is stored under.

    The check is not about our own packing but about the bucket: these bytes arrived from storage
    somebody else can write to, and a member that fails here must never reach Buck. Bytes that do
    not frame as members at all are the same kind of failure and are reported the same way, since a
    caller is deciding what to do about the bucket rather than about the container.
    """
    offset = 0
    while offset < len(data):
        if len(data) - offset < HEADER.size:
            raise ValueError(f"bundle ends in {len(data) - offset} bytes that are not a member")
        name, size = HEADER.unpack_from(data, offset)
        offset += HEADER.size
        if size > len(data) - offset:
            raise ValueError(f"bundle member {name!r} claims {size} bytes, {len(data) - offset} are left")
        blob = data[offset : offset + size]
        offset += size
        found = hashlib.sha256(blob).hexdigest()
        if found.encode() != name:
            raise ValueError(f"bundle member {name!r} hashes to {found}")
        yield found, blob
