# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""A build cache for Buck2.

Serves the cache half of the Bazel Remote Execution API: action results and the blobs they name.
See docs/design/architecture.md.

Buck2 calls nine methods. It never asks to execute anything, so there is no Execution service.
"""

import argparse
import datetime
import functools
import json
import logging
import os
import sys
import threading
import time
import urllib.error
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent import futures
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast, override

import grpc
from cryptography.exceptions import UnsupportedAlgorithm

import bundle
import reapi
import signing
import wire
from bucket import Bucket, Reader, S3Writer, Writer
from status import ask, serving
from store import Store

# What Buck2 falls back to when a server advertises no limit of its own, so advertising the same
# keeps it on the path it would take against a real cache: batches below this, ByteStream above.
MAX_BATCH_SIZE = 4 * 1000 * 1000

# One ByteStream Read message. Buck2 decodes up to its own limit, well above this.
CHUNK_SIZE = 1 << 20

log = logging.getLogger("shim")


@dataclass(frozen=True, slots=True)
class Pointer:
    """What an `ac/<action digest>` object holds: which bundle, and what the result said.

    This is the only object in the bucket that is not content-addressed, so it is the only one an
    operator could swap for another without a digest catching it. It needs to be signed.
    The result is treated as opaque bytes.

    The bundle name is not itself trusted. It only says where to look, and every member that comes
    back is checked against its own digest, which the result carries.
    """

    bundle: str
    result: bytes


BUNDLE_FIELD = 1
RESULT_FIELD = 2


def ac_pack(pointer: Pointer) -> bytes:
    """The stored form, as a protobuf message so that a later field costs nothing to add."""
    return wire.text(BUNDLE_FIELD, pointer.bundle) + wire.blob(RESULT_FIELD, pointer.result)


def ac_unpack(data: bytes) -> Pointer:
    """Split a stored object, refusing one that does not name a bundle."""
    message = wire.Message(data)
    bundle_name = message.text(BUNDLE_FIELD)
    if not signing.is_sha256_hex(bundle_name):
        raise ValueError(f"pointer does not name a bundle: {bundle_name!r}")
    return Pointer(bundle=bundle_name, result=message.blob(RESULT_FIELD))


def abort(context: grpc.ServicerContext, code: grpc.StatusCode, message: str) -> NoReturn:
    """Fail this call.

    `ServicerContext.abort` raises, but grpc ships no type information saying so, and reading a
    servicer method is much easier when the lines after a failure are visibly unreachable.
    """
    context.abort(code, message)
    raise AssertionError("grpc abort returned")


class Counts:
    """What has happened since this shim started, by kind.

    These are for the status report, so that the user can see the kinds of errors easily.
    """

    def __init__(self) -> None:
        self._counter: Counter[str] = Counter()
        self._lock = threading.Lock()

    def add(self, what: str) -> None:
        with self._lock:
            self._counter[what] += 1

    def report(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._counter.items()))


class Local:
    """The local cache store, and populating it from the bucket.

    The store evicts blob bytes by size, but keeps the database row saying which bundle each blob arrived
    in. Buck2 may later ask for an evicted blob by digest alone, with no result lookup ahead of it to say
    where to look, so a blob missing from the store is fetched again from that bundle. That is why the
    servicers read blobs through this class rather than calling the store directly.
    """

    def __init__(self, store: Store, bucket: Bucket | None, counts: Counts | None = None) -> None:
        self.store = store
        self.bucket = bucket
        self.counts = counts or Counts()
        # Bundles that came back from the bucket not matching their name. A publisher normally
        # skips a bundle the bucket already holds, since the name says what is in it; one of these
        # is uploaded again regardless, or the bad object would sit there for its whole lifetime,
        # a miss for every action that shares it, with every rebuild pointing at it afresh.
        self.tainted: set[str] = set()

    def blob(self, digest: reapi.Digest) -> bytes | None:
        """A blob's bytes, fetching the bundle it arrived in if they are no longer here."""
        return self.store.blob(digest) if self.ensure([digest]) else None

    def ensure(self, digests: list[reapi.Digest]) -> bool:
        """Make all of these present, fetching whole bundles rather than asking per blob."""
        missing = [one for one, here in zip(digests, self.store.has_blobs(digests), strict=True) if not here]
        if not missing:
            return True
        if self.bucket is None:
            return False
        wanted = {name for one in missing if (name := self.store.bundle_of(one)) is not None}
        for name in wanted:
            # Every member is checked against its own name on the way out, and this bundle was
            # already proven against the result that named it when it first arrived. There is no
            # result to prove it against now, so only what this store already attributes to the
            # bundle is taken from it: a rewritten one could otherwise fill the store with junk
            # that hashes correctly, and re-point another bundle's blobs at itself.
            members = self.fetch_bundle(name)
            if members is not None:
                known = self.store.members_of(name)
                self.admit({member: blob for member, blob in members.items() if member in known}, name)
                self.counts.add("bundles refilled")
        return all(self.store.has_blobs(digests))

    def fetch_bundle(self, name: str) -> dict[str, bytes] | None:
        """A bundle's members by hash, or None with the reason logged and counted."""
        assert self.bucket is not None
        try:
            # Bounded by the store: a bundle that would not fit could not be kept anyway, and the
            # operator, not the builder, decides how large the object under this name is.
            packed = self.bucket.reader.get(f"bundle/{name}", self.store.max_bytes)
        except urllib.error.URLError as error:
            log.error("bundle %s cannot be read: %s", name[:12], error)
            self.counts.add("bucket errors")
            return None
        except ValueError as error:
            # A builder with a larger store published a bundle this one could not hold. Nothing is
            # wrong with it, so it is not refused and re-uploaded; it is simply a miss here, and
            # counted apart because a store sized too small for the build shows up as nothing else.
            log.error("bundle %s is not read: %s", name[:12], error)
            self.counts.add("bundles too large")
            return None
        if packed is None:
            log.info("bundle %s is gone from the bucket", name[:12])
            self.counts.add("bundles gone")
            return None
        try:
            return dict(bundle.unpack(packed))
        except ValueError as error:
            log.error("bundle %s is unreadable: %s", name[:12], error)
            self.refuse(name)
            return None

    def refuse(self, name: str) -> None:
        """This bundle is not what its name says. Remembered, so a publisher writes it again."""
        self.tainted.add(name)
        self.counts.add("bundles refused")

    def admit(self, members: dict[str, bytes], name: str) -> None:
        """Every member into the store, each remembering the bundle it can be fetched from again."""
        for member, blob in members.items():
            self.store.put_blob(reapi.Digest(hash=member, size_bytes=len(blob)), blob, bundle=name)


