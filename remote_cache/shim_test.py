"""The gRPC surface, against a shim in this process and a client built the same way.

Buck2 exercises these paths only incidentally, and only the ones its own build happens to take.
Here each one is asked for directly, including the ones it never takes and a server still has to
answer.

The client below encodes with the same code the server decodes with, so nothing here can catch a
wrong field number. `reapi_test.py` pins those against the reference implementation, and the demo
build pins them against Buck2 itself.
"""

import datetime
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast, override

import grpc
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import bucket
import bundle
import mock_bucket
import reapi
import seaweed
import shim
import signing
import store as store_module
import test_ca
import wire


def status_of(error: grpc.RpcError) -> grpc.StatusCode:
    """The status grpc failed a call with.

    What it raises implements `grpc.Call` too, and that is where `code()` lives, but the exception
    type it declares says nothing about it.
    """
    assert isinstance(error, grpc.Call)
    return cast(grpc.StatusCode, error.code())


def serialize(message: reapi.Wireable) -> bytes:
    return message.to_bytes()


class ShimCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.start_shim()

    def a_bucket(self) -> bucket.Bucket | None:
        """What the shim under test has behind it: nothing, unless a subclass says otherwise."""
        return None

    def a_store(self, max_bytes: int = 1_000_000_000) -> store_module.Store:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        keeping = store_module.Store(Path(directory.name), max_bytes=max_bytes)
        self.addCleanup(keeping.close)
        return keeping

    def start_shim(
        self,
        signer: signing.Signer | None = None,
        verifier: signing.Authority | None = None,
        store: store_module.Store | None = None,
    ) -> None:
        """A shim on a fresh store, and a channel to it. Called again for a second shim."""
        self.counts = shim.Counts()
        self.activity = shim.Activity()
        self.server, self.store, self.port = shim.build_server(
            0,
            store or self.a_store(),
            bucket=self.a_bucket(),
            signer=signer,
            verifier=verifier,
            activity=self.activity,
            counts=self.counts,
        )
        self.server.start()
        self.addCleanup(self.server.stop, 0)
        self.channel = grpc.insecure_channel(f"127.0.0.1:{self.port}")
        self.addCleanup(self.channel.close)
        # A call on a channel still connecting fails fast with UNAVAILABLE, which on a loaded
        # machine is what the first call of a test would otherwise see instead of its answer.
        grpc.channel_ready_future(self.channel).result(timeout=30)

    def unary(self, service: str, method: str, parse: Callable[[bytes], object]) -> Callable[..., object]:
        return self.channel.unary_unary(
            f"/{service}/{method}", request_serializer=serialize, response_deserializer=parse
        )

    # One callable per method, named the way the API names it.
    def get_capabilities(self) -> reapi.ServerCapabilities:
        call = self.unary(reapi.CAPABILITIES, "GetCapabilities", reapi.ServerCapabilities.parse)
        return cast(reapi.ServerCapabilities, call(reapi.Empty()))

    def get_action_result(self, request: reapi.GetActionResultRequest) -> reapi.ActionResult:
        call = self.unary(reapi.ACTION_CACHE, "GetActionResult", reapi.ActionResult.parse)
        return cast(reapi.ActionResult, call(request))

    def update_action_result(self, request: reapi.UpdateActionResultRequest) -> reapi.ActionResult:
        call = self.unary(reapi.ACTION_CACHE, "UpdateActionResult", reapi.ActionResult.parse)
        return cast(reapi.ActionResult, call(request))

    def find_missing(self, request: reapi.FindMissingBlobsRequest) -> reapi.FindMissingBlobsResponse:
        call = self.unary(reapi.CAS, "FindMissingBlobs", reapi.FindMissingBlobsResponse.parse)
        return cast(reapi.FindMissingBlobsResponse, call(request))

    def batch_update(self, request: reapi.BatchUpdateBlobsRequest) -> reapi.BatchUpdateBlobsResponse:
        call = self.unary(reapi.CAS, "BatchUpdateBlobs", reapi.BatchUpdateBlobsResponse.parse)
        return cast(reapi.BatchUpdateBlobsResponse, call(request))

    def batch_read(self, request: reapi.BatchReadBlobsRequest) -> reapi.BatchReadBlobsResponse:
        call = self.unary(reapi.CAS, "BatchReadBlobs", reapi.BatchReadBlobsResponse.parse)
        return cast(reapi.BatchReadBlobsResponse, call(request))

    def read(self, request: reapi.ReadRequest) -> bytes:
        call = self.channel.unary_stream(
            f"/{reapi.BYTESTREAM}/Read",
            request_serializer=serialize,
            response_deserializer=reapi.ReadResponse.parse,
        )
        return b"".join(response.data for response in call(request))

    def write(self, requests: Iterator[reapi.WriteRequest]) -> reapi.WriteResponse:
        call = self.channel.stream_unary(
            f"/{reapi.BYTESTREAM}/Write",
            request_serializer=serialize,
            response_deserializer=reapi.WriteResponse.parse,
        )
        return cast(reapi.WriteResponse, call(requests))

    # Two shapes every test needs.
    def upload(self, *bodies: bytes) -> list[reapi.Digest]:
        """Put blobs in with one BatchUpdateBlobs, the way Buck2 does for anything small."""
        digests = [reapi.Digest.for_bytes(body) for body in bodies]
        self.batch_update(
            reapi.BatchUpdateBlobsRequest(
                blobs=[
                    reapi.Blob(digest=digest, data=body)
                    for digest, body in zip(digests, bodies, strict=True)
                ]
            )
        )
        return digests

    def stream_up(self, data: bytes, chunk: int = 1 << 20, offset: int = 0) -> int:
        """Put one blob in over ByteStream, chunked, returning the size it committed."""
        digest = reapi.Digest.for_bytes(data)
        resource = f"uploads/a-client-uuid/blobs/{digest.hash}/{digest.size_bytes}"

        def requests() -> Iterator[reapi.WriteRequest]:
            for start in range(0, max(len(data), 1), chunk):
                piece = data[start : start + chunk]
                yield reapi.WriteRequest(
                    resource_name=resource,
                    write_offset=offset + start,
                    data=piece,
                    finish_write=start + len(piece) >= len(data),
                )

        return self.write(requests()).committed_size

    def resource(self, digest: reapi.Digest) -> str:
        return f"blobs/{digest.hash}/{digest.size_bytes}"


