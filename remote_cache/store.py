"""What the shim keeps locally: blobs on disk, results and provenance beside them.

On disk, because a shim outlives the build that filled it: Buck2 declares an artifact in one build and
fetches it in a later one, and a real cacheable set does not fit in a process anyway. On disk means
bounded, and bounded means a blob can be evicted while the bundle it came from is still in the bucket.
Buck2 can then ask for it by digest alone, with no result to say where to look, so every blob remembers
which bundle carried it, and that row outlives the bytes: it is what makes a second fetch possible
instead of a failed build.

Blobs are kept one per file rather than as the bundles the bucket holds, because that is the shape
of every request: Buck2 uploads, asks for and reads blobs by digest, never by bundle, and a bundle
only comes into being when a result is published. It also stores a blob once however many bundles
carry it, evicts at the granularity Buck2 reads at, and serves a ranged read straight from a file.
Everything else is a row: results are a few hundred bytes and provenance is two hashes.
"""

import fcntl
import hashlib
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO, cast

import reapi
import signing

log = logging.getLogger("store")


def write_atomically(path: Path, data: bytes) -> None:
    """Written beside and renamed, so a reader never sees half a file.

    The temporary name is unique per write, not per path: two threads store the same blob, or two
    actions with identical outputs publish the same bundle, at the same moment, and a name shared
    by path means one rename finds the file the other already moved. A real build hits this
    within minutes.
    """
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temporary, path)
    finally:
        # Gone already after the rename; still there after anything that stopped short of it.
        Path(temporary).unlink(missing_ok=True)


def _now() -> int:
    """Unix nanoseconds.

    Finer than anything reads it back, on purpose: this is what orders row eviction, and a whole
    build's worth of rows lands inside one second.
    """
    return time.time_ns()


# Held open for as long as a store is served, naming who holds it.
LOCK_NAME = "lock"

# Bumped whenever the tables below change shape. A store is a cache, so the answer to meeting an
# older one is to throw it away rather than to carry migrations for something nobody deployed.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS blob (
    hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    -- The bundle this arrived in, kept after the bytes are evicted so they can be fetched again.
    bundle TEXT,
    -- Whether the bytes are on disk. A row with none is provenance and nothing else.
    present INTEGER NOT NULL,
    -- Unix nanoseconds when this row was last written, or its bytes last evicted. The order the
    -- row bound evicts in.
    seen INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS blob_present ON blob (present);
CREATE INDEX IF NOT EXISTS blob_seen ON blob (seen);