def referenced(
    lookup: Callable[[reapi.Digest], bytes | None], result: reapi.ActionResult
) -> list[reapi.Digest] | None:
    """Every blob an action result names, or None if the result cannot be resolved.

    An output directory names a Tree, and the Tree lists the files in it, so resolving one result
    means reading a blob to find the rest. This is the set a bundle holds. The lookup is a
    parameter because a bundle has to be resolved out of itself, before anything it carries is
    allowed into the store.
    """
    digests = result.named()
    for tree in result.tree_digests:
        data = lookup(tree)
        if data is None:
            return None
        digests += reapi.tree_files(data)
    return digests


def accepts_uploads(signer: signing.Signer | None, verifier: signing.Authority | None) -> bool:
    """Whether Buck may store results and blobs here.

    Refusing unsigned uploads from a reader keeps a build from filling the store with junk and evicting
    legit content. Not advertised as a capability, as Buck2 ignores those and probes by uploading instead.
    """
    return verifier is None or signer is not None


NOT_ACCEPTING = "a reader must not store uploads from Buck with a configured authority"


def capabilities(request: reapi.Empty, context: grpc.ServicerContext) -> reapi.ServerCapabilities:
    return reapi.ServerCapabilities(max_batch_total_size_bytes=MAX_BATCH_SIZE)