class TestCapabilities(ShimCase):
    def test_the_batch_limit_and_no_compressor_are_advertised(self) -> None:
        """Buck2 reads both, and an empty compressor list is what keeps it on the plain paths."""
        capabilities = self.get_capabilities()
        self.assertEqual(capabilities.max_batch_total_size_bytes, shim.MAX_BATCH_SIZE)
        self.assertTrue(capabilities.update_enabled)
        self.assertEqual(capabilities.digest_functions, (reapi.SHA256,))


class TestContentAddressableStorage(ShimCase):
    def test_a_batch_upload_reads_back(self) -> None:
        digests = self.upload(b"one", b"two")
        response = self.batch_read(reapi.BatchReadBlobsRequest(digests=digests))
        self.assertEqual([one.data for one in response.blobs], [b"one", b"two"])

    def test_reading_an_unknown_blob_says_not_found_per_blob(self) -> None:
        """A batch answers per digest, so one missing blob must not fail the whole call."""
        known = self.upload(b"here")[0]
        unknown = reapi.Digest.for_bytes(b"not uploaded")
        response = self.batch_read(reapi.BatchReadBlobsRequest(digests=[known, unknown]))
        self.assertEqual(
            [one.status.code for one in response.blobs if one.status],
            [reapi.OK, reapi.NOT_FOUND],
        )

    def test_find_missing_reports_only_what_is_missing(self) -> None:
        present = self.upload(b"present")[0]
        absent = reapi.Digest.for_bytes(b"absent")
        response = self.find_missing(reapi.FindMissingBlobsRequest(blob_digests=[present, absent]))
        self.assertEqual(response.missing_blob_digests, [absent])

    def test_the_empty_blob_is_always_there(self) -> None:
        """Clients take it for granted rather than uploading it, so it cannot be missing."""
        response = self.find_missing(
            reapi.FindMissingBlobsRequest(blob_digests=[reapi.Digest.for_bytes(b"")])
        )
        self.assertEqual(response.missing_blob_digests, [])

    def test_a_hash_that_is_not_a_hash_never_becomes_a_path(self) -> None:
        """The client is trusted, but `../` in a digest reading a file outside the store is not a
        thing a trusted client should be able to do by accident either."""
        outside = reapi.Digest(hash="../../lock", size_bytes=1)
        response = self.batch_read(reapi.BatchReadBlobsRequest(digests=[outside]))
        self.assertEqual([one.status.code for one in response.blobs if one.status], [reapi.NOT_FOUND])
        missing = self.find_missing(reapi.FindMissingBlobsRequest(blob_digests=[outside]))
        self.assertEqual(missing.missing_blob_digests, [outside])
        with self.assertRaises(grpc.RpcError) as caught:
            self.get_action_result(reapi.GetActionResultRequest(action_digest=outside))
        self.assertEqual(status_of(caught.exception), grpc.StatusCode.INVALID_ARGUMENT)
        with self.assertRaises(grpc.RpcError) as caught:
            self.read(reapi.ReadRequest(resource_name="blobs/../../lock/1"))
        self.assertEqual(status_of(caught.exception), grpc.StatusCode.INVALID_ARGUMENT)

    def test_bytes_that_do_not_match_their_digest_are_refused(self) -> None:
        response = self.batch_update(
            reapi.BatchUpdateBlobsRequest(
                blobs=[reapi.Blob(digest=reapi.Digest.for_bytes(b"honest"), data=b"a lie")]
            )
        )
        status = response.blobs[0].status
        assert status is not None
        self.assertEqual(status.code, reapi.INVALID_ARGUMENT)
        self.assertEqual(self.store.counts()[0], 1)  # only the empty blob


