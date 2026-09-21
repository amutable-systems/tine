"""Manage the shim serving the shared build cache.

Buck speaks its remote cache API to a shim on this machine, and the shim talks to the bucket; see
docs/remote-cache.md. This reads the `[cache]` settings, starts the shim through Buck when nothing
serves the configured store yet, and asks a running one what it is doing. The shim itself is
`tine//remote_cache:shim`, and it runs in a box, so Buck is what builds and starts it.
"""

import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from util import cache_home, fail, object_table, resolved

# The settings table this reads, which `tine` allows in a project's settings files.
SECTION = "cache"
SHIM = "tine//remote_cache:shim"

# The first start builds the shim's box, which is minutes on a machine that has never built one.
START_TIMEOUT = 900

# Where a build registers itself, so the shim outlives it; the same path as `remote_cache/status.py`.
CLIENTS = "/clients/"

# Below the ephemeral range in /proc/sys/net/ipv4/ip_local_port_range, so an outgoing connection's
# source port cannot be holding the one a shim is about to bind.
PORT_BASE = 20480
PORT_COUNT = 4096

# Settings which can't be committed to tine.toml. dir= is machine specific, and a checked out branch
# must not weaken a configured cache.
LOCAL_ONLY = frozenset({"dir", "s3_insecure", "unsigned"})

KEYS = frozenset(
    {
        "authority",
        "dir",
        "enabled",
        "object_lifetime",
        "port",
        "read_url",
        "s3_bucket",
        "s3_endpoint",
        "s3_insecure",
        "s3_key_file",
        "signing_certificate",
        "signing_key",
        "store_size",
        "unsigned",
    }
)


@dataclass(frozen=True, slots=True)
class CacheSettings:
    """The shared cache a project builds against, as `[cache]` in its settings describes it.

    A reader names `read_url` and either `authority` or `unsigned`. A builder adds the S3 bucket it
    writes to and the key it signs with. The rest has a default, the shim's where it has one.
    """

    read_url: str
    authorities: tuple[Path, ...]
    s3_endpoint: str | None
    s3_bucket: str | None
    s3_key_file: Path | None
    s3_insecure: bool
    signing_key: Path | None
    signing_certificate: Path | None
    object_lifetime: int | None
    dir: Path
    store_size: int | None
    port: int

    @property
    def accepts_uploads(self) -> bool:
        """Return whether the settings permit writing."""
        return not self.authorities or self.signing_key is not None


