# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The wire contract, against bytes produced by the reference protobuf implementation.

Every hex string below came from `protobuf` serialising the same message, captured with generated code
(not in the tree any more).
"""

import unittest
from collections.abc import Callable

import reapi

REFERENCE = {
    "digest": "0a4061616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161100b",
    "get_action_result_request": "0a046d61696e12440a4061616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161100b1801",
    "action_result": "12510a076f75742f6f6e6512440a4061616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161100b2001124f0a076f75742f74776f12440a406262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626210021a4f0a076f75742f6469721a440a4063636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363102132440a40626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262621002",
    "update_action_result_request": "0a046d61696e12440a4061616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161100b1abb0212510a076f75742f6f6e6512440a4061616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161100b2001124f0a076f75742f74776f12440a406262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626210021a4f0a076f75742f6469721a440a4063636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363102132440a40626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262621002",
    "tree": "0a510a4f0a05696e6e657212440a4061616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161100b200112500a4e0a0664656570657212440a40626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262621002",
    "find_missing_request": "0a046d61696e12440a4061616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161100b12440a40626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262621002",
    "find_missing_response": "12440a40626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262621002",
    "batch_update_request": "0a046d61696e12530a440a4061616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161100b120b68656c6c6f20776f726c64124a0a440a4062626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262100212026869",
    "batch_update_response": "0a480a440a4061616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161100b12000a510a440a4062626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262100212090803120561206c6965",
    "batch_read_request": "0a046d61696e12440a4061616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161100b12440a40626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262621002",
    "batch_read_response": "0a550a440a4061616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161100b120b68656c6c6f20776f726c641a000a4a0a440a406262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626262626210021a020805",
    "read_request": "0a0a626c6f62732f61612f3110041808",
    "read_response": "520a736f6d65206279746573",
    "write_request": "0a1475706c6f6164732f752f626c6f62732f61612f311801520b68656c6c6f20776f726c64",
    "write_response": "080b",
    "server_capabilities": "0a0e0a010112020801208092f4012802220208022a0408021003",
}

ONE = reapi.Digest(hash="a" * 64, size_bytes=11)
TWO = reapi.Digest(hash="b" * 64, size_bytes=2)
TREE = reapi.Digest(hash="c" * 64, size_bytes=33)


def reference(name: str) -> bytes:
    return bytes.fromhex(REFERENCE[name])


def result() -> reapi.ActionResult:
    return reapi.ActionResult(
        raw=reference("action_result"),
        file_digests=[ONE, TWO],
        tree_digests=[TREE],
        stdout=TWO,
    )


# What each reference has to parse into, and what parses it. Requests carry fields the shim does not
# model, such as the instance name, so those are only checked in this direction.
DECODES: list[tuple[str, Callable[[bytes], object], object]] = [
    ("digest", reapi.Digest.parse, ONE),
    (
        "get_action_result_request",
        reapi.GetActionResultRequest.parse,
        reapi.GetActionResultRequest(action_digest=ONE),
    ),
    ("action_result", reapi.ActionResult.parse, result()),
    (
        "update_action_result_request",
        reapi.UpdateActionResultRequest.parse,
        reapi.UpdateActionResultRequest(action_digest=ONE, action_result=result()),
    ),
    (
        "find_missing_request",
        reapi.FindMissingBlobsRequest.parse,
        reapi.FindMissingBlobsRequest(blob_digests=[ONE, TWO]),
    ),
    (
        "find_missing_response",
        reapi.FindMissingBlobsResponse.parse,
        reapi.FindMissingBlobsResponse(missing_blob_digests=[TWO]),
    ),
    (
        "batch_update_request",
        reapi.BatchUpdateBlobsRequest.parse,
        reapi.BatchUpdateBlobsRequest(
            blobs=[
                reapi.Blob(digest=ONE, data=b"hello world"),
                reapi.Blob(digest=TWO, data=b"hi"),
            ]
        ),
    ),
    (
        "batch_update_response",
        reapi.BatchUpdateBlobsResponse.parse,
        reapi.BatchUpdateBlobsResponse(
            blobs=[
                reapi.Blob(digest=ONE, status=reapi.Status(code=reapi.OK)),
                reapi.Blob(
                    digest=TWO,
                    status=reapi.Status(code=reapi.INVALID_ARGUMENT, message="a lie"),
                ),
            ]
        ),
    ),
    (
        "batch_read_request",
        reapi.BatchReadBlobsRequest.parse,
        reapi.BatchReadBlobsRequest(digests=[ONE, TWO]),
    ),
    (
        "batch_read_response",
        reapi.BatchReadBlobsResponse.parse,
        reapi.BatchReadBlobsResponse(
            blobs=[
                reapi.Blob(digest=ONE, data=b"hello world", status=reapi.Status(code=reapi.OK)),
                reapi.Blob(digest=TWO, status=reapi.Status(code=reapi.NOT_FOUND)),
            ]
        ),
    ),
    (
        "read_request",
        reapi.ReadRequest.parse,
        reapi.ReadRequest(resource_name="blobs/aa/1", read_offset=4, read_limit=8),
    ),
    ("read_response", reapi.ReadResponse.parse, reapi.ReadResponse(data=b"some bytes")),
    (
        "write_request",
        reapi.WriteRequest.parse,
        reapi.WriteRequest(resource_name="uploads/u/blobs/aa/1", finish_write=True, data=b"hello world"),
    ),
    ("write_response", reapi.WriteResponse.parse, reapi.WriteResponse(committed_size=11)),
    (
        "server_capabilities",
        reapi.ServerCapabilities.parse,
        reapi.ServerCapabilities(max_batch_total_size_bytes=4000000),
    ),
]

# The messages the shim emits, which have to come out byte for byte as protobuf would write them.
ENCODES: list[tuple[str, reapi.Wireable]] = [
    ("digest", ONE),
    ("action_result", result()),
    ("find_missing_response", reapi.FindMissingBlobsResponse(missing_blob_digests=[TWO])),
    (
        "batch_update_response",
        reapi.BatchUpdateBlobsResponse(
            blobs=[
                reapi.Blob(digest=ONE, status=reapi.Status(code=reapi.OK)),
                reapi.Blob(digest=TWO, status=reapi.Status(code=reapi.INVALID_ARGUMENT, message="a lie")),
            ]
        ),
    ),
    (
        "batch_read_response",
        reapi.BatchReadBlobsResponse(
            blobs=[
                reapi.Blob(digest=ONE, data=b"hello world", status=reapi.Status(code=reapi.OK)),
                reapi.Blob(digest=TWO, status=reapi.Status(code=reapi.NOT_FOUND)),
            ]
        ),
    ),
    ("read_response", reapi.ReadResponse(data=b"some bytes")),
    ("write_response", reapi.WriteResponse(committed_size=11)),
    ("server_capabilities", reapi.ServerCapabilities(max_batch_total_size_bytes=4000000)),
]


class TestReferenceBytes(unittest.TestCase):
    def test_every_reference_parses_to_what_it_says(self) -> None:
        for name, parse, expected in DECODES:
            with self.subTest(message=name):
                self.assertEqual(parse(reference(name)), expected)

    def test_what_the_shim_emits_matches_protobuf_byte_for_byte(self) -> None:
        for name, message in ENCODES:
            with self.subTest(message=name):
                self.assertEqual(message.to_bytes().hex(), REFERENCE[name])

    def test_a_tree_gives_up_every_file_it_lists(self) -> None:
        """Both the root and the children, since an output directory nests."""
        self.assertEqual(reapi.tree_files(reference("tree")), [ONE, TWO])

    def test_a_result_names_its_files_its_trees_and_its_output(self) -> None:
        self.assertEqual(result().named(), [ONE, TWO, TREE, TWO])

    def test_a_result_is_served_as_the_bytes_it_arrived_as(self) -> None:
        """Nothing re-serialises a result, so nothing can change bytes a signature will cover."""
        parsed = reapi.ActionResult.parse(reference("action_result"))
        self.assertEqual(parsed.to_bytes(), reference("action_result"))


class TestUnmodelledFields(unittest.TestCase):
    def test_fields_we_do_not_read_are_ignored(self) -> None:
        """The request carries `instance_name` and `inline_stdout`, which this parser does not model.

        Unknown fields are skipped rather than refused, so a newer Buck2 adding more is served too.
        """
        request = reapi.GetActionResultRequest.parse(reference("get_action_result_request"))
        self.assertEqual(request.action_digest, ONE)

    def test_an_empty_message_parses_to_nothing_rather_than_failing(self) -> None:
        self.assertIsNone(reapi.GetActionResultRequest.parse(b"").action_digest)
        self.assertEqual(reapi.FindMissingBlobsRequest.parse(b"").blob_digests, [])
        self.assertEqual(reapi.WriteResponse.parse(b"").committed_size, 0)

    def test_a_result_with_every_field_at_its_default_is_still_a_result(self) -> None:
        """Buck2's permission probe is one; present is not the same as non-empty."""
        empty = reapi.ActionResult.parse(b"")
        request = reapi.UpdateActionResultRequest(action_digest=ONE, action_result=empty).to_bytes()
        self.assertEqual(reapi.UpdateActionResultRequest.parse(request).action_result, empty)