class TestByteStream(ShimCase):
    def test_a_chunked_upload_reads_back_whole(self) -> None:
        data = bytes(range(256)) * 8192  # 2 MiB, so it takes more than one chunk
        self.assertEqual(self.stream_up(data), len(data))
        resource = self.resource(reapi.Digest.for_bytes(data))
        self.assertEqual(self.read(reapi.ReadRequest(resource_name=resource)), data)

    def test_a_read_honours_offset_and_limit(self) -> None:
        data = b"abcdefghij"
        self.stream_up(data)
        resource = self.resource(reapi.Digest.for_bytes(data))
        self.assertEqual(self.read(reapi.ReadRequest(resource_name=resource, read_offset=4)), b"efghij")
        self.assertEqual(
            self.read(reapi.ReadRequest(resource_name=resource, read_offset=2, read_limit=3)),
            b"cde",
        )

    def test_reading_an_unknown_blob_is_not_found(self) -> None:
        resource = self.resource(reapi.Digest.for_bytes(b"never uploaded"))
        with self.assertRaises(grpc.RpcError) as caught:
            self.read(reapi.ReadRequest(resource_name=resource))
        self.assertEqual(status_of(caught.exception), grpc.StatusCode.NOT_FOUND)

    def test_bytes_that_do_not_match_their_digest_are_refused(self) -> None:
        digest = reapi.Digest.for_bytes(b"honest")
        resource = f"uploads/x/blobs/{digest.hash}/{digest.size_bytes}"
        with self.assertRaises(grpc.RpcError) as caught:
            self.write(iter([reapi.WriteRequest(resource_name=resource, data=b"a lie", finish_write=True)]))
        self.assertEqual(status_of(caught.exception), grpc.StatusCode.INVALID_ARGUMENT)

    def test_a_resumed_upload_is_refused_rather_than_mishandled(self) -> None:
        """Buck2 always writes from zero. Anything else is a path we have never served."""
        with self.assertRaises(grpc.RpcError) as caught:
            self.stream_up(b"resuming", offset=4)
        self.assertEqual(status_of(caught.exception), grpc.StatusCode.UNIMPLEMENTED)

    def test_a_nonsense_resource_name_is_refused(self) -> None:
        with self.assertRaises(grpc.RpcError) as caught:
            self.read(reapi.ReadRequest(resource_name="compressed-blobs/zstd/x/1"))
        self.assertEqual(status_of(caught.exception), grpc.StatusCode.INVALID_ARGUMENT)


class TestActionCache(ShimCase):
    def result_with_a_tree(self) -> tuple[reapi.Digest, reapi.ActionResult]:
        """A result shaped like a real one: a file output, and a directory naming a Tree.

        Built out of wire primitives rather than our own message writers, since the shim has no
        reason to write an ActionResult and a test should not invent one for it.
        """
        (file_digest,) = self.upload(b"an output file")
        node = wire.text(1, "inner") + wire.submessage(2, file_digest.to_bytes())
        (tree_digest,) = self.upload(wire.submessage(1, wire.submessage(1, node)))
        raw = wire.submessage(
            2, wire.text(1, "out") + wire.submessage(2, file_digest.to_bytes())
        ) + wire.submessage(3, wire.text(1, "dir") + wire.submessage(3, tree_digest.to_bytes()))
        result = reapi.ActionResult.parse(raw)
        action = reapi.Digest.for_bytes(b"an action")
        self.update_action_result(
            reapi.UpdateActionResultRequest(action_digest=action, action_result=result)
        )
        return action, result

    def test_an_unknown_action_is_not_found(self) -> None:
        with self.assertRaises(grpc.RpcError) as caught:
            self.get_action_result(
                reapi.GetActionResultRequest(action_digest=reapi.Digest.for_bytes(b"unknown"))
            )
        self.assertEqual(status_of(caught.exception), grpc.StatusCode.NOT_FOUND)

    def test_a_stored_result_reads_back(self) -> None:
        action, result = self.result_with_a_tree()
        served = self.get_action_result(reapi.GetActionResultRequest(action_digest=action))
        self.assertEqual(served, result)

    def test_a_result_whose_blob_is_gone_is_a_miss(self) -> None:
        """Including a blob named inside the Tree, which is only reachable by parsing it."""
        action, result = self.result_with_a_tree()
        self.store.drop_blob(result.file_digests[0])
        with self.assertRaises(grpc.RpcError) as caught:
            self.get_action_result(reapi.GetActionResultRequest(action_digest=action))
        self.assertEqual(status_of(caught.exception), grpc.StatusCode.NOT_FOUND)

    def test_a_result_whose_tree_is_gone_is_a_miss(self) -> None:
        action, result = self.result_with_a_tree()
        self.store.drop_blob(result.tree_digests[0])
        with self.assertRaises(grpc.RpcError) as caught:
            self.get_action_result(reapi.GetActionResultRequest(action_digest=action))
        self.assertEqual(status_of(caught.exception), grpc.StatusCode.NOT_FOUND)

    def test_a_blob_rewritten_on_disk_is_a_miss_and_is_dropped(self) -> None:
        """A hit is claimed only for bytes that still hash to their names, whatever the files say."""
        action, result = self.result_with_a_tree()
        digest = result.file_digests[0]
        (self.store.root / "blobs" / digest.hash[:2] / digest.hash).write_bytes(b"x" * digest.size_bytes)
        with self.assertRaises(grpc.RpcError) as caught:
            self.get_action_result(reapi.GetActionResultRequest(action_digest=action))
        self.assertEqual(status_of(caught.exception), grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.store.has_blobs([digest]), [False])
        self.assertEqual(self.counts.report()["incomplete"], 1)


def one_file_result(digest: reapi.Digest) -> reapi.ActionResult:
    """A result with one output file, built from wire primitives: the shim never writes one."""
    return reapi.ActionResult.parse(
        wire.submessage(2, wire.text(1, "out") + wire.submessage(2, digest.to_bytes()))
    )


class BucketCase(ShimCase):
    """A shim with a test bucket behind it, which counts every request."""

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.served = mock_bucket.Served(Path(directory.name))
        self.addCleanup(self.served.close)
        super().setUp()

    @override
    def a_bucket(self) -> bucket.Bucket:
        return bucket.Bucket(bucket.Reader(self.served.url), self.served)

    def publish(self, action: bytes, body: bytes) -> reapi.Digest:
        """One result with one output file, through this shim, which publishes it."""
        (digest,) = self.upload(body)
        named = reapi.Digest.for_bytes(action)
        self.update_action_result(
            reapi.UpdateActionResultRequest(action_digest=named, action_result=one_file_result(digest))
        )
        return named

    def lookup(self, action: reapi.Digest) -> grpc.StatusCode | None:
        """The status a lookup failed with, or None for a hit."""
        try:
            self.get_action_result(reapi.GetActionResultRequest(action_digest=action))
        except grpc.RpcError as error:
            return status_of(error)
        return None


