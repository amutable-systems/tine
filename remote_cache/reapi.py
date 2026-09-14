"""The messages of the Bazel Remote Execution API that a cache has to understand.

Only the fields this shim reads or writes, and only the messages it exchanges: 20 fields across the
dozen messages Buck2's client sends and expects, out of the 55 the API defines. Field numbers are
stated here rather than inherited from a generated descriptor, so the wire contract we depend on is
one readable file. Correctness is verified by building against a real Buck2.

An ActionResult is kept as the bytes it arrived as. The shim stores and re-serves it unchanged and only
needs a few digests out of it, so parsing it fully would be unnecessary work, and re-serialising risks
breaking the signature.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Protocol, Self, override

import wire


class Wireable(Protocol):
    """Anything the server sends back. gRPC needs one serializer per method, not per type."""

    def to_bytes(self) -> bytes: ...


# Service names, as they appear in a gRPC method path.
ACTION_CACHE = "build.bazel.remote.execution.v2.ActionCache"
CAS = "build.bazel.remote.execution.v2.ContentAddressableStorage"
CAPABILITIES = "build.bazel.remote.execution.v2.Capabilities"
BYTESTREAM = "google.bytestream.ByteStream"

# google.rpc.Code, the few a cache answers with.
OK = 0
INVALID_ARGUMENT = 3
NOT_FOUND = 5

# DigestFunction.Value.SHA256 and SymlinkAbsolutePathStrategy.Value.ALLOWED.
SHA256 = 1
SYMLINKS_ALLOWED = 2


@dataclass(frozen=True, slots=True)
class Digest:
    hash: str
    size_bytes: int

    @classmethod
    def parse(cls, data: bytes) -> Self:
        one = wire.Message(data)
        return cls(hash=one.text(1), size_bytes=one.integer(2))

    @classmethod
    def of(cls, one: wire.Message | None) -> Self | None:
        return cls(hash=one.text(1), size_bytes=one.integer(2)) if one else None

    @classmethod
    def for_bytes(cls, data: bytes) -> Self:
        return cls(hash=hashlib.sha256(data).hexdigest(), size_bytes=len(data))

    def to_bytes(self) -> bytes:
        return wire.text(1, self.hash) + wire.varint(2, self.size_bytes)

    @override
    def __str__(self) -> str:
        return f"{self.hash}/{self.size_bytes}"


@dataclass(frozen=True, slots=True)
class Status:
    code: int
    message: str = ""

    def to_bytes(self) -> bytes:
        return wire.varint(1, self.code) + wire.text(2, self.message)

    @classmethod
    def parse(cls, data: bytes) -> Self:
        one = wire.Message(data)
        return cls(code=one.integer(1), message=one.text(2))


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What an action produced, as bytes plus the digests the shim has to resolve.

    `tree_digests` name Tree messages, each of which lists the files in an output directory, so
    resolving a result means reading blobs to find the rest of it.
    """

    raw: bytes
    file_digests: list[Digest] = field(default_factory=list)
    tree_digests: list[Digest] = field(default_factory=list)
    stdout: Digest | None = None
    stderr: Digest | None = None

    @classmethod
    def parse(cls, data: bytes) -> Self:
        one = wire.Message(data)
        return cls(
            raw=data,
            file_digests=[
                digest for output in one.children(2) if (digest := Digest.of(output.child(2))) is not None
            ],
            tree_digests=[
                digest for output in one.children(3) if (digest := Digest.of(output.child(3))) is not None
            ],
            stdout=Digest.of(one.child(6)),
            stderr=Digest.of(one.child(8)),
        )

    def to_bytes(self) -> bytes:
        return self.raw

    def named(self) -> list[Digest]:
        """Every digest this result names directly, tree contents aside."""
        named = [*self.file_digests, *self.tree_digests]
        named += [one for one in (self.stdout, self.stderr) if one is not None]
        return named


def tree_files(data: bytes) -> list[Digest]:
    """The digests of every file a Tree message lists, in its root and in its children."""
    tree = wire.Message(data)
    directories = [one for one in [tree.child(1)] if one is not None] + tree.children(2)
    return [
        digest
        for directory in directories
        for node in directory.children(1)
        if (digest := Digest.of(node.child(2))) is not None
    ]