class ActionCache:
    """Action results, and the only place that talks to the bucket.

    Everything the CAS and ByteStream servicers hand out is local, because a result arrives from the
    bucket together with every blob it names. That is the whole point of the bundle: there is no
    later moment at which a blob could turn out to be missing.
    """

    def __init__(
        self,
        local: Local,
        signer: signing.Signer | None = None,
        verifier: signing.Authority | None = None,
    ) -> None:
        self.local = local
        self.store = local.store
        self.bucket = local.bucket
        self.counts = local.counts
        self.signer = signer
        self.verifier = verifier
        self.accepting = accepts_uploads(signer, verifier)
        # One lock per bundle being uploaded, so two results that share a bundle do not both send
        # it. Keyed by name and never cleaned up, which costs a lock object per distinct bundle a
        # shim ever publishes: a few dozen per build.
        self._uploads: dict[str, threading.Lock] = {}
        self._uploads_guard = threading.Lock()

    def _upload_lock(self, name: str) -> threading.Lock:
        with self._uploads_guard:
            return self._uploads.setdefault(name, threading.Lock())

    def get(
        self, request: reapi.GetActionResultRequest, context: grpc.ServicerContext
    ) -> reapi.ActionResult:
        action = request.action_digest
        if action is None or not signing.is_sha256_hex(action.hash):
            abort(context, grpc.StatusCode.INVALID_ARGUMENT, "no action digest")
        self.counts.add("lookups")
        found = self._kept(action)
        if found is None and self.bucket is not None:
            found = self._fetch(action)
        if found is None:
            log.info("AC miss %s", action.hash[:12])
            abort(context, grpc.StatusCode.NOT_FOUND, "no result for this action")
        _, result = found
        blobs = referenced(self.local.blob, result)
        # Present, then read: `ensure` fetches what is missing bundle by bundle, and reading is
        # what proves the bytes on disk still hash to their names.
        if (
            blobs is None
            or not self.local.ensure(blobs)
            or any(self.store.blob(one) is None for one in blobs)
        ):
            # A result we cannot back is a miss, never a hit that fails later: that is the whole
            # reason Buck2's "expired in the RE CAS" error stays out of reach.
            log.info("AC incomplete %s", action.hash[:12])
            self.counts.add("incomplete")
            abort(context, grpc.StatusCode.NOT_FOUND, "result names blobs this cache does not have")
        self.counts.add("hits")
        log.info("AC hit %s, %d blobs", action.hash[:12], len(blobs))
        return result

    def update(
        self, request: reapi.UpdateActionResultRequest, context: grpc.ServicerContext
    ) -> reapi.ActionResult:
        action, result = request.action_digest, request.action_result
        if action is None or result is None or not signing.is_sha256_hex(action.hash):
            abort(context, grpc.StatusCode.INVALID_ARGUMENT, "no action digest or no result")
        if not self.accepting:
            self.counts.add("uploads refused")
            abort(context, grpc.StatusCode.PERMISSION_DENIED, NOT_ACCEPTING)
        prepared = self._prepare(action, result)
        if prepared is None:
            return result
        payload, bundle_name, blobs = prepared
        self.store.put_result(action, payload)
        log.info("AC put %s", action.hash[:12])
        if self.bucket is not None and self.bucket.writer is not None:
            published = False
            try:
                self._publish(action, payload, bundle_name, blobs, self.bucket.writer)
                published = True
            finally:
                # Counted whatever went wrong, and the error still goes to Buck2, which logs a
                # failed upload as a warning and carries on: a builder that has stopped publishing
                # is otherwise invisible.
                if not published:
                    self.counts.add("publish failures")
        return result

    def _trusted(self, action: reapi.Digest, stored: bytes) -> tuple[Pointer, reapi.ActionResult]:
        """The pointer and result these bytes hold, if this shim may serve them, or a `ValueError`.

        One rule for bytes from the bucket and bytes from the store: the store is written by every
        build on the machine and outlives them all, so it is trusted no further than the bucket.
        Once keys are configured, nothing unsigned passes. Without keys, unsigned bytes pass
        through, and signed ones are still refused rather than taken as a shortcut: that way
        forgetting the keys cannot quietly turn signing off.
        """
        if self.verifier is not None:
            stored = self.verifier.unwrap(action.hash, stored)
        elif stored.startswith(signing.MAGIC):
            raise ValueError("signed, but this cache was given no trusted keys")
        pointer = ac_unpack(stored)
        return pointer, reapi.ActionResult.parse(pointer.result)

    def _kept(self, action: reapi.Digest) -> tuple[Pointer, reapi.ActionResult] | None:
        """What the store holds for this action, verified as if it had just come from the bucket."""
        stored = self.store.result(action)
        if stored is None:
            return None
        try:
            return self._trusted(action, stored)
        except urllib.error.URLError as error:
            # Verifying what the store holds can still need the bucket: the leaf certificate it was
            # signed under is fetched when the remembered one has run out. An unreachable bucket
            # says nothing about this pointer, so it stays where it is and this is a miss, the same
            # as any other read the bucket could not answer.
            log.error("AC %s cannot be verified: %s", action.hash[:12], error)
            self.counts.add("bucket errors")
            return None
        except ValueError as error:
            # Written by something other than a verified fetch, or signed by a leaf that has since
            # run out. Dropped, so that the bucket's copy is looked up instead.
            log.error("AC %s in the store refused: %s", action.hash[:12], error)
            self.counts.add("pointers refused")
            self.store.drop_result(action)
            return None

    def _fetch(self, action: reapi.Digest) -> tuple[Pointer, reapi.ActionResult] | None:
        """Read a result and everything it names, or nothing, which leaves this a cache miss.

        Fetching the bundle *is* the existence check. Nothing is remembered about what the bucket
        holds, so nothing can be stale, and a pointer whose bundle an age rule has deleted costs one
        request and a rebuild.
        """
        assert self.bucket is not None
        try:
            stored = self.bucket.reader.get(f"ac/{action.hash}")
            if stored is None:
                self.counts.add("misses")
                return None
            pointer, result = self._trusted(action, stored)
            # Another pointer to the same bundle, usually: the same outputs under an action digest
            # that differs only in configuration. The pointer is signed and every blob it names is
            # content-addressed and already proven, so there is nothing left for the bundle to
            # prove, and a hit stays at one request rather than re-downloading the same bundle.
            local = referenced(self.store.blob, result)
            if local is not None and all(self.store.has_blobs(local)):
                self.store.put_result(action, stored)
                # The blobs may have come from this machine's own build and know no bundle yet;
                # the signed pointer says which one has them, and that is what an eviction needs.
                for digest in local:
                    self.store.remember(digest, pointer.bundle)
                log.info("AC from bucket %s, %d blobs already here", action.hash[:12], len(local))
                return pointer, result
        except urllib.error.URLError as error:
            # A broken endpoint. Loud, and still a miss: the build rebuilds rather than failing on
            # something the bucket did.
            log.error("AC %s unreadable from the bucket: %s", action.hash[:12], error)
            self.counts.add("bucket errors")
            return None
        except ValueError as error:
            # A forged, unsigned, or unreadable pointer. The same miss, counted apart, because a
            # reader given the wrong keys sees nothing but these.
            log.error("AC %s refused: %s", action.hash[:12], error)
            self.counts.add("pointers refused")
            return None
        members = self.local.fetch_bundle(pointer.bundle)
        if members is None:
            return None
        try:
            blobs = self._proven(pointer, result, members)
        except ValueError as error:
            log.error("AC %s: %s", action.hash[:12], error)
            self.local.refuse(pointer.bundle)
            return None
        self.local.admit(members, pointer.bundle)
        self.store.put_result(action, stored)
        log.info(
            "AC from bucket %s, bundle %s, %d blobs",
            action.hash[:12],
            pointer.bundle[:12],
            len(blobs),
        )
        return pointer, result

    @staticmethod
    def _proven(
        pointer: Pointer, result: reapi.ActionResult, members: dict[str, bytes]
    ) -> list[reapi.Digest]:
        """What the result names, once the bundle is shown to be exactly that, or a `ValueError`.

        Resolved out of the bundle itself, so that nothing it carries reaches the store until the
        whole of it is proven to be what the pointer says. Each member was already checked against
        its own name on the way out of the bundle, so what is left to check is the set: that the
        bundle holds nothing the result does not name and lacks nothing it does, and that the set
        hashes to the name the bundle was stored under.
        """
        blobs = referenced(lambda digest: members.get(digest.hash), result)
        if blobs is None:
            raise ValueError(f"bundle {pointer.bundle[:12]} does not resolve the result")
        named = {digest.hash for digest in blobs}
        if named != set(members):
            raise ValueError(
                f"bundle {pointer.bundle[:12]} holds {len(members)} members, the result names {len(named)}"
            )
        derived = bundle.bundle_name(blobs)
        if derived != pointer.bundle:
            raise ValueError(f"bundle {pointer.bundle[:12]} has contents that make {derived[:12]}")
        return blobs

    def _prepare(
        self, action: reapi.Digest, result: reapi.ActionResult
    ) -> tuple[bytes, str, list[reapi.Digest]] | None:
        """The pointer for a result Buck handed over, and the blobs it names, or None with the reason logged.

        Signed if this shim signs. The same bytes go into the store and, on a builder, into the
        bucket: what is served back later is verified like anything else, so the store has to hold
        the form that passes.
        """
        blobs = referenced(self.store.blob, result)
        if blobs is None or not all(self.store.has_blobs(blobs)):
            log.error("not storing %s: its outputs were not all uploaded", action.hash[:12])
            return None
        bundle_name = bundle.bundle_name(blobs)
        payload = ac_pack(Pointer(bundle=bundle_name, result=result.raw))
        if self.signer is not None:
            # Before anything is kept: a signer that has run out of certificate has nothing to point
            # at a bundle with, and an unsigned pointer is one this shim would refuse to serve.
            try:
                payload = self.signer.wrap(action.hash, payload)
            except ValueError as error:
                log.error("not storing %s: %s", action.hash[:12], error)
                return None
        return payload, bundle_name, blobs

    def _publish(
        self,
        action: reapi.Digest,
        payload: bytes,
        bundle_name: str,
        blobs: list[reapi.Digest],
        writer: Writer,
    ) -> None:
        """Write one bundle holding every blob the result names, then the pointer to it.

        Bundle first, always: a pointer published ahead of its contents is the one ordering that can
        be observed as a broken hit.
        """
        key = f"bundle/{bundle_name}"
        # A bundle is named by what is in it, so one that is already there is already right. Asking
        # costs one request and saves re-sending every byte, which is what makes the indirection
        # worth having: actions whose digests differ but whose outputs do not are the common case.
        # The same request re-dates the bundle, so it ages from this pointer rather than its first.
        #
        # Under the lock, because two results finishing together share a bundle often enough to
        # matter: a full build sent 39 uploads for 33 bundles before this, and one of them was 24
        # MB. Waiting rather than skipping ahead is the point. A second publisher that assumed the
        # first would finish could write its pointer before the bundle was there, which is the one
        # ordering below that can be seen as a broken hit.
        with self._upload_lock(bundle_name):
            # A bucket ages an object from when it was written, which happens only once for all results
            # that share it. Left alone, the most shared bundles would be the first to expire, even
            # with recently written pointers. Refreshing on reuse makes a bundle as old as its
            # newest pointer, so one age rule can cover both.
            shared = bundle_name not in self.local.tainted and writer.refresh(key)
            if not shared:
                members = {}
                for digest in blobs:
                    data = self.store.blob(digest)
                    if data is None:
                        log.error("not publishing %s: %s is gone from the store", action.hash[:12], digest)
                        return
                    members[digest.hash] = data
                writer.put(key, bundle.pack(members))
                self.local.tainted.discard(bundle_name)
        writer.put(f"ac/{action.hash}", payload)
        # Now that it is there, every blob remembers which bundle it can be fetched from again.
        for digest in blobs:
            self.store.remember(digest, bundle_name)
        self.counts.add("published")
        if not shared:
            self.counts.add("bundles uploaded")
        log.info(
            "published %s as %s bundle %s, %d blobs",
            action.hash[:12],
            "shared" if shared else "new",
            bundle_name[:12],
            len(blobs),
        )