class TestBucketFetch(BucketCase):
    """What a reader does with the bucket, one request at a time."""

    def test_a_second_pointer_to_a_bundle_already_here_costs_one_request(self) -> None:
        """The same outputs under another action digest are the common case, and the bundle may be big."""
        first = self.publish(b"one action", b"the same output")
        second = self.publish(b"another", b"the same output")
        (bundle_name,) = self.served.keys("bundle")
        self.start_shim()
        self.assertIsNone(self.lookup(first))
        self.assertEqual(self.served.counts[f"bundle/{bundle_name}"], 1)
        self.assertIsNone(self.lookup(second))
        self.assertEqual(self.served.counts[f"bundle/{bundle_name}"], 1)
        self.assertEqual(self.served.counts[f"ac/{second.hash}"], 1)

    def test_what_happened_is_counted_by_kind(self) -> None:
        """Buck2 sees one kind of miss; the status report has to tell a refusal from an absence."""
        action = self.publish(b"one action", b"the output")
        self.start_shim()
        self.assertIsNone(self.lookup(action))
        self.assertEqual(self.lookup(reapi.Digest.for_bytes(b"never")), grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.counts.report(), {"hits": 1, "lookups": 2, "misses": 1})

    def test_a_bucket_that_answers_badly_is_a_miss_and_is_asked_again(self) -> None:
        """A 503 is never "not cached": counted as the bucket's fault, and the next lookup tries."""
        action = self.publish(b"one action", b"the output")
        self.start_shim()
        self.served.broken.add(f"ac/{action.hash}")
        self.assertEqual(self.lookup(action), grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.counts.report()["bucket errors"], 1)
        self.served.broken.clear()
        self.assertIsNone(self.lookup(action))

    def test_a_bundle_too_large_for_this_store_is_a_miss(self) -> None:
        """A builder with a bigger store publishes bundles a smaller reader cannot take.

        Nothing is wrong with the object, so this is a miss and not a refusal: refusing would have
        the next publisher send it again, just as large.
        """
        action = self.publish(b"one action", b"the output")
        self.start_shim(store=self.a_store(max_bytes=8))
        self.assertEqual(self.lookup(action), grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.counts.report()["bundles too large"], 1)

    def test_a_bundle_that_failed_to_verify_is_uploaded_again(self) -> None:
        """Otherwise a rewritten bundle is a miss for its whole lifetime, re-pointed at by every rebuild."""
        action = self.publish(b"one action", b"the output")
        (bundle_name,) = self.served.keys("bundle")
        path = self.served.root / "bundle" / bundle_name
        path.write_bytes(b"not a bundle")
        self.start_shim()
        self.assertEqual(self.lookup(action), grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.counts.report()["bundles refused"], 1)
        self.publish(b"another action", b"the output")
        expected = {reapi.Digest.for_bytes(b"the output").hash: b"the output"}
        self.assertEqual(dict(bundle.unpack(path.read_bytes())), expected)

    def test_an_evicted_blob_comes_back_from_its_bundle_by_digest_alone(self) -> None:
        """Buck2 can ask for a blob with no result lookup in front, which is what provenance is for."""
        action = self.publish(b"one action", b"the output")
        digest = reapi.Digest.for_bytes(b"the output")
        self.start_shim()
        self.assertIsNone(self.lookup(action))
        self.store.drop_blob(digest)
        self.assertEqual(self.read(reapi.ReadRequest(resource_name=self.resource(digest))), b"the output")
        self.store.drop_blob(digest)
        response = self.batch_read(reapi.BatchReadBlobsRequest(digests=[digest]))
        self.assertEqual([one.data for one in response.blobs], [b"the output"])
        self.assertEqual(self.counts.report()["bundles refilled"], 2)

    def test_a_refill_from_a_rewritten_bundle_is_a_refusal(self) -> None:
        action = self.publish(b"one action", b"the output")
        digest = reapi.Digest.for_bytes(b"the output")
        (bundle_name,) = self.served.keys("bundle")
        self.start_shim()
        self.assertIsNone(self.lookup(action))
        (self.served.root / "bundle" / bundle_name).write_bytes(b"not a bundle")
        self.store.drop_blob(digest)
        response = self.batch_read(reapi.BatchReadBlobsRequest(digests=[digest]))
        self.assertEqual([one.status.code for one in response.blobs if one.status], [reapi.NOT_FOUND])
        self.assertEqual(self.counts.report()["bundles refused"], 1)

    def test_a_refill_takes_only_what_the_store_attributes_to_that_bundle(self) -> None:
        """No result to prove the bundle against, so a rewritten one may not smuggle members in.

        The operator packs junk and another bundle's blob into bundle A. Neither may enter the store
        from A: the junk would sit there until eviction, and the other blob's provenance would now
        name A, which is a persistent miss once A is restored and the blob evicted.
        """
        first = self.publish(b"one action", b"first output")
        second = self.publish(b"another", b"second output")
        one, two = reapi.Digest.for_bytes(b"first output"), reapi.Digest.for_bytes(b"second output")
        self.start_shim()
        self.assertIsNone(self.lookup(first))
        self.assertIsNone(self.lookup(second))
        assert (name := self.store.bundle_of(one)) is not None
        junk = reapi.Digest.for_bytes(b"junk")
        rewritten = {one.hash: b"first output", junk.hash: b"junk", two.hash: b"second output"}
        (self.served.root / "bundle" / name).write_bytes(bundle.pack(rewritten))
        self.store.drop_blob(one)
        response = self.batch_read(reapi.BatchReadBlobsRequest(digests=[one]))
        self.assertEqual([blob.data for blob in response.blobs], [b"first output"])
        self.assertEqual(self.store.has_blobs([junk]), [False])
        self.assertNotEqual(self.store.bundle_of(two), name)

    def test_a_result_served_without_its_bundle_still_knows_where_its_blobs_live(self) -> None:
        """The shortcut path names no bundle for blobs this machine built itself, so record one."""
        action = self.publish(b"one action", b"the output")
        digest = reapi.Digest.for_bytes(b"the output")
        self.start_shim()
        self.upload(b"the output")
        self.assertIsNone(self.lookup(action))
        self.store.drop_blob(digest)
        response = self.batch_read(reapi.BatchReadBlobsRequest(digests=[digest]))
        self.assertEqual([blob.data for blob in response.blobs], [b"the output"])

    def test_find_missing_never_asks_the_bucket(self) -> None:
        """A blob this shim could fetch back is still reported missing: that question is local."""
        action = self.publish(b"one action", b"the output")
        digest = reapi.Digest.for_bytes(b"the output")
        self.start_shim()
        self.assertIsNone(self.lookup(action))
        self.store.drop_blob(digest)
        asked = sum(self.served.counts.values())
        response = self.find_missing(reapi.FindMissingBlobsRequest(blob_digests=[digest]))
        self.assertEqual(response.missing_blob_digests, [digest])
        self.assertEqual(sum(self.served.counts.values()), asked)