CREATE TABLE IF NOT EXISTS result (
    action TEXT PRIMARY KEY,
    -- Which bundle backs it, so an incomplete result can be made whole rather than missed.
    bundle TEXT,
    proto BLOB NOT NULL,
    seen INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS result_seen ON result (seen);
"""


class Store:
    """Blobs, results and where they came from, under one directory.

    Two bounds, because the two halves fail differently. `max_bytes` covers the blobs, which are
    the big thing and are cheap to lose: a bundle fetch brings them back. `max_rows` covers the
    index, which is small per entry but grows for as long as the store exists, because a row
    deliberately outlives the bytes it describes. Losing the wrong row costs a rebuild, so the row
    bound has to be loose enough that provenance outlives its blob by a wide margin, which a
    default of a million rows against a store measured in gigabytes comfortably is.
    """

    def __init__(self, root: Path, max_bytes: int, max_rows: int = 1_000_000) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.max_rows = max_rows
        self.blobs = root / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self._held_by = self._claim()
        self._lock = threading.Lock()
        # One connection behind one lock: the server is threaded, and sqlite is not the bottleneck
        # in anything this does.
        self._db = sqlite3.connect(root / "index.sqlite", check_same_thread=False)
        # Like the blobs: which actions this machine builds is nobody else's business.
        (root / "index.sqlite").chmod(0o600)
        self._discard_if_old()
        self._db.executescript(SCHEMA)
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._db.commit()
        self._reconcile()
        # Clients take the empty blob for granted rather than uploading it, so it cannot be absent.
        self.put_blob(reapi.Digest.for_bytes(b""), b"")

    def _claim(self) -> TextIO:
        """Take the directory, or refuse to run.

        Two processes sharing one store would each keep their own idea of what is on disk and how
        much of it there is, and write both into the same index. `flock` rather than `lockf`: the
        lock belongs to this open file, so it is not dropped by some unrelated close of the same
        path, and a second store in one process is caught too.
        """
        handle = (self.root / LOCK_NAME).open("a+", encoding="utf-8")
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            holder = handle.read().strip() or "an unknown process"
            handle.close()
            raise RuntimeError(f"{self.root} is already served by {holder}") from None
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid {os.getpid()}\n")
        handle.flush()
        return handle

    def _discard_if_old(self) -> None:
        """Start over if this store was written by a different version of these tables.

        A cache is the one kind of state that may simply be dropped, so an older one costs a slow
        build rather than a migration nobody would exercise. The blobs go with it: their rows are
        what says where they came from, and a blob with no row is a file nothing can ever name.
        """
        # Zero is what a database nobody has written yet reports; nothing older than the first
        # version was ever deployed, so nothing else reports it.
        found = cast(int, self._db.execute("PRAGMA user_version").fetchone()[0])
        if found in (0, SCHEMA_VERSION):
            return
        log.warning("store at %s is version %d, not %d: starting over", self.root, found, SCHEMA_VERSION)
        self._db.close()
        (self.root / "index.sqlite").unlink(missing_ok=True)
        shutil.rmtree(self.blobs, ignore_errors=True)
        self.blobs.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.root / "index.sqlite", check_same_thread=False)

    def close(self) -> None:
        """Give up the database and the directory, in that order."""
        self._db.close()
        self._held_by.close()

    def _path(self, digest_hash: str) -> Path:
        """Sharded by the first byte, because a directory of a million entries is a slow one.

        The bucket needs no such thing, having no directories; a filesystem does. The hash becomes
        a path here and nowhere else, so this is where anything that is not a hash is stopped: a
        client is trusted, but `../` in a digest reading a file outside the store is not a thing a
        trusted client should be able to do by accident either.
        """
        if not signing.is_sha256_hex(digest_hash):
            raise ValueError(f"not a SHA-256 hex digest: {digest_hash!r}")
        return self.blobs / digest_hash[:2] / digest_hash

    def _reconcile(self) -> None:
        """Agree with the filesystem about what is actually here.

        A kill between writing a file and committing the row, or a directory emptied by hand,
        leaves the two disagreeing either way: a row without its file, or a file without a row. The
        second kind includes a temporary file the kill left behind. A file nothing can name would
        otherwise sit outside the byte bound forever, so both are settled at startup.
        """
        with self._lock:
            rows = dict(self._db.execute("SELECT hash, size FROM blob WHERE present = 1").fetchall())
            on_disk = {path.name: path for shard in self.blobs.iterdir() for path in shard.iterdir()}
            missing = [(one,) for one in rows if one not in on_disk]
            if missing:
                self._db.executemany("UPDATE blob SET present = 0 WHERE hash = ?", missing)
                self._db.commit()
                log.info("%d blobs in the index were not on disk", len(missing))
            orphans = [path for name, path in on_disk.items() if name not in rows]
            for path in orphans:
                path.unlink()
            if orphans:
                log.info("%d files on disk were not in the index", len(orphans))
            self.held = sum(size for one, size in rows.items() if one in on_disk)

    # Blobs.

    def put_blob(self, digest: reapi.Digest, data: bytes, bundle: str | None = None) -> None:
        """Store a blob under the digest it was filed under, if that is what it hashes to.

        Checking here is what a cache filled from somewhere else cannot skip, so it is not an
        assertion about our own client but the one place the rule belongs.
        """
        found = hashlib.sha256(data).hexdigest()
        if found != digest.hash or len(data) != digest.size_bytes:
            raise ValueError(f"filed under {digest} but hashes to {found}/{len(data)}")
        path = self._path(digest.hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomically(path, data)
        with self._lock:
            known = self._db.execute(
                "SELECT size, present FROM blob WHERE hash = ?", (digest.hash,)
            ).fetchone()
            self._db.execute(
                "INSERT INTO blob (hash, size, bundle, present, seen) VALUES (?, ?, ?, 1, ?)"
                " ON CONFLICT (hash) DO UPDATE SET present = 1, bundle = COALESCE(?, bundle), seen = ?",
                (digest.hash, digest.size_bytes, bundle, _now(), bundle, _now()),
            )
            self._db.commit()
            if known is None or not known[1]:
                self.held += digest.size_bytes
        self._evict()
        self._evict_rows()

    def blob(self, digest: reapi.Digest) -> bytes | None:
        if not signing.is_sha256_hex(digest.hash):
            return None
        path = self._path(digest.hash)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        # By hand, because the filesystem's own atime is not to be relied on for this: `relatime`
        # records a read at most once a day, and `noatime`, usual in containers, never. One
        # syscall per read keeps eviction least-recently-*used* everywhere.
        os.utime(path)
        return data

    def has_blobs(self, digests: list[reapi.Digest]) -> list[bool]:
        """Which of these are here. Something that is not a digest is not, rather than an error."""
        return [signing.is_sha256_hex(one.hash) and self._path(one.hash).is_file() for one in digests]

    # The two below are how a test takes blobs away to see what happens: nothing in the shim
    # enumerates or evicts by hand.

    def digests(self) -> list[reapi.Digest]:
        """Every blob whose bytes are here."""
        with self._lock:
            rows = self._db.execute("SELECT hash, size FROM blob WHERE present = 1").fetchall()
        return [reapi.Digest(hash=one, size_bytes=size) for one, size in rows]

    def drop_blob(self, digest: reapi.Digest) -> None:
        """Forget a blob's bytes. Where it came from is kept: that is how it can be asked for again."""
        self._forget([(digest.hash, digest.size_bytes)])

    def remember(self, digest: reapi.Digest, bundle: str) -> None:
        """Record which bundle a blob arrived in, whether or not its bytes are still here."""
        with self._lock:
            self._db.execute(
                "INSERT INTO blob (hash, size, bundle, present, seen) VALUES (?, ?, ?, 0, ?)"
                " ON CONFLICT (hash) DO UPDATE SET bundle = ?, seen = ?",
                (digest.hash, digest.size_bytes, bundle, _now(), bundle, _now()),
            )
            self._db.commit()
        self._evict_rows()

    def bundle_of(self, digest: reapi.Digest) -> str | None:
        """Where a blob can be fetched again, if anything here ever knew."""
        with self._lock:
            row = self._db.execute("SELECT bundle FROM blob WHERE hash = ?", (digest.hash,)).fetchone()
        return cast("str | None", row[0]) if row else None

    def members_of(self, bundle: str) -> set[str]:
        """Every blob this store attributes to a bundle, present or not."""
        with self._lock:
            rows = self._db.execute("SELECT hash FROM blob WHERE bundle = ?", (bundle,)).fetchall()
        return {one for (one,) in rows}

    # Results.

    def put_result(
        self, action: reapi.Digest, result: reapi.ActionResult, bundle: str | None = None
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO result (action, bundle, proto, seen) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (action) DO UPDATE SET proto = ?, bundle = COALESCE(?, bundle), seen = ?",
                (action.hash, bundle, result.raw, _now(), result.raw, bundle, _now()),
            )
            self._db.commit()
        self._evict_rows()

    def result(self, action: reapi.Digest) -> reapi.ActionResult | None:
        with self._lock:
            row = self._db.execute("SELECT proto FROM result WHERE action = ?", (action.hash,)).fetchone()
        return reapi.ActionResult.parse(cast(bytes, row[0])) if row else None

    def result_bundle(self, action: reapi.Digest) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT bundle FROM result WHERE action = ?", (action.hash,)).fetchone()
        return cast("str | None", row[0]) if row else None

    def counts(self) -> tuple[int, int]:
        with self._lock:
            blobs = self._db.execute("SELECT COUNT(*) FROM blob WHERE present = 1").fetchone()[0]
            results = self._db.execute("SELECT COUNT(*) FROM result").fetchone()[0]
        return cast(int, blobs), cast(int, results)

    # Eviction.

    def _forget(self, blobs: Iterable[tuple[str, int]]) -> None:
        """Drop the bytes and keep the row, dated from now.

        The row's age is what decides when it too is forgotten, and what it has to outlive is the
        chance of Buck2 asking for this digest again with no result in front of it. That chance
        starts when the bytes go, not when they arrived: a blob read every day for a month would
        otherwise carry the oldest row in the index the moment it was evicted.
        """
        freed = 0
        with self._lock:
            for one, size in blobs:
                self._path(one).unlink(missing_ok=True)
                changed = self._db.execute(
                    "UPDATE blob SET present = 0, seen = ? WHERE hash = ? AND present = 1", (_now(), one)
                )
                freed += size * changed.rowcount
            self._db.commit()
            self.held -= freed

    def _evict(self) -> None:
        """Drop the least recently used blobs until the store is inside its bound.

        Least recently used by the file's own timestamp, which `blob` moves on every read.
        """
        if self.held <= self.max_bytes:
            return
        with self._lock:
            # Only blobs whose bytes are worth something. The one blob of no size is the empty one,
            # which clients take for granted rather than uploading, so dropping it frees nothing and
            # costs every later result that names an empty file.
            rows = self._db.execute("SELECT hash, size FROM blob WHERE present = 1 AND size > 0").fetchall()
        ages = []
        for one, size in rows:
            try:
                ages.append((self._path(one).stat().st_atime, one, size))
            except FileNotFoundError:
                # Dropped by another thread between the query and here.
                continue
        ages.sort()
        over = self.held - self.max_bytes
        going = []
        freed = 0
        for _, one, size in ages:
            if freed >= over:
                break
            going.append((one, size))
            freed += size
        if going:
            self._forget(going)
            log.info("evicted %d blobs, %d bytes, to stay under %d", len(going), freed, self.max_bytes)

    def _evict_rows(self) -> None:
        """Drop the oldest rows the index can afford to lose, once it holds too many.

        Two kinds are affordable. A `result` row costs one bucket request to fetch again. A `blob`
        row whose bytes are gone costs a rebuild, but only if Buck2 asks for exactly that digest
        again, which grows less likely the longer nothing has.

        A blob whose bytes are *here* is never dropped: its provenance is what lets it be fetched
        again after eviction, so forgetting it while keeping the bytes would throw away the only
        thing that makes the bytes replaceable. Those rows are bounded by `max_bytes` instead, and
        if they alone exceed the row bound then the row bound simply does not bind.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT (SELECT COUNT(*) FROM blob) + (SELECT COUNT(*) FROM result)"
            ).fetchone()[0]
            over = cast(int, rows) - self.max_rows
            if over <= 0:
                return
            # One budget over both tables, oldest first, so whichever is growing gives way.
            going = self._db.execute(
                "SELECT which, id FROM ("
                "  SELECT 'blob' AS which, hash AS id, seen FROM blob WHERE present = 0"
                "  UNION ALL SELECT 'result', action, seen FROM result"
                ") ORDER BY seen LIMIT ?",
                (over,),
            ).fetchall()
            for which, one in going:
                self._db.execute(
                    "DELETE FROM blob WHERE hash = ?"
                    if which == "blob"
                    else "DELETE FROM result WHERE action = ?",
                    (one,),
                )
            self._db.commit()
        if going:
            log.info("forgot %d index rows to stay under %d", len(going), self.max_rows)