def _digest(text: str) -> str:
    """A short stable name for a string, for a path or a port that has to key on one."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _string(table: Mapping[str, object], key: str, source: str) -> str | None:
    """A URL, a host or a bucket name, none of which hold whitespace."""
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != "".join(value.split()):
        fail(f"[{SECTION}] {key} in {source} must be a non-empty string without whitespace")
    return value


def _bool(table: Mapping[str, object], key: str, default: bool, source: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        fail(f"[{SECTION}] {key} in {source} must be true or false")
    return value


def _number(table: Mapping[str, object], key: str, floor: int, ceiling: int, source: str) -> int | None:
    # A TOML boolean is an int to isinstance, so it has to be refused before the range check.
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not floor <= value <= ceiling:
        fail(f"[{SECTION}] {key} in {source} must be a whole number from {floor} to {ceiling}")
    return value


def _path(value: object, key: str, root: Path, source: str) -> Path:
    """One path from the settings, relative to the project like everything else in them."""
    if not isinstance(value, str) or not value:
        fail(f"[{SECTION}] {key} in {source} must be a non-empty path")
    return resolved(root / Path(value).expanduser())


def _optional_path(table: Mapping[str, object], key: str, root: Path, source: str) -> Path | None:
    value = table.get(key)
    return None if value is None else _path(value, key, root, source)


def _paths(table: Mapping[str, object], key: str, root: Path, source: str) -> tuple[Path, ...]:
    value = table.get(key, [])
    if not isinstance(value, list):
        fail(f"[{SECTION}] {key} in {source} must be a list of paths")
    return tuple(_path(one, key, root, source) for one in value)


def settings(config: Mapping[str, object], root: Path, source: str) -> CacheSettings | None:
    """The shared cache this project builds against, or None when nothing configures one."""
    table = object_table(config.get(SECTION, {}), f"[{SECTION}] in {source}")
    if not table or not _bool(table, "enabled", True, source):
        return None
    if extra := sorted(set(table) - KEYS):
        fail(f"[{SECTION}] in {source} has unsupported keys: {', '.join(extra)}")

    read_url = _string(table, "read_url", source)
    if read_url is None:
        fail(f"[{SECTION}] in {source} needs read_url, where the bucket is read from")
    authorities = _paths(table, "authority", root, source)
    unsigned = _bool(table, "unsigned", False, source)
    if bool(authorities) == unsigned:
        fail(
            f"[{SECTION}] in {source} needs authority, or unsigned = true to trust whoever writes the bucket"
        )

    s3_bucket = _string(table, "s3_bucket", source)
    s3_endpoint = _string(table, "s3_endpoint", source)
    if s3_endpoint is not None and "/" in s3_endpoint:
        # The shim puts the scheme in front itself, from s3_insecure, so one written here ends up in
        # the host part of every request URL and surfaces as a name lookup failure at the first write.
        fail(f"[{SECTION}] s3_endpoint in {source} must be a host, without a scheme or a path")
    s3_key_file = _optional_path(table, "s3_key_file", root, source)
    s3_insecure = _bool(table, "s3_insecure", False, source)
    if s3_bucket is None:
        for key in ("s3_endpoint", "s3_key_file", "s3_insecure"):
            if key in table:
                fail(f"[{SECTION}] {key} in {source} needs s3_bucket")
    elif s3_endpoint is None or s3_key_file is None:
        fail(f"[{SECTION}] s3_bucket in {source} needs s3_endpoint and s3_key_file")

    signing_key = _optional_path(table, "signing_key", root, source)
    signing_certificate = _optional_path(table, "signing_certificate", root, source)
    object_lifetime = _number(table, "object_lifetime", 0, 36500, source)
    if signing_key is None:
        for key in ("signing_certificate", "object_lifetime"):
            if key in table:
                fail(f"[{SECTION}] {key} in {source} needs signing_key")
        if s3_bucket is not None and not unsigned:
            # Readers holding an authority refuse unsigned results, so every upload would be wasted.
            fail(f"[{SECTION}] s3_bucket in {source} needs signing_key, or unsigned = true")
    else:
        if unsigned:
            fail(f"[{SECTION}] signing_key in {source} needs authority: it says who may read what it signs")
        if s3_bucket is None:
            fail(f"[{SECTION}] signing_key in {source} needs s3_bucket to publish to")
        if signing_certificate is None:
            fail(f"[{SECTION}] signing_key in {source} needs signing_certificate, for readers to find")

    directory = _optional_path(table, "dir", root, source) or resolved(cache_home() / "tine" / "cache")
    # Derived from the store, so two checkouts configured alike agree on a port.
    derived_port = PORT_BASE + int(_digest(str(directory))[:4], 16) % PORT_COUNT
    return CacheSettings(
        read_url=read_url,
        authorities=authorities,
        s3_endpoint=s3_endpoint,
        s3_bucket=s3_bucket,
        s3_key_file=s3_key_file,
        s3_insecure=s3_insecure,
        signing_key=signing_key,
        signing_certificate=signing_certificate,
        object_lifetime=object_lifetime,
        dir=directory,
        store_size=_number(table, "store_size", 1, 1 << 20, source),
        port=_number(table, "port", 1, 65535, source) or derived_port,
    )


def arguments(cache: CacheSettings) -> list[str]:
    """The shim's command line for these settings.

    Also what a running shim is recognised by: it reports its own arguments, and a shim started for
    other settings must not be shared, since it may sign as someone else or read another bucket.
    """
    args: list[str] = ["--store", str(cache.dir), "--port", str(cache.port), "--read-url", cache.read_url]
    for authority in cache.authorities:
        args += ["--authority", str(authority)]
    if not cache.authorities:
        args.append("--unsigned")
    if cache.s3_bucket is not None:
        assert cache.s3_endpoint is not None and cache.s3_key_file is not None
        args += ["--s3-bucket", cache.s3_bucket, "--s3-endpoint", cache.s3_endpoint]
        args += ["--s3-key-file", str(cache.s3_key_file)]
        if cache.s3_insecure:
            args.append("--s3-insecure")
    if cache.signing_key is not None:
        assert cache.signing_certificate is not None
        args += [
            "--signing-key",
            str(cache.signing_key),
            "--signing-certificate",
            str(cache.signing_certificate),
        ]
        if cache.object_lifetime is not None:
            args += ["--object-lifetime", str(cache.object_lifetime)]
    if cache.store_size is not None:
        args += ["--store-size", str(cache.store_size)]
    return args


def socket_path(store: Path) -> Path:
    """Where the shim serving `store` answers; the same derivation as `remote_cache/status.py`.

    Both ends compute it rather than passing it around, so this is the one duplicate of that code,
    and a test holds the two together.
    """
    named = _digest(str(store.resolve()))
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / f"tine-cache-{named}.sock"
    private = Path(tempfile.gettempdir()) / f"tine-cache-{os.getuid()}"
    private.mkdir(mode=0o700, exist_ok=True)
    if private.stat().st_uid != os.getuid():
        fail(f"{private} belongs to someone else")
    return private / f"{named}.sock"


def ask(store: Path, timeout: float = 5, client: int | None = None) -> dict[str, object] | None:
    """What the shim serving `store` says about itself, or None if nothing is.

    `client` is a process about to build. A shim exits once it has been idle for a while, and a
    build can go that long between two cache calls, so whoever is about to hand over to Buck says
    so here and the shim stays for as long as that process lives.
    """
    path = socket_path(store)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    with connection:
        try:
            connection.connect(str(path))
            _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                fail(f"{path} is served by uid {uid}, not us")
            with connection.makefile("rwb") as stream:
                request = "GET /" if client is None else f"POST {CLIENTS}{client}"
                stream.write(f"{request} HTTP/1.0\r\n\r\n".encode())
                stream.flush()
                _, _, body = stream.read().partition(b"\r\n\r\n")
        except OSError:
            # No socket, one left behind by a shim that is gone, or one closing under us as its
            # shim stops. Nothing is serving in each case.
            return None
    return cast(dict[str, object], json.loads(body))


def log_path(cache: CacheSettings) -> Path:
    """Everything the shim, and the Buck run that started it, printed since it last started."""
    return cache_home() / "tine" / "cache-shim" / f"{_digest(str(cache.dir))}.log"


def _other(report: Mapping[str, object], cache: CacheSettings) -> str:
    return (
        f"[{SECTION}] a cache shim already serves {cache.dir} with other settings, as pid "
        f"{report.get('pid')}; stop it, or wait for its idle timeout"
    )


def start(cache: CacheSettings, buck: list[str]) -> None:
    """Start the shim through `buck`, and wait until it serves or say why it does not.

    Buck builds the shim's box first, so what this waits for is the status socket rather than a
    process: the process is Buck for most of the wait. A concurrent start from another checkout
    loses the store's lock to the first, which is then the one to use.
    """
    expected = arguments(cache)
    log = log_path(cache)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as out:
        process = subprocess.Popen(
            [*buck, "run", "-v", "0", SHIM, "--", *expected],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            # Its own session, so a closing terminal does not take the shim with it.
            start_new_session=True,
        )
    started = time.monotonic()
    announced = False
    while True:
        report = ask(cache.dir, client=os.getpid())
        if report is not None:
            if report.get("argv") != expected:
                fail(_other(report, cache))
            return
        if process.poll() is not None:
            if (report := ask(cache.dir, client=os.getpid())) is not None and report.get("argv") == expected:
                return
            tail = " ".join(log.read_text(errors="replace").split()[-60:])
            fail(f"the cache shim exited with {process.returncode} instead of serving {cache.dir}: {tail}")
        waited = time.monotonic() - started
        if waited > START_TIMEOUT:
            os.killpg(process.pid, 9)
            fail(f"the cache shim has not served {cache.dir} after {START_TIMEOUT}s; see {log}")
        # Only once the wait is long enough to be mistaken for a hang.
        if not announced and waited > 2:
            announced = True
            print(f"tine: starting the cache shim through Buck; see {log}", file=sys.stderr)
        time.sleep(0.2)


def ensure(cache: CacheSettings, prepare: Callable[[], list[str]]) -> None:
    """Have a shim serving this cache, starting one with the Buck `prepare` readies unless one does.

    This process is what becomes Buck, so registering it here is what keeps the shim from timing
    out during a build whose actions have nothing to ask it for a while.
    """
    report = ask(cache.dir, client=os.getpid())
    if report is None:
        start(cache, prepare())
    elif report.get("argv") != arguments(cache):
        fail(_other(report, cache))


def status(cache: CacheSettings) -> None:
    """Report what serves this cache, and what it has been doing."""
    print(f"store     {cache.dir}")
    print(f"address   127.0.0.1:{cache.port}")
    print(f"log       {log_path(cache)}")
    report = ask(cache.dir)
    if report is None:
        print("shim      not running; the next build starts one")
        return
    print(f"shim      pid {report.get('pid')}, {report.get('bucket')}, {report.get('trust')}")
    held = f"{report.get('blobs')} blobs, {report.get('results')} results, {report.get('held_bytes')} bytes"
    print(f"holding   {held}")
    print(f"idle      {report.get('idle_seconds')}s, {report.get('builds')} builds running")
    for what, count in cast(dict[str, int], report.get("counts", {})).items():
        print(f"{what:<17} {count}")