class TestProven(unittest.TestCase):
    """The bundle is shown to be exactly what the signed result names before anything reaches the store."""

    def test_every_way_a_bundle_can_disagree_with_its_pointer(self) -> None:
        file = reapi.Digest.for_bytes(b"a file")
        # A Tree whose root directory lists that one file, which is how a directory output arrives.
        tree_bytes = wire.submessage(
            1, wire.submessage(1, wire.text(1, "inner") + wire.submessage(2, file.to_bytes()))
        )
        tree = reapi.Digest.for_bytes(tree_bytes)
        result = reapi.ActionResult.parse(
            wire.submessage(2, wire.text(1, "out") + wire.submessage(2, file.to_bytes()))
            + wire.submessage(3, wire.text(1, "dir") + wire.submessage(3, tree.to_bytes()))
        )
        members = {file.hash: b"a file", tree.hash: tree_bytes}
        right = bundle.bundle_name((file, tree))
        pointer = shim.Pointer(bundle=right, result=result.raw)
        # The file is named twice, as an output and inside the tree; the set is what the name covers.
        self.assertEqual(
            {one.hash for one in shim.ActionCache._proven(pointer, result, members)}, set(members)
        )
        extra = reapi.Digest.for_bytes(b"not named")
        misnamed = shim.Pointer(bundle="f" * 64, result=result.raw)
        cases = (
            ("an extra member", pointer, members | {extra.hash: b"not named"}, "holds"),
            ("a missing tree", pointer, {file.hash: b"a file"}, "does not resolve"),
            ("the wrong name", misnamed, members, "make"),
        )
        for what, one, held, why in cases:
            with self.subTest(what=what), self.assertRaisesRegex(ValueError, why):
                shim.ActionCache._proven(one, result, held)