class ContentAddressableStorage:
    def __init__(self, local: Local, accepting: bool) -> None:
        self.local = local
        self.store = local.store
        self.accepting = accepting

    def find_missing(
        self, request: reapi.FindMissingBlobsRequest, context: grpc.ServicerContext
    ) -> reapi.FindMissingBlobsResponse:
        digests = request.blob_digests
        answers = zip(digests, self.store.has_blobs(digests), strict=True)
        missing = [digest for digest, found in answers if not found]
        log.info("find missing: %d of %d", len(missing), len(digests))
        return reapi.FindMissingBlobsResponse(missing_blob_digests=missing)

    def batch_update(
        self, request: reapi.BatchUpdateBlobsRequest, context: grpc.ServicerContext
    ) -> reapi.BatchUpdateBlobsResponse:
        if not self.accepting:
            self.local.counts.add("uploads refused")
            abort(context, grpc.StatusCode.PERMISSION_DENIED, NOT_ACCEPTING)
        answers = []
        for entry in request.blobs:
            status = reapi.Status(code=reapi.OK)
            if entry.digest is None:
                status = reapi.Status(code=reapi.INVALID_ARGUMENT, message="no digest")
            else:
                try:
                    self.store.put_blob(entry.digest, entry.data)
                except ValueError as error:
                    log.error("batch put %s: %s", entry.digest, error)
                    status = reapi.Status(code=reapi.INVALID_ARGUMENT, message=str(error))
            answers.append(reapi.Blob(digest=entry.digest, status=status))
        log.info("batch put: %d blobs", len(answers))
        return reapi.BatchUpdateBlobsResponse(blobs=answers)

    def batch_read(
        self, request: reapi.BatchReadBlobsRequest, context: grpc.ServicerContext
    ) -> reapi.BatchReadBlobsResponse:
        answers = []
        for digest in request.digests:
            data = self.local.blob(digest)
            answers.append(
                reapi.Blob(
                    digest=digest,
                    data=data or b"",
                    status=reapi.Status(code=reapi.OK if data is not None else reapi.NOT_FOUND),
                )
            )
        found = sum(1 for one in answers if one.status and one.status.code == reapi.OK)
        log.info("batch read: %d of %d", found, len(answers))
        return reapi.BatchReadBlobsResponse(blobs=answers)


