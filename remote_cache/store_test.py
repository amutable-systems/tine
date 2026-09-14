"""The store on disk: what it keeps, what it drops, and what it remembers about what it dropped."""

import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import override

import reapi
import store as store_module

BUNDLE = "b" * 64
OTHER_BUNDLE = "c" * 64


def blob(body: bytes) -> tuple[reapi.Digest, bytes]:
    return reapi.Digest(hash=hashlib.sha256(body).hexdigest(), size_bytes=len(body)), body


class StoreCase(unittest.TestCase):
    MAX_BYTES = 1_000_000
    MAX_ROWS = 1_000_000

    @override
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = self.a_store()

    def a_store(self) -> store_module.Store:
        store = store_module.Store(self.root, max_bytes=self.MAX_BYTES, max_rows=self.MAX_ROWS)
        self.addCleanup(store.close)
        return store

    def restart(self) -> store_module.Store:
        """The same directory, served by a new store. The old one has to let go first."""
        self.store.close()
        self.store = self.a_store()
        return self.store


class TestBlobs(StoreCase):
    def test_a_blob_round_trips(self) -> None:
        digest, body = blob(b"some bytes")
        self.store.put_blob(digest, body)
        self.assertEqual(self.store.blob(digest), body)
        self.assertEqual(self.store.has_blobs([digest]), [True])

    def test_a_blob_that_does_not_hash_to_its_digest_is_refused(self) -> None:
        digest, _ = blob(b"honest")
        with self.assertRaisesRegex(ValueError, "hashes to"):
            self.store.put_blob(digest, b"a lie")

    def test_the_empty_blob_is_always_there(self) -> None:
        """Clients take it for granted rather than uploading it."""
        digest, body = blob(b"")
        self.assertEqual(self.store.blob(digest), body)

    def test_blobs_are_sharded_rather_than_heaped_in_one_directory(self) -> None:
        digest, body = blob(b"sharded")
        self.store.put_blob(digest, body)
        self.assertTrue((self.root / "blobs" / digest.hash[:2] / digest.hash).is_file())

    def test_storing_the_same_blob_twice_counts_it_once(self) -> None:
        digest, body = blob(b"x" * 500)
        self.store.put_blob(digest, body)
        held = self.store.held
        self.store.put_blob(digest, body)
        self.assertEqual(self.store.held, held)

    def test_a_file_that_no_longer_hashes_to_its_name_is_dropped_on_read(self) -> None:
        """The directory is shared and outlives the process, so the name proves nothing about the bytes."""
        digest, body = blob(b"honest")
        self.store.put_blob(digest, body, bundle=BUNDLE)
        (self.root / "blobs" / digest.hash[:2] / digest.hash).write_bytes(b"a lie!")
        self.assertIsNone(self.store.blob(digest))
        self.assertEqual(self.store.has_blobs([digest]), [False])
        self.assertEqual(self.store.held, 0)
        self.assertEqual(self.store.bundle_of(digest), BUNDLE)


class TestPersistence(StoreCase):
    def test_what_was_stored_is_there_after_a_restart(self) -> None:
        """Buck2 declares an artifact in one build and fetches it in a later one."""
        digest, body = blob(b"outlives the process")
        self.store.put_blob(digest, body, bundle=BUNDLE)
        action = reapi.Digest(hash="a" * 64, size_bytes=1)
        self.store.put_result(action, b"the pointer, as the bucket holds it")
        self.store.put_certificate(b"\x01\x02", "-----BEGIN CERTIFICATE-----")

        again = self.restart()
        self.assertEqual(again.blob(digest), body)
        self.assertEqual(again.bundle_of(digest), BUNDLE)
        self.assertEqual(again.result(action), b"the pointer, as the bucket holds it")
        self.assertEqual(again.certificate(b"\x01\x02"), "-----BEGIN CERTIFICATE-----")
        self.assertIsNone(again.certificate(b"\x03"))
        again.drop_result(action)
        self.assertIsNone(again.result(action))

    def test_a_blob_deleted_behind_its_back_is_noticed_at_startup(self) -> None:
        """A kill between the file and the row, or a directory emptied by hand."""
        digest, body = blob(b"gone by morning")
        self.store.put_blob(digest, body)
        (self.root / "blobs" / digest.hash[:2] / digest.hash).unlink()

        again = self.restart()
        self.assertEqual(again.has_blobs([digest]), [False])
        self.assertEqual(again.held, 0)
        self.assertEqual(again.counts()[0], 1)  # the empty blob, which it rewrites

    def test_a_file_the_index_does_not_know_is_swept_at_startup(self) -> None:
        """The other half of the same kill: the bytes landed, the row never did. Or a temp file."""
        digest, body = blob(b"never made it into the index")
        orphan = self.root / "blobs" / digest.hash[:2] / digest.hash
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(body)
        leftover = orphan.with_name(f".{digest.hash}.tmp123")
        leftover.write_bytes(body[:3])

        self.restart()
        self.assertFalse(orphan.exists())
        self.assertFalse(leftover.exists())