@dataclass(frozen=True, slots=True)
class GetActionResultRequest:
    action_digest: Digest | None

    @classmethod
    def parse(cls, data: bytes) -> Self:
        return cls(action_digest=Digest.of(wire.Message(data).child(2)))

    def to_bytes(self) -> bytes:
        return wire.submessage(2, self.action_digest.to_bytes()) if self.action_digest else b""


@dataclass(frozen=True, slots=True)
class UpdateActionResultRequest:
    action_digest: Digest | None
    action_result: ActionResult | None

    @classmethod
    def parse(cls, data: bytes) -> Self:
        one = wire.Message(data)
        # Present or not, never "non-empty": a result with every field at its default is still a
        # result, and Buck2's permission probe is one.
        results = one.blobs(3)
        return cls(
            action_digest=Digest.of(one.child(2)),
            action_result=ActionResult.parse(results[-1]) if results else None,
        )

    def to_bytes(self) -> bytes:
        out = b""
        if self.action_digest:
            out += wire.submessage(2, self.action_digest.to_bytes())
        if self.action_result:
            out += wire.submessage(3, self.action_result.to_bytes())
        return out


@dataclass(frozen=True, slots=True)
class FindMissingBlobsRequest:
    blob_digests: list[Digest]

    @classmethod
    def parse(cls, data: bytes) -> Self:
        return cls(blob_digests=[Digest.parse(one.data) for one in wire.Message(data).children(2)])

    def to_bytes(self) -> bytes:
        return b"".join(wire.submessage(2, one.to_bytes()) for one in self.blob_digests)


@dataclass(frozen=True, slots=True)
class FindMissingBlobsResponse:
    missing_blob_digests: list[Digest]

    @classmethod
    def parse(cls, data: bytes) -> Self:
        return cls(missing_blob_digests=[Digest.parse(one.data) for one in wire.Message(data).children(2)])

    def to_bytes(self) -> bytes:
        return b"".join(wire.submessage(2, one.to_bytes()) for one in self.missing_blob_digests)


@dataclass(frozen=True, slots=True)
class Blob:
    """One blob in a batch, on the way in or out."""

    digest: Digest | None
    data: bytes = b""
    status: Status | None = None


@dataclass(frozen=True, slots=True)
class BatchUpdateBlobsRequest:
    blobs: list[Blob]

    @classmethod
    def parse(cls, data: bytes) -> Self:
        return cls(
            blobs=[
                Blob(digest=Digest.of(one.child(1)), data=one.blob(2))
                for one in wire.Message(data).children(2)
            ]
        )

    def to_bytes(self) -> bytes:
        return b"".join(
            wire.submessage(
                2,
                (wire.submessage(1, one.digest.to_bytes()) if one.digest else b"") + wire.blob(2, one.data),
            )
            for one in self.blobs
        )


@dataclass(frozen=True, slots=True)
class BatchUpdateBlobsResponse:
    blobs: list[Blob]

    @classmethod
    def parse(cls, data: bytes) -> Self:
        return cls(
            blobs=[
                Blob(
                    digest=Digest.of(one.child(1)),
                    status=Status.parse(one.blob(2)),
                )
                for one in wire.Message(data).children(1)
            ]
        )

    def to_bytes(self) -> bytes:
        return b"".join(
            wire.submessage(
                1,
                (wire.submessage(1, one.digest.to_bytes()) if one.digest else b"")
                + wire.submessage(2, (one.status or Status(OK)).to_bytes()),
            )
            for one in self.blobs
        )


@dataclass(frozen=True, slots=True)
class BatchReadBlobsRequest:
    digests: list[Digest]

    @classmethod
    def parse(cls, data: bytes) -> Self:
        return cls(digests=[Digest.parse(one.data) for one in wire.Message(data).children(2)])

    def to_bytes(self) -> bytes:
        return b"".join(wire.submessage(2, one.to_bytes()) for one in self.digests)