def resource_digest(resource: str) -> reapi.Digest:
    """The digest a ByteStream resource name carries.

    Buck2 reads `[<instance>/]blobs/<hash>/<size>` and writes
    `[<instance>/]uploads/<uuid>/blobs/<hash>/<size>`, so the pair after the `blobs` segment is the
    digest either way. A compressed resource says `compressed-blobs` instead and fails here, which
    is what we want while no compressor is advertised.
    """
    parts = resource.split("/")
    if "blobs" not in parts:
        raise ValueError(f"not a plain blob resource: {resource!r}")
    index = parts.index("blobs")
    if len(parts) < index + 3 or not signing.is_sha256_hex(parts[index + 1]):
        raise ValueError(f"truncated or malformed resource name: {resource!r}")
    return reapi.Digest(hash=parts[index + 1], size_bytes=int(parts[index + 2]))


class ByteStream:
    def __init__(self, local: Local, accepting: bool) -> None:
        self.local = local
        self.store = local.store
        self.accepting = accepting

    def read(
        self, request: reapi.ReadRequest, context: grpc.ServicerContext
    ) -> Iterator[reapi.ReadResponse]:
        try:
            digest = resource_digest(request.resource_name)
        except ValueError as error:
            abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(error))
        data = self.local.blob(digest)
        if data is None:
            log.info("read miss %s", digest)
            abort(context, grpc.StatusCode.NOT_FOUND, "no such blob")
        end = len(data) if not request.read_limit else request.read_offset + request.read_limit
        window = data[request.read_offset : end]
        log.info("read %s, %d bytes", digest, len(window))
        for start in range(0, len(window), CHUNK_SIZE) or [0]:
            yield reapi.ReadResponse(data=window[start : start + CHUNK_SIZE])

    def write(
        self, requests: Iterator[reapi.WriteRequest], context: grpc.ServicerContext
    ) -> reapi.WriteResponse:
        if not self.accepting:
            self.local.counts.add("uploads refused")
            abort(context, grpc.StatusCode.PERMISSION_DENIED, NOT_ACCEPTING)
        digest = None
        chunks: list[bytes] = []
        for request in requests:
            if digest is None:
                try:
                    digest = resource_digest(request.resource_name)
                except ValueError as error:
                    abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(error))
                if request.write_offset != 0:
                    # Resuming an interrupted upload. Buck2 always starts at zero, so refusing is
                    # honest rather than limiting; if this ever fires, a kill criterion did.
                    abort(context, grpc.StatusCode.UNIMPLEMENTED, "resumed uploads are not served")
            chunks.append(request.data)
            if request.finish_write:
                break
        if digest is None:
            abort(context, grpc.StatusCode.INVALID_ARGUMENT, "no write request received")
        data = b"".join(chunks)
        try:
            self.store.put_blob(digest, data)
        except ValueError as error:
            log.error("write %s: %s", digest, error)
            abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(error))
        log.info("write %s, %d bytes", digest, len(data))
        return reapi.WriteResponse(committed_size=len(data))

    def query_write_status(self, request: reapi.Empty, context: grpc.ServicerContext) -> reapi.Empty:
        # Buck2 never calls this, and answering wrongly would let it skip an upload.
        abort(context, grpc.StatusCode.UNIMPLEMENTED, "write status is not tracked")