class TestExclusive(StoreCase):
    def test_a_second_store_on_one_directory_is_refused_naming_the_first(self) -> None:
        """Two of them would each keep their own idea of what is on disk, into one index."""
        with self.assertRaisesRegex(RuntimeError, f"already served by pid {os.getpid()}"):
            self.a_store()


class TestProvenance(StoreCase):
    def test_where_a_blob_came_from_outlives_the_blob(self) -> None:
        """The whole reason the index exists: an evicted blob can be asked for again."""
        digest, body = blob(b"evict me")
        self.store.put_blob(digest, body, bundle=BUNDLE)
        self.store.drop_blob(digest)
        self.assertIsNone(self.store.blob(digest))
        self.assertEqual(self.store.bundle_of(digest), BUNDLE)

    def test_provenance_can_be_recorded_without_the_bytes(self) -> None:
        digest, _ = blob(b"never stored here")
        self.store.remember(digest, BUNDLE)
        self.assertEqual(self.store.bundle_of(digest), BUNDLE)
        self.assertEqual(self.store.has_blobs([digest]), [False])

    def test_a_later_bundle_replaces_an_earlier_one(self) -> None:
        """Identical outputs share a bundle, so the same blob arrives under more than one name."""
        digest, body = blob(b"shared between results")
        self.store.put_blob(digest, body, bundle=BUNDLE)
        self.store.remember(digest, OTHER_BUNDLE)
        self.assertEqual(self.store.bundle_of(digest), OTHER_BUNDLE)

    def test_a_blob_with_no_provenance_says_so(self) -> None:
        digest, body = blob(b"uploaded by buck, from nowhere")
        self.store.put_blob(digest, body)
        self.assertIsNone(self.store.bundle_of(digest))


class TestEviction(StoreCase):
    MAX_BYTES = 1000

    def test_the_least_recently_used_goes_first(self) -> None:
        """Used, not written: a read has to count, whatever the mount's atime policy is."""
        old, old_body = blob(b"o" * 400)
        new, new_body = blob(b"n" * 400)
        self.store.put_blob(old, old_body)
        self.store.put_blob(new, new_body)
        self.assertEqual(self.store.blob(old), old_body)

        third, third_body = blob(b"t" * 400)
        self.store.put_blob(third, third_body)
        self.assertEqual(self.store.has_blobs([old, new, third]), [True, False, True])

    def test_it_stays_under_its_bound_and_what_it_evicted_can_still_be_found_again(self) -> None:
        for index in range(10):
            digest, body = blob(f"{index}".encode() * 300)
            self.store.put_blob(digest, body, bundle=BUNDLE)
        self.assertLessEqual(self.store.held, 1000)
        gone = [one for one in range(10) if not self.store.has_blobs([blob(f"{one}".encode() * 300)[0]])[0]]
        self.assertTrue(gone)
        for index in gone:
            digest, _ = blob(f"{index}".encode() * 300)
            self.assertEqual(self.store.bundle_of(digest), BUNDLE)

    def test_a_blob_larger_than_the_bound_does_not_wedge_it(self) -> None:
        """It is stored, then immediately evicted, rather than refused or kept forever."""
        digest, body = blob(b"x" * 5000)
        self.store.put_blob(digest, body, bundle=BUNDLE)
        self.assertLessEqual(self.store.held, 1000)

    def test_the_empty_blob_survives_eviction(self) -> None:
        """It is the oldest thing in every store and frees nothing, so it would always go first.

        Clients never upload it, so once it is gone a result naming an empty file is unpublishable.
        """
        for index in range(4):
            digest, body = blob(f"{index}".encode() * 300)
            self.store.put_blob(digest, body)
        self.assertEqual(self.store.has_blobs([blob(b"")[0]]), [True])