class TestSignedFetch(BucketCase):
    """A reader holding only the authority, against a bucket an operator can rewrite.

    And against a store that every build on the machine can write: what the store holds is served
    under the same rule as what the bucket holds.
    """

    @override
    def setUp(self) -> None:
        super().setUp()
        self.ca_key, self.ca = test_ca.authority("shim CA")
        self.leaf_key = Ed25519PrivateKey.generate()
        key_file = test_ca.write_key(self.served.root / "leaf", self.leaf_key)
        certificate = self.served.root / "leaf.crt"
        certificate.write_text(self.issued(self.leaf_key))
        self.signer = signing.Signer(key_file, certificate)
        # As `configured_trust` sets a builder up: its certificate admitted into its own store.
        self.builder_store = self.a_store()
        builder_trust = self.trust(self.builder_store)
        builder_trust.admit(self.signer)
        self.start_shim(signer=self.signer, verifier=builder_trust, store=self.builder_store)
        shim.publish_certificate(self.a_bucket(), self.signer)
        self.action = self.publish(b"one action", b"the output")

    def issued(self, key: test_ca.Signing, valid_from: datetime.timedelta = -test_ca.DAY) -> str:
        """A leaf under the shim CA, valid for a year from `valid_from`."""
        valid_to = valid_from + 365 * test_ca.DAY
        return test_ca.issue(
            "builder", key, test_ca.name("shim CA"), self.ca_key, valid_from=valid_from, valid_to=valid_to
        )

    def trust(
        self,
        remembered: signing.Certificates | None = None,
        fetch: Callable[[str], bytes | None] | None = None,
    ) -> signing.Authority:
        return signing.Authority([self.ca], fetch or self.a_bucket().reader.get, remembered)

    def rewrite_pointers(self, rewrite: Callable[[str, bytes], bytes]) -> None:
        """Every pointer replaced by what the operator makes of its verified payload."""
        for name in self.served.keys("ac"):
            path = self.served.root / "ac" / name
            path.write_bytes(rewrite(name, self.trust().unwrap(name, path.read_bytes())))

    def refused(self) -> None:
        self.start_shim(verifier=self.trust())
        self.assertEqual(self.lookup(self.action), grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.counts.report()["pointers refused"], 1)

    def test_a_reader_holding_only_the_authority_gets_the_result(self) -> None:
        self.start_shim(verifier=self.trust())
        self.assertIsNone(self.lookup(self.action))

    def test_a_reader_refuses_what_buck_uploads(self) -> None:
        """It could never serve them back, so storing them would only crowd out what it can."""
        self.start_shim(verifier=self.trust())
        self.assertFalse(self.get_capabilities().update_enabled)
        digest = reapi.Digest.for_bytes(b"built here")
        for attempt in (
            lambda: self.upload(b"built here"),
            lambda: self.stream_up(b"built here"),
            lambda: self.update_action_result(
                reapi.UpdateActionResultRequest(action_digest=digest, action_result=one_file_result(digest))
            ),
        ):
            with self.assertRaises(grpc.RpcError) as caught:
                attempt()
            self.assertEqual(status_of(caught.exception), grpc.StatusCode.PERMISSION_DENIED)
        self.assertEqual(self.counts.report()["uploads refused"], 3)
        self.assertEqual(self.store.counts(), (1, 0))  # the empty blob and nothing else

    def test_a_pointer_in_the_store_is_verified_like_one_from_the_bucket(self) -> None:
        """A row written by anything but a verified fetch is refused, dropped and looked up afresh."""
        self.start_shim(verifier=self.trust())
        self.assertIsNone(self.lookup(self.action))
        forged = reapi.Digest.for_bytes(b"what an attacker built")
        (bundle_name,) = self.served.keys("bundle")
        pointer = shim.Pointer(bundle=bundle_name, result=one_file_result(forged).raw)
        self.store.put_result(self.action, shim.ac_pack(pointer))
        self.assertIsNone(self.lookup(self.action))
        self.assertEqual(self.counts.report()["pointers refused"], 1)
        self.assertEqual(self.served.counts[f"ac/{self.action.hash}"], 2)
        served = self.get_action_result(reapi.GetActionResultRequest(action_digest=self.action))
        self.assertEqual(served.file_digests, [reapi.Digest.for_bytes(b"the output")])

    def test_a_builders_own_results_verify_after_a_restart_with_the_bucket_gone(self) -> None:
        """The store keeps the certificates it took, so what it holds needs no bucket to be served."""
        asked = sum(self.served.counts.values())
        self.start_shim(
            verifier=self.trust(self.builder_store, fetch=lambda _: None), store=self.builder_store
        )
        self.assertIsNone(self.lookup(self.action))
        self.assertEqual(sum(self.served.counts.values()), asked)

    def test_a_leaf_that_runs_out_expires_what_the_store_holds(self) -> None:
        """A local entry lives exactly as long as a bucket entry would: while its leaf is valid."""
        trust = self.trust()
        self.start_shim(verifier=trust)
        self.assertIsNone(self.lookup(self.action))
        trust.clock = lambda: datetime.datetime.now(datetime.UTC) + 400 * test_ca.DAY
        self.assertEqual(self.lookup(self.action), grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.counts.report()["pointers refused"], 2)  # the store's copy, then the bucket's

    def test_a_bucket_that_cannot_say_whether_a_kept_pointer_is_current_is_a_miss(self) -> None:
        """Verifying what the store holds can need the bucket, and that read fails like any other.

        The leaf is fetched again once the remembered one runs out. A bucket that will not answer
        leaves this unknown rather than refused, so the pointer stays and the build rebuilds.
        """
        trust = self.trust()
        self.start_shim(verifier=trust)
        self.assertIsNone(self.lookup(self.action))
        trust.clock = lambda: datetime.datetime.now(datetime.UTC) + 400 * test_ca.DAY
        self.served.broken.add(signing.certificate_key(self.signer.id))
        self.assertEqual(self.lookup(self.action), grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.counts.report()["bucket errors"], 2)  # the store's copy, then the bucket's
        self.assertIsNotNone(self.store.result(self.action))

    def test_a_result_signed_by_a_stranger_is_refused(self) -> None:
        """Bucket write access alone has to be worth nothing, own CA and own leaf included."""
        stranger_ca_key, _ = test_ca.authority("shim CA")
        stranger_key = Ed25519PrivateKey.generate()
        forger = signing.Signer(test_ca.write_key(self.served.root / "stranger", stranger_key))
        forged = test_ca.issue("builder", stranger_key, test_ca.name("shim CA"), stranger_ca_key)
        self.served.put(signing.certificate_key(forger.id), forged.encode())
        self.rewrite_pointers(forger.wrap)
        self.refused()

    def test_a_result_moved_to_another_action_is_refused(self) -> None:
        self.rewrite_pointers(lambda _, payload: self.signer.wrap("b" * 64, payload))
        self.refused()

    def test_a_result_stripped_of_its_signature_is_refused(self) -> None:
        self.rewrite_pointers(lambda _, payload: payload)
        self.refused()

    def test_a_result_under_an_expired_leaf_is_refused(self) -> None:
        expired = self.issued(self.leaf_key, valid_from=-400 * test_ca.DAY)
        self.served.put(signing.certificate_key(self.signer.id), expired.encode())
        self.refused()

    def test_a_signed_bucket_read_without_keys_is_refused(self) -> None:
        """Forgetting the keys must not quietly turn signing off."""
        self.start_shim()
        self.assertEqual(self.lookup(self.action), grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.counts.report()["pointers refused"], 1)