def _serialize(message: reapi.Wireable) -> bytes:
    return message.to_bytes()


def handlers(
    local: Local,
    signer: signing.Signer | None = None,
    verifier: signing.Authority | None = None,
) -> tuple[grpc.GenericRpcHandler, grpc.GenericRpcHandler, grpc.GenericRpcHandler, grpc.GenericRpcHandler]:
    """The four services, as gRPC sees them: a method name, a codec pair, and a function.

    This is the whole of what a generated `_pb2_grpc` module would have done, and keeping it here
    means the method names Buck2 calls are visible rather than buried.
    """
    action_cache = ActionCache(local, signer, verifier)
    cas = ContentAddressableStorage(local, action_cache.accepting)
    stream = ByteStream(local, action_cache.accepting)

    # The casts are load-bearing: grpc ships no type information, and without them the checker
    # cannot see that these are handlers.
    def method(
        kind: Callable[..., object], behavior: Callable[..., object], parse: Callable[[bytes], object]
    ) -> grpc.RpcMethodHandler:
        return cast(
            grpc.RpcMethodHandler,
            kind(behavior, request_deserializer=parse, response_serializer=_serialize),
        )

    unary = functools.partial(method, grpc.unary_unary_rpc_method_handler)

    def generic(service: str, methods: dict[str, grpc.RpcMethodHandler]) -> grpc.GenericRpcHandler:
        return cast(grpc.GenericRpcHandler, grpc.method_handlers_generic_handler(service, methods))

    return (
        generic(
            reapi.CAPABILITIES,
            {"GetCapabilities": unary(capabilities, reapi.Empty.parse)},
        ),
        generic(
            reapi.ACTION_CACHE,
            {
                "GetActionResult": unary(action_cache.get, reapi.GetActionResultRequest.parse),
                "UpdateActionResult": unary(action_cache.update, reapi.UpdateActionResultRequest.parse),
            },
        ),
        generic(
            reapi.CAS,
            {
                "FindMissingBlobs": unary(cas.find_missing, reapi.FindMissingBlobsRequest.parse),
                "BatchUpdateBlobs": unary(cas.batch_update, reapi.BatchUpdateBlobsRequest.parse),
                "BatchReadBlobs": unary(cas.batch_read, reapi.BatchReadBlobsRequest.parse),
            },
        ),
        generic(
            reapi.BYTESTREAM,
            {
                "Read": method(grpc.unary_stream_rpc_method_handler, stream.read, reapi.ReadRequest.parse),
                "Write": method(
                    grpc.stream_unary_rpc_method_handler, stream.write, reapi.WriteRequest.parse
                ),
                "QueryWriteStatus": unary(stream.query_write_status, reapi.Empty.parse),
            },
        ),
    )