@dataclass(frozen=True, slots=True)
class BatchReadBlobsResponse:
    blobs: list[Blob]

    @classmethod
    def parse(cls, data: bytes) -> Self:
        return cls(
            blobs=[
                Blob(
                    digest=Digest.of(one.child(1)),
                    data=one.blob(2),
                    status=Status.parse(one.blob(3)),
                )
                for one in wire.Message(data).children(1)
            ]
        )

    def to_bytes(self) -> bytes:
        return b"".join(
            wire.submessage(
                1,
                (wire.submessage(1, one.digest.to_bytes()) if one.digest else b"")
                + wire.blob(2, one.data)
                + wire.submessage(3, (one.status or Status(OK)).to_bytes()),
            )
            for one in self.blobs
        )


@dataclass(frozen=True, slots=True)
class ReadRequest:
    resource_name: str
    read_offset: int = 0
    read_limit: int = 0

    @classmethod
    def parse(cls, data: bytes) -> Self:
        one = wire.Message(data)
        return cls(resource_name=one.text(1), read_offset=one.integer(2), read_limit=one.integer(3))

    def to_bytes(self) -> bytes:
        return (
            wire.text(1, self.resource_name)
            + wire.varint(2, self.read_offset)
            + wire.varint(3, self.read_limit)
        )


@dataclass(frozen=True, slots=True)
class ReadResponse:
    data: bytes

    @classmethod
    def parse(cls, data: bytes) -> Self:
        return cls(data=wire.Message(data).blob(10))

    def to_bytes(self) -> bytes:
        return wire.blob(10, self.data)


@dataclass(frozen=True, slots=True)
class WriteRequest:
    resource_name: str
    write_offset: int = 0
    finish_write: bool = False
    data: bytes = b""

    @classmethod
    def parse(cls, data: bytes) -> Self:
        one = wire.Message(data)
        return cls(
            resource_name=one.text(1),
            write_offset=one.integer(2),
            finish_write=one.flag(3),
            data=one.blob(10),
        )

    def to_bytes(self) -> bytes:
        return (
            wire.text(1, self.resource_name)
            + wire.varint(2, self.write_offset)
            + wire.flag(3, self.finish_write)
            + wire.blob(10, self.data)
        )


@dataclass(frozen=True, slots=True)
class WriteResponse:
    committed_size: int

    @classmethod
    def parse(cls, data: bytes) -> Self:
        return cls(committed_size=wire.Message(data).integer(1))

    def to_bytes(self) -> bytes:
        return wire.varint(1, self.committed_size)


@dataclass(frozen=True, slots=True)
class Empty:
    """A request whose contents we do not read, such as asking for capabilities."""

    @classmethod
    def parse(cls, data: bytes) -> Self:
        return cls()

    def to_bytes(self) -> bytes:
        return b""


@dataclass(frozen=True, slots=True)
class ServerCapabilities:
    """What the cache can do, which for a client is the batch limit and the compressor list.

    No compressors are offered, so Buck2 stays on the plain paths and its own 4 MB batch default
    applies unless this says otherwise.
    """

    max_batch_total_size_bytes: int
    update_enabled: bool = True
    digest_functions: tuple[int, ...] = (SHA256,)

    def to_bytes(self) -> bytes:
        cache = (
            wire.packed(1, self.digest_functions)
            + wire.submessage(2, wire.flag(1, self.update_enabled))
            + wire.varint(4, self.max_batch_total_size_bytes)
            + wire.varint(5, SYMLINKS_ALLOWED)
        )
        return (
            wire.submessage(1, cache) + wire.submessage(4, _semver(2, 0)) + wire.submessage(5, _semver(2, 3))
        )

    @classmethod
    def parse(cls, data: bytes) -> Self:
        cache = wire.Message(data).child(1) or wire.Message(b"")
        update = cache.child(2)
        return cls(
            max_batch_total_size_bytes=cache.integer(4),
            update_enabled=bool(update and update.flag(1)),
            digest_functions=tuple(cache.numbers(1)),
        )


def _semver(major: int, minor: int) -> bytes:
    return wire.varint(1, major) + wire.varint(2, minor)