class TestRowBound(StoreCase):
    """The index grows for as long as the store exists, because rows outlive their bytes."""

    MAX_BYTES = 10_000_000
    MAX_ROWS = 12

    def test_it_stays_under_its_row_bound_dropping_the_oldest_first(self) -> None:
        first = blob(b"oldest")[0]
        self.store.remember(first, BUNDLE)
        for index in range(self.MAX_ROWS + 4):
            self.store.remember(blob(f"newer {index}".encode())[0], BUNDLE)
        self.assertLessEqual(sum(self.store.counts()), self.MAX_ROWS)
        self.assertIsNone(self.store.bundle_of(first))

    def test_a_blob_that_is_here_keeps_its_provenance(self) -> None:
        """Dropping that row would throw away the one thing making the bytes replaceable."""
        kept, body = blob(b"still here")
        self.store.put_blob(kept, body, bundle=BUNDLE)
        for index in range(self.MAX_ROWS * 3):
            self.store.remember(blob(f"provenance {index}".encode())[0], OTHER_BUNDLE)
        self.assertEqual(self.store.bundle_of(kept), BUNDLE)
        self.assertEqual(self.store.blob(kept), body)

    def test_provenance_ages_from_the_eviction_not_the_write(self) -> None:
        """What the row has to outlive is Buck2 asking again, and that clock starts when the bytes go."""
        evicted, body = blob(b"here for a long time")
        self.store.put_blob(evicted, body, bundle=BUNDLE)
        for index in range(4):
            self.store.remember(blob(f"younger {index}".encode())[0], OTHER_BUNDLE)
        self.store.drop_blob(evicted)
        # Six rows so far, the empty blob included; push exactly four over the bound.
        for index in range(self.MAX_ROWS - 2):
            self.store.remember(blob(f"youngest {index}".encode())[0], OTHER_BUNDLE)
        self.assertEqual(self.store.bundle_of(evicted), BUNDLE)
        self.assertIsNone(self.store.bundle_of(blob(b"younger 0")[0]))

    def test_present_blobs_alone_may_exceed_it(self) -> None:
        """Then the row bound does not bind, rather than evicting what it must not."""
        for index in range(self.MAX_ROWS + 8):
            digest, body = blob(f"present {index}".encode())
            self.store.put_blob(digest, body, bundle=BUNDLE)
        self.assertGreater(self.store.counts()[0], self.MAX_ROWS)

    def test_results_are_droppable_too(self) -> None:
        """Losing one costs a bucket request, which is the cheapest thing here to lose."""
        for index in range(self.MAX_ROWS * 2):
            action = reapi.Digest(hash=f"{index:064x}", size_bytes=1)
            self.store.put_result(action, b"")
        self.assertLessEqual(self.store.counts()[1], self.MAX_ROWS)


class TestSchemaVersion(StoreCase):
    def test_a_store_from_another_version_is_thrown_away_blob_files_included(self) -> None:
        """A cache is the one thing that may simply be dropped instead of migrated.

        The files go with the index: a blob whose row is gone is a file nothing can ever name again.
        """
        digest, body = blob(b"written by an older shim")
        self.store.put_blob(digest, body, bundle=BUNDLE)
        path = self.root / "blobs" / digest.hash[:2] / digest.hash
        self.store.close()
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db:
            db.execute(f"PRAGMA user_version = {store_module.SCHEMA_VERSION + 1}")

        again = self.a_store()
        self.assertEqual(again.has_blobs([digest]), [False])
        self.assertIsNone(again.bundle_of(digest))
        self.assertEqual(again.held, 0)
        self.assertFalse(path.exists())

    def test_a_fresh_directory_is_not_mistaken_for_an_old_one(self) -> None:
        self.assertEqual(self.store.counts(), (1, 0))  # the empty blob