def alive(pid: int) -> bool:
    """Whether that process is still there.

    A pid the kernel has handed to someone else since reads as alive, which costs this shim one
    more idle window and nothing else.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It belongs to another user now, so the build that registered it is long gone.
        return False
    return True


class Activity(grpc.ServerInterceptor):
    """When this was last asked for anything, and which builds are still running.

    One touch per call, at the point the handler is looked up, which is why an idle timeout has to
    be much longer than any single RPC can take. At fifteen minutes against uploads measured in
    seconds that is not close, and Buck2 surrounds a large transfer with small calls anyway.

    A whole *build*, though, easily goes that long without a cache call: one miss and then a compile
    or an rpm build that takes an hour. Timing out under it would fail its next lookup with
    UNAVAILABLE, so `tine buck` registers itself here before handing over to Buck, and a shim counts
    as idle only once every build that registered has gone.
    """

    def __init__(self) -> None:
        self.last = time.monotonic()
        self._building: set[int] = set()
        self._lock = threading.Lock()

    def attend(self, pid: int) -> None:
        """Take a process that is about to build: this shim must outlive it."""
        with self._lock:
            self._building.add(pid)
        self.last = time.monotonic()

    def building(self) -> int:
        """How many registered builds are still running. The rest are forgotten here."""
        with self._lock:
            self._building = {pid for pid in self._building if alive(pid)}
            return len(self._building)

    @override
    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler | None],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        self.last = time.monotonic()
        return continuation(handler_call_details)


def build_server(
    port: int,
    store: Store,
    workers: int = 32,
    bucket: Bucket | None = None,
    signer: signing.Signer | None = None,
    verifier: signing.Authority | None = None,
    activity: Activity | None = None,
    counts: Counts | None = None,
) -> tuple[grpc.Server, Store, int]:
    """A server bound to `port`, not yet started, with the store it serves.

    Handing back the store is what lets a test assert on what a build actually left behind, rather
    than reading it back out through the API it is testing.
    """
    server = cast(
        grpc.Server,
        grpc.server(
            futures.ThreadPoolExecutor(max_workers=workers),
            interceptors=[activity] if activity is not None else None,
        ),
    )
    server.add_generic_rpc_handlers(handlers(Local(store, bucket, counts), signer, verifier))
    # Port 0 asks the kernel for a free one, which a test wants and a person does not.
    bound = cast(int, server.add_insecure_port(f"127.0.0.1:{port}"))
    return server, store, bound


def wait(server: grpc.Server, activity: Activity, idle_timeout: float) -> str:
    """Until the server stops, or until nothing has asked it anything for `idle_timeout`.

    No polling: each wait runs exactly to the deadline the last call set, and a call that lands
    meanwhile moves the deadline, so the next wait is simply recomputed. A build that registered
    itself holds the shim open past that deadline, at the cost of one liveness check per timeout,
    which is also how long a shim can outlive the last build that wanted it.
    """
    while True:
        if idle_timeout <= 0:
            server.wait_for_termination()
            return "stopped"
        left = activity.last + idle_timeout - time.monotonic()
        if left <= 0:
            if not activity.building():
                return "idle"
            left = idle_timeout
        # Reads the wrong way round at a glance: grpc returns whether the *timeout* expired, so a
        # false here is the server having terminated under us.
        if not server.wait_for_termination(timeout=left):
            return "stopped"


def serve(
    port: int,
    workers: int,
    store: Store,
    bucket: Bucket | None,
    signer: signing.Signer | None,
    verifier: signing.Authority | None,
    idle_timeout: float = 0,
) -> None:
    activity = Activity()
    counts = Counts()
    server, store, bound = build_server(port, store, workers, bucket, signer, verifier, activity, counts)
    server.start()
    trust = "unsigned" if verifier is None else verifier.describe()
    if signer is not None:
        trust += f", signing as {signer.id.hex()}"
    where = bucket.describe() if bucket else "no bucket"
    log.warning("serving 127.0.0.1:%d, %s, %s", bound, where, trust)

    def report() -> dict[str, object]:
        """What a running shim says about itself. Counted afresh, because a stale answer is worse."""
        blobs, results = store.counts()
        return {
            "pid": os.getpid(),
            # Verbatim, so whoever would start a shim can tell whether this one is the one they want.
            "argv": sys.argv[1:],
            "address": f"127.0.0.1:{bound}",
            "store": str(store.root),
            "bucket": where,
            "trust": trust,
            "counts": counts.report(),
            "blobs": blobs,
            "results": results,
            "held_bytes": store.held,
            "max_bytes": store.max_bytes,
            "max_rows": store.max_rows,
            "idle_seconds": round(time.monotonic() - activity.last, 1),
            "idle_timeout_seconds": idle_timeout,
            "builds": activity.building(),
        }

    why = "interrupted"
    with ExitStack() as stack:
        try:
            stack.enter_context(serving(store.root, report, activity.attend))
        except OSError as error:
            # Being able to answer "how are you" is a convenience; serving the cache is the job.
            log.error("no status socket: %s", error)
        try:
            why = wait(server, activity, idle_timeout)
        except KeyboardInterrupt:
            pass
    blobs, results = store.counts()
    happened = ", ".join(f"{count} {what}" for what, count in counts.report().items()) or "nothing asked"
    log.warning("%s, holding %d blobs and %d action results; %s", why, blobs, results, happened)
    server.stop(0)


def configured_bucket(args: argparse.Namespace) -> Bucket | None:
    """The bucket the arguments describe, or None for a shim with nothing behind it."""
    if args.read_url is None:
        # A builder reads too, and tine's settings validation already insists on it.
        assert args.s3_bucket is None, "--s3-bucket without --read-url"
        return None
    writer = None
    if args.s3_bucket is not None:
        if args.s3_key_file is None or not args.s3_endpoint:
            sys.exit("--s3-bucket needs --s3-key-file and --s3-endpoint")
        # One line, `<key id> <secret>`, as tine's cache settings already expect.
        parts = args.s3_key_file.read_text(encoding="utf-8").split()
        if len(parts) != 2:
            sys.exit(f"{args.s3_key_file} must hold an access key id and a secret")
        writer = S3Writer(
            endpoint=args.s3_endpoint,
            bucket=args.s3_bucket,
            access_key=parts[0],
            secret_key=parts[1],
            secure=not args.s3_insecure,
        )
    return Bucket(Reader(args.read_url), writer)


def configured_trust(
    args: argparse.Namespace, bucket: Bucket | None, store: Store
) -> tuple[signing.Signer | None, signing.Authority | None]:
    """Who this signs as and whose results it will serve, and the rules about how they go together.

    A bucket with no authority is refused unless the caller says `--unsigned` in so many words. The
    bucket operator is the adversary the signature exists for, and a reader that quietly took
    unsigned objects because nobody gave it keys would be trusting exactly that operator.
    Forgetting the keys has to fail at startup, never downgrade.

    The store keeps the leaf certificates the authority accepts, so that what it holds can be
    verified after a restart without asking the bucket.
    """
    signer = None
    trust = None
    # A key file that is missing, unreadable, not a key, or a key of a kind cryptography does not
    # build is a configuration mistake, and gets one line like every other one here.
    try:
        if args.signing_key:
            signer = signing.Signer(
                args.signing_key, args.signing_certificate, datetime.timedelta(days=args.object_lifetime)
            )
            signer.check_current()
        if args.authority:
            if bucket is None:
                sys.exit("--authority needs a bucket: leaf certificates are fetched from it")
            trust = signing.Authority(
                (one.read_text(encoding="utf-8") for one in args.authority), bucket.reader.get, store
            )
    except (ValueError, OSError, UnsupportedAlgorithm) as error:
        sys.exit(str(error))
    if trust is None and bucket is not None and not args.unsigned:
        sys.exit("a bucket needs --authority, or --unsigned to trust whoever writes it")
    if signer is not None:
        if trust is None:
            # It would fill a cache nobody, itself included, can read.
            sys.exit("--signing-key needs --authority to say who may read the results")
        try:
            trust.admit(signer)
        except ValueError as error:
            sys.exit(f"this cache would not read its own results back: {error}")
        publish_certificate(bucket, signer)
    return signer, trust


def publish_certificate(bucket: Bucket | None, signer: signing.Signer) -> None:
    """Put the builder's own certificate where readers look for it.

    The builder is the only party that knows which certificate goes with its key, and a reader that
    cannot find one treats the result as a miss, so this is what makes a rotation take effect.
    Only after the certificate passed the builder's own trust: one that fails it would sit in the
    bucket for every reader to refuse.

    This is also the one moment a builder finds out whether it can write at all, so a refusal stops
    it: one that carried on would publish nothing, and nobody would hear about it until someone
    wondered why the bucket had gone quiet.
    """
    if bucket is None or bucket.writer is None or signer.certificate_pem is None:
        return
    # Unconditionally, rather than only when the key is absent. Renewing a certificate keeps the
    # key and so keeps the name, so a builder that skipped the write would go on publishing under a
    # certificate readers are about to stop accepting, with nothing to say why. One small object
    # once per start is not worth being clever about.
    try:
        bucket.writer.put(signing.certificate_key(signer.id), signer.certificate_pem.encode())
    except OSError as error:
        # A wrong endpoint, bucket or key reads as this. One line like every other configuration
        # fault here, because what reports it is a launcher quoting the tail of a log.
        sys.exit(f"cannot publish the certificate to {bucket.writer.describe()}: {error}")
    log.warning("published certificate for %s", signer.id.hex())


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=20555)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--read-url", help="base URL objects are read from, over plain HTTP")
    parser.add_argument("--s3-bucket", help="write objects to this S3 bucket")
    parser.add_argument("--s3-endpoint", default="", help="S3 endpoint host, without a scheme")
    parser.add_argument("--s3-key-file", type=Path, help="one line, `<key id> <secret>`")
    parser.add_argument("--s3-insecure", action="store_true", help="talk plain HTTP to S3")
    parser.add_argument("--store", type=Path, required=True, help="directory the local cache lives in")
    parser.add_argument(
        "--status",
        action="store_true",
        help="report on the shim already serving --store, rather than serving it",
    )
    # Small by default: a developer's buck-out already holds whatever this build materialized, so the
    # blob cache only pays where buck-out is thrown away, which is CI and a fresh worktree. It bounds
    # the blobs alone; the index that says which bundle a blob came from is the part that must not be
    # lost, and it is a few hundred bytes per action.
    parser.add_argument(
        "--store-size",
        type=int,
        default=1_000,
        metavar="MB",
        help="how much of it blobs may use before the least recently used are dropped",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=15,
        metavar="MINUTES",
        help="exit after this long with nothing asked of it, 0 to run until stopped",
    )
    parser.add_argument(
        "--index-rows",
        type=int,
        default=1_000_000,
        metavar="N",
        help="how many rows the index may hold before the oldest droppable ones are forgotten",
    )
    parser.add_argument("--signing-key", type=Path, help="private key to sign results with, PEM or OpenSSH")
    parser.add_argument(
        "--authority",
        action="append",
        default=[],
        metavar="FILE",
        type=Path,
        help="CA certificate whose leaves this will serve; repeatable",
    )
    parser.add_argument(
        "--signing-certificate",
        type=Path,
        help="this builder's own certificate, published for readers to find",
    )
    parser.add_argument(
        "--object-lifetime",
        type=float,
        default=0,
        metavar="DAYS",
        help="how long the bucket keeps an object; nothing is signed the certificate would not outlive",
    )
    parser.add_argument(
        "--unsigned",
        action="store_true",
        help="read a bucket nobody signs into, trusting whoever writes it; for testing",
    )
    return parser


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    if args.status:
        running = ask(args.store)
        if running is None:
            sys.exit(f"nothing is serving {args.store}")
        print(json.dumps(running, indent=2, sort_keys=True))
        return
    bucket = configured_bucket(args)
    try:
        store = Store(args.store, max_bytes=args.store_size * 1_000_000, max_rows=args.index_rows)
    except RuntimeError as error:
        # Someone else is already serving this directory, which is a thing to say plainly rather
        # than a traceback: sharing one shim is the intended arrangement, starting two is not.
        sys.exit(str(error))
    with closing(store):
        signer, verifier = configured_trust(args, bucket, store)
        serve(
            args.port,
            args.workers,
            store,
            bucket,
            signer,
            verifier,
            args.idle_timeout * 60,
        )


if __name__ == "__main__":
    main()