class TestConfiguredTrust(unittest.TestCase):
    """The command line's rules about keys, which is where the design's trust rule is enforced.

    Against SeaweedFS, because the only writer the command line knows is the S3 one.
    """

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.weed = seaweed.Seaweed(Path(os.environ["WEED"]), Path(cls.directory.name), "tine-cache")
        cls.addClassCleanup(cls.weed.close)
        cls.key_file = Path(cls.directory.name) / "s3.key"
        cls.key_file.write_text("unchecked unchecked\n")
        cls.writing = (
            *("--read-url", cls.weed.read_url),
            *("--s3-bucket", cls.weed.bucket),
            *("--s3-endpoint", cls.weed.s3_endpoint),
            *("--s3-key-file", str(cls.key_file)),
            "--s3-insecure",
        )

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ca_key, ca = test_ca.authority("cli CA")
        self.ca = self.root / "ca.crt"
        self.ca.write_text(ca)
        leaf_key = Ed25519PrivateKey.generate()
        self.leaf = test_ca.write_key(self.root / "leaf", leaf_key)
        self.certificate = self.root / "leaf.crt"
        self.certificate.write_text(test_ca.issue("builder", leaf_key, test_ca.name("cli CA"), self.ca_key))
        self.stranger = self.root / "stranger.crt"
        self.stranger.write_text(
            test_ca.issue("builder", leaf_key, test_ca.name("cli CA"), test_ca.authority()[0])
        )
        self.published = signing.certificate_key(signing.key_id(leaf_key.public_key()))

    def configured(self, *arguments: str) -> tuple[signing.Signer | None, signing.Authority | None]:
        store = store_module.Store(Path(tempfile.mkdtemp(dir=self.root)), max_bytes=1)
        self.addCleanup(store.close)
        args = shim.parser().parse_args([*arguments, "--store", str(store.root)])
        self.bucket = shim.configured_bucket(args)
        return shim.configured_trust(args, self.bucket, store)

    def in_bucket(self) -> str | None:
        """The certificate the bucket holds for this test's leaf key, if any."""
        assert self.bucket is not None
        pem = self.bucket.reader.get(self.published)
        return pem.decode() if pem is not None else None

    def test_a_bucket_needs_trust_or_an_explicit_unsigned(self) -> None:
        with self.assertRaisesRegex(SystemExit, "--unsigned"):
            self.configured(*self.writing)
        _, trust = self.configured(*self.writing, "--unsigned")
        self.assertIsNone(trust)
        _, trust = self.configured(*self.writing, "--authority", str(self.ca))
        self.assertIsInstance(trust, signing.Authority)

    def test_an_authority_needs_a_bucket_to_fetch_leaves_from(self) -> None:
        with self.assertRaisesRegex(SystemExit, "needs a bucket"):
            self.configured("--authority", str(self.ca))

    def test_a_signer_needs_someone_to_read_it_back(self) -> None:
        with self.assertRaisesRegex(SystemExit, "who may read"):
            self.configured("--signing-key", str(self.leaf))

    def test_a_certificate_is_judged_before_it_is_published(self) -> None:
        """A bad one must never reach the bucket, where every reader would go on refusing it."""
        signing_as = (*self.writing, "--authority", str(self.ca), "--signing-key", str(self.leaf))
        with self.assertRaisesRegex(SystemExit, "candidates exhausted"):
            self.configured(*signing_as, "--signing-certificate", str(self.stranger))
        self.assertIsNone(self.in_bucket())
        self.configured(*signing_as, "--signing-certificate", str(self.certificate))
        self.assertEqual(self.in_bucket(), self.certificate.read_text())
        # Published again on every start, since a renewal keeps the key and so the name.
        assert self.bucket is not None and self.bucket.writer is not None
        self.bucket.writer.put(self.published, b"stale")
        self.configured(*signing_as, "--signing-certificate", str(self.certificate))
        self.assertEqual(self.in_bucket(), self.certificate.read_text())

    def test_a_builder_that_cannot_write_says_so_and_stops(self) -> None:
        """Carrying on would publish nothing, and the bucket going quiet is nobody's alarm."""
        nowhere = (
            *("--read-url", self.weed.read_url),
            *("--s3-bucket", self.weed.bucket),
            # Nothing listens here, which is what a wrong endpoint or a revoked key reads as.
            *("--s3-endpoint", "127.0.0.1:1"),
            *("--s3-key-file", str(self.key_file)),
            "--s3-insecure",
            *("--authority", str(self.ca)),
            *("--signing-key", str(self.leaf)),
            *("--signing-certificate", str(self.certificate)),
        )
        with self.assertRaisesRegex(SystemExit, "cannot publish the certificate to"):
            self.configured(*nowhere)

    def test_a_missing_key_file_is_one_line_not_a_traceback(self) -> None:
        with self.assertRaisesRegex(SystemExit, "No such file"):
            self.configured(*self.writing, "--authority", str(self.root / "nowhere"))


class TestPointerFraming(unittest.TestCase):
    def test_a_round_trip_and_an_empty_result(self) -> None:
        """Buck2 publishes an empty result to probe whether it may write at all, so it must frame too."""
        for result in (b"a result", b""):
            one = shim.Pointer(bundle="e" * 64, result=result)
            with self.subTest(result=result):
                self.assertEqual(shim.ac_unpack(shim.ac_pack(one)), one)

    def test_a_stored_pointer_not_naming_a_bundle_is_refused(self) -> None:
        """Never a default: a field that can be skipped by omission is not one a reader can rely on."""
        for stored in (b"", wire.text(shim.BUNDLE_FIELD, "z" * 64)):
            with self.subTest(stored=stored[:12]), self.assertRaisesRegex(ValueError, "does not name"):
                shim.ac_unpack(stored)

    def test_a_field_this_does_not_know_passes_through(self) -> None:
        """The reason for the framing: whatever is added next must not break a reader."""
        one = shim.Pointer(bundle="e" * 64, result=b"a result")
        self.assertEqual(shim.ac_unpack(shim.ac_pack(one) + wire.text(9, "from the future")), one)

    def test_a_resource_name_is_read_for_its_digest(self) -> None:
        digest = reapi.Digest(hash="a" * 64, size_bytes=5)
        self.assertEqual(shim.resource_digest(f"uploads/uuid/blobs/{'a' * 64}/5"), digest)
        self.assertEqual(shim.resource_digest(f"blobs/{'a' * 64}/5"), digest)
        for resource, why in (("compressed-blobs/zstd/x/1", "not a plain"), ("blobs/aa", "truncated")):
            with self.subTest(resource=resource), self.assertRaisesRegex(ValueError, why):
                shim.resource_digest(resource)


class TestIdleTimeout(ShimCase):
    """What decides a shim has outlived the builds that wanted it."""

    def test_it_gives_up_after_its_timeout(self) -> None:
        self.assertEqual(shim.wait(self.server, self.activity, 0.05), "idle")

    def test_a_call_puts_the_deadline_back(self) -> None:
        """Otherwise a long build with a quiet patch in it would lose its shim mid-way."""

        def busy() -> None:
            # Well inside the timeout each time, so a scheduler stall cannot turn this into "idle".
            for _ in range(4):
                time.sleep(0.02)
                self.activity.last = time.monotonic()
            self.server.stop(0)

        keeping = threading.Thread(target=busy)
        keeping.start()
        self.addCleanup(keeping.join)
        self.assertEqual(shim.wait(self.server, self.activity, 0.5), "stopped")

    def test_a_timeout_of_zero_means_until_stopped(self) -> None:
        threading.Timer(0.1, lambda: self.server.stop(0)).start()
        self.assertEqual(shim.wait(self.server, self.activity, 0), "stopped")

    def test_a_running_build_holds_it_open_past_the_timeout(self) -> None:
        """One miss and then an hour-long rpm build is ordinary, and the lookup after it must land."""
        self.activity.attend(os.getpid())
        threading.Timer(0.2, lambda: self.server.stop(0)).start()
        self.assertEqual(shim.wait(self.server, self.activity, 0.05), "stopped")

    def test_a_build_that_has_finished_holds_nothing_open(self) -> None:
        finished = subprocess.Popen([sys.executable, "-c", ""])
        finished.wait()
        self.activity.attend(finished.pid)
        self.assertEqual(shim.wait(self.server, self.activity, 0.05), "idle")

    def test_a_request_is_what_counts_as_activity(self) -> None:
        """Through the interceptor, so every method counts and none has to remember to."""
        before = self.activity.last
        self.get_capabilities()
        self.assertGreater(self.activity.last, before)


class SlowWriter:
    """A writer that takes its time, so a second publisher of the same bundle overlaps the first."""

    def __init__(self, delay: float = 0.2) -> None:
        self.held: dict[str, bytes] = {}
        self.puts: list[str] = []
        self.delay = delay
        self.guard = threading.Lock()

    def put(self, key: str, data: bytes) -> None:
        if key.startswith("bundle/"):
            time.sleep(self.delay)
        with self.guard:
            self.puts.append(key)
            self.held[key] = data

    def refresh(self, key: str) -> bool:
        with self.guard:
            return key in self.held

    def describe(self) -> str:
        return "slow"


class TestPublishRace(ShimCase):
    """Two results finishing together share a bundle often enough to matter."""

    @override
    def a_bucket(self) -> bucket.Bucket:
        self.writer = SlowWriter()
        return bucket.Bucket(bucket.Reader("http://127.0.0.1:1"), self.writer)

    def publish_together(self, body: bytes) -> None:
        def publish(action_hash: str) -> None:
            (digest,) = self.upload(body)
            self.update_action_result(
                reapi.UpdateActionResultRequest(
                    action_digest=reapi.Digest(hash=action_hash, size_bytes=1),
                    action_result=one_file_result(digest),
                )
            )

        threads = [threading.Thread(target=publish, args=(f"{index:064x}",)) for index in range(4)]
        for one in threads:
            one.start()
        for one in threads:
            one.join()

    def test_one_bundle_is_sent_once_however_many_results_name_it(self) -> None:
        """Same outputs, different action digests: the common case when a config changes."""
        self.publish_together(b"the same output from two actions")
        bundles = [key for key in self.writer.puts if key.startswith("bundle/")]
        pointers = [key for key in self.writer.puts if key.startswith("ac/")]
        self.assertEqual(len(bundles), 1, f"the same bundle was sent {len(bundles)} times")
        self.assertEqual(len(pointers), 4)

    def test_the_bundle_is_there_before_any_pointer_naming_it(self) -> None:
        """The one ordering that can be observed as a broken hit.

        `puts` records completions, not attempts, so a pointer ahead of its bundle shows up here.
        """
        self.publish_together(b"shared")
        self.assertTrue(self.writer.puts[0].startswith("bundle/"), self.writer.puts)
