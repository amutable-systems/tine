"""Manage the remote cache shim, which translates Buck's gRPC API to S3.

Abstracts the role, as there are several implementations around and we might want/have to switch at some
point.

Everything here is about one running process: which cache it serves, where its state lives, and how it is
started, shared and reported on. The binary itself is pinned in ../tools/tools.json as usual.
"""

import hashlib
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from util import cache_home, fail, object_table, resolved, write_if_changed

SHIM = "bazel-remote"
SHIM_STATE = "state.json"
# The shim indexes its whole cache directory before it listens.
SHIM_START_TIMEOUT = 60
# Long enough that consecutive builds during iteration don't reindex every time
SHIM_IDLE_TIMEOUT = "15m"

# The settings table this reads, which `tine` allows in a project's settings files.
SECTION = "cache"

# Room for a graph whose expensive outputs are small. The shim requires a size.
MAX_SIZE = 20

# What the shim itself may need, on top of every AWS_* variable: its own TLS trust and whatever
# proxy this network wants. Nothing else this command happens to be carrying.
PASSED = ("HOME", "PATH", "SSL_CERT_DIR", "SSL_CERT_FILE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")

# Below the ephemeral range in /proc/sys/net/ipv4/ip_local_port_range, so an outgoing connection's
# source port cannot be holding the one a shim is about to bind.
PORT_BASE = 20480
PORT_COUNT = 4096


@dataclass(frozen=True, slots=True)
class CacheSettings:
    endpoint: str
    bucket: str
    region: str
    auth_method: str
    write: bool
    # None for a bucket that serves unsigned requests; see `cache_shim.environment`
    key_file: Path | None
    dir: Path
    max_size: int
    port: int


def _digest(text: str) -> str:
    """A short stable name for a string, for a path or a record that has to key on one."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _cache_string(table: Mapping[str, object], key: str, default: str, source: str) -> str:
    """One string naming a host, a bucket, a region or an auth method, none of which hold whitespace."""
    value = table.get(key, default)
    if not isinstance(value, str) or not value:
        fail(f"[{SECTION}] {key} in {source} must be a non-empty string")
    if value != "".join(value.split()):
        fail(f"[{SECTION}] {key} in {source} must hold no whitespace")
    return value


def _cache_bool(table: Mapping[str, object], key: str, default: bool, source: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        fail(f"[{SECTION}] {key} in {source} must be true or false")
    return value


def _cache_number(table: Mapping[str, object], key: str, default: int, ceiling: int, source: str) -> int:
    # A TOML boolean is an int to isinstance, so it has to be refused before the range check.
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
        fail(f"[{SECTION}] {key} in {source} must be a whole number from 1 to {ceiling}")
    return value


def _cache_path(table: Mapping[str, object], key: str, source: str, *, follow: bool = False) -> Path | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        fail(f"[{SECTION}] {key} in {source} must be a non-empty path")
    path = Path(value).expanduser()
    return resolved(path) if follow else path


def settings(config: Mapping[str, object], source: str) -> CacheSettings | None:
    """The shared cache this project builds against, or None when nothing configures one."""
    table = object_table(config.get(SECTION, {}), f"[{SECTION}] in {source}")
    if not table or not _cache_bool(table, "enabled", True, source):
        return None
    allowed = {
        "auth_method",
        "bucket",
        "dir",
        "enabled",
        "endpoint",
        "key_file",
        "max_size",
        "port",
        "region",
        "write",
    }
    if extra := sorted(set(table) - allowed):
        fail(f"[{SECTION}] in {source} has unsupported keys: {', '.join(extra)}")

    endpoint = _cache_string(table, "endpoint", "", source)
    bucket = _cache_string(table, "bucket", "", source)

    auth_method = _cache_string(table, "auth_method", "access_key", source)
    write = _cache_bool(table, "write", False, source)
    key_file = _cache_path(table, "key_file", source)
    if write and key_file is None and auth_method == "access_key":
        fail(f"[{SECTION}] write in {source} needs a key_file")

    # Keyed by bucket for co-existing projects
    directory = _cache_path(table, "dir", source, follow=True) or resolved(
        cache_home() / SHIM / _digest(f"{endpoint}/{bucket}")
    )
    # Derived from the directory, so two checkouts configured alike agree on a port
    derived_port = PORT_BASE + int(_digest(str(directory))[:4], 16) % PORT_COUNT
    return CacheSettings(
        endpoint=endpoint,
        bucket=bucket,
        region=_cache_string(table, "region", "auto", source),
        auth_method=auth_method,
        write=write,
        key_file=key_file,
        dir=directory,
        max_size=_cache_number(table, "max_size", MAX_SIZE, 1 << 20, source),
        port=_cache_number(table, "port", derived_port, 65535, source),
    )


def state_dir(cache: CacheSettings) -> Path:
    """Where tine records the shim it started, keyed by the directory that shim caches into.

    Separate from the cache dir to not interfere with the shim. Not XDG_RUNTIME_DIR, tempting as a
    tmpfs that a reboot empties is: a session without one is ordinary (a container, a cron job), and
    two sessions of one user that disagree about this path disagree about the socket in it too, so
    each refuses to share a shim the other started. A record a reboot left behind is harmless, since
    one is only read while some process holds the lock on the cache directory.
    """
    return cache_home() / "tine" / "cache" / _digest(str(cache.dir))


def environment(cache: CacheSettings) -> dict[str, str]:
    # default to unsigned requests/public buckets (values have to be present)
    # env instead of argv to avoid exposing in /proc
    keys = {"ACCESS_KEY_ID": "unused", "SECRET_ACCESS_KEY": "unused", "SIGNATURE_TYPE": "anonymous"}
    if cache.key_file is not None:
        parts = cache.key_file.read_text(encoding="utf-8").split()
        if len(parts) != 2:
            fail(f"[{SECTION}] key_file {cache.key_file} must hold an access key id and a secret")
        keys = dict(zip(("ACCESS_KEY_ID", "SECRET_ACCESS_KEY"), parts, strict=True))
    environment = {f"BAZEL_REMOTE_S3_{key}": value for key, value in keys.items()}
    # AWS_* by prefix: the auth methods that find their own credentials read a whole family of
    # them, and a web identity or container role is nothing but those variables.
    for name, value in os.environ.items():
        if name.upper() in PASSED or name.startswith("AWS_"):
            environment[name] = value
    return environment


def socket_path(cache: CacheSettings) -> Path:
    """The shim's unix HTTP socket, for `tine cache-status`.

    bazel-remote insists on an HTTP listener and would otherwise default to TCP:8080.
    """
    return state_dir(cache) / "http.sock"


def log_path(cache: CacheSettings) -> Path:
    """Everything the shim printed since it last started."""
    return state_dir(cache) / "log"


def command(cache: CacheSettings, binary: Path) -> list[str]:
    """Build shim argv."""
    command = [
        str(binary),
        "--dir",
        str(cache.dir),
        "--max_size",
        str(cache.max_size),
        "--grpc_address",
        f"127.0.0.1:{cache.port}",
        "--http_address",
        f"unix://{socket_path(cache)}",
        "--idle_timeout",
        SHIM_IDLE_TIMEOUT,
        # Validating a result means asking the bucket about every file in its output tree, which is
        # tens of thousands of requests for one cache hit.
        "--disable_grpc_ac_deps_check",
        f"--s3.endpoint={cache.endpoint}",
        f"--s3.bucket={cache.bucket}",
        f"--s3.region={cache.region}",
        f"--s3.auth_method={cache.auth_method}",
    ]
    if not cache.write:
        # Tell the shim that it can't write to the bucket, to avoid upload error logs
        command += ["--num_uploaders=0"]
    return command


def identity(cache: CacheSettings, binary: Path) -> str:
    """What a running shim has to match for another checkout to share it.

    The key contributes its hash and never its value, so the recorded state holds no secret.
    """
    digest = hashlib.sha256()
    for part in command(cache, binary):
        digest.update(f"{part}\0".encode())
    for key, value in sorted(environment(cache).items()):
        if key.startswith("BAZEL_REMOTE_"):
            digest.update(f"{key}\0{value}\0".encode())
    return digest.hexdigest()[:16]


def _is_port_listening(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_listening(cache: CacheSettings) -> None:
    """Wait for a shim someone else started to listen."""
    import time

    started = time.monotonic()
    while not _is_port_listening(cache.port):
        if time.monotonic() - started > SHIM_START_TIMEOUT:
            fail(
                f"{SHIM} holding {cache.dir} has not served 127.0.0.1:{cache.port} in {SHIM_START_TIMEOUT}s"
            )
        time.sleep(0.1)


def _check_shareable(cache: CacheSettings, binary: Path) -> None:
    """A shim already holds this directory: either it serves this cache, or nothing here can.

    Refuse rather than adopt, because a shim holding a write key would upload this project's outputs
    under a credential it was never given. Refuse rather than replace, because killing it drops the
    uploads it has queued and takes the cache away from whoever started it.
    """
    import json

    try:
        running = json.loads((state_dir(cache) / SHIM_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail(
            f"[{SECTION}] something already holds {cache.dir} and tine did not start it; "
            "stop it, or configure a dir of this project's own"
        )
    if running.get("digest") == identity(cache, binary):
        # It may still be indexing: the checkout that started it is waiting on the same thing, and
        # handing over to Buck before it listens fails the build rather than delaying it.
        wait_listening(cache)
        return
    fail(
        f"[{SECTION}] {SHIM} holds {cache.dir} as pid {running.get('pid')}, for "
        f"{running.get('bucket')} at {running.get('endpoint')} through {running.get('binary')}; "
        f"kill it, or wait for the {SHIM_IDLE_TIMEOUT} idle timeout"
    )


def start(cache: CacheSettings, binary: Path, lock: int) -> None:
    """Start a shim holding `lock`, and wait until it serves or says why it will not."""
    import json
    import subprocess
    import time

    state = state_dir(cache)
    # TOCTOU, just for a nicer error message: on a race, the shim will fail with its own bind error
    if _is_port_listening(cache.port):
        fail(f"[{SECTION}] 127.0.0.1:{cache.port} is already in use")

    # Private! we hand credentials to the shim, it can write to the bucket without further auth
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = log_path(cache)
    record: dict[str, object] = {
        "binary": str(binary),
        "bucket": cache.bucket,
        "digest": identity(cache, binary),
        "dir": str(cache.dir),
        "endpoint": cache.endpoint,
        "port": cache.port,
        "write": cache.write,
    }
    # Written before spawning, because a concurrent invocation that loses the lock reads it to
    # decide whether it may share this shim, and no record at all reads as somebody else's process.
    write_if_changed(state / SHIM_STATE, json.dumps(record, indent=2, sort_keys=True) + "\n")
    # Clean up a stale socket after a killed shim. This holds the lock, thus safe now
    socket_path(cache).unlink(missing_ok=True)
    with log.open("wb") as out:
        process = subprocess.Popen(
            command(cache, binary),
            cwd=cache.dir,
            env=environment(cache),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            # shim holds the lock for its lifetime
            pass_fds=(lock,),
            # guard against closing terminal
            start_new_session=True,
        )

    # A second write, since the pid exists only now. It labels messages and nothing decides on it,
    # so a shim that dies between the two writes leaves nothing worse than a record without a pid.
    record["pid"] = process.pid
    write_if_changed(state / SHIM_STATE, json.dumps(record, indent=2, sort_keys=True) + "\n")

    started = time.monotonic()
    announced = False
    while not _is_port_listening(cache.port):
        if (code := process.poll()) is not None:
            tail = " ".join(log.read_text(errors="replace").split()[-40:])
            fail(f"{SHIM} exited with {code} instead of serving {cache.dir}: {tail}")
        waited = time.monotonic() - started
        if waited > SHIM_START_TIMEOUT:
            # It holds the lock, and holding it without serving would refuse every later command.
            # Kernel releases the lock when the process is gone.
            process.kill()
            process.wait()
            fail(f"{SHIM} did not serve 127.0.0.1:{cache.port} after {SHIM_START_TIMEOUT}s; see {log}")
        # Only once the wait is long enough to be mistaken for a hang
        if not announced and waited > 1:
            announced = True
            print(f"tine: waiting for {SHIM} to index {cache.dir}; see {log}", file=sys.stderr)
        time.sleep(0.1)


def _holds(path: Path) -> dict[str, object]:
    """What the shim says it holds, asked over its own socket rather than read from the record."""
    import json
    import socket

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        connection.sendall(b"GET /status HTTP/1.0\r\nHost: shim\r\n\r\n")
        received = b""
        while chunk := connection.recv(1 << 16):
            received += chunk
    return cast(dict[str, object], json.loads(received.partition(b"\r\n\r\n")[2]))


def _mode(write: bool) -> str:
    return "read-write" if write else "read-only"


def status(cache: CacheSettings) -> None:
    """Report what serves this cache, and what it holds."""
    import json

    print(f"endpoint  {cache.endpoint}")
    print(f"bucket    {cache.bucket}")
    print(f"dir       {cache.dir}")
    print(f"address   127.0.0.1:{cache.port}")
    print(f"log       {log_path(cache)}")
    if not _is_port_listening(cache.port):
        print(f"shim      not running; the next build starts one {_mode(cache.write)}")
        return
    try:
        held = _holds(socket_path(cache))
        running = json.loads((state_dir(cache) / SHIM_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as error:
        # Something serves the address, and what is missing is the socket and record tine's own shim
        # leaves in the state dir: either tine did not start what listens there, or these files
        # outlived the shim that wrote them.
        print(f"shim      serving, but not one tine started: {error}")
        return
    # The mode the shim was started with, not the one configured here: whoever changed the settings
    # since gets that one only after this shim is gone.
    print(f"shim      serving {_mode(running['write'])} as pid {running['pid']}")
    print(f"local     {held['NumFiles']} files, {held['CurrSize']} bytes compressed")


def ensure(cache: CacheSettings, binary: Path) -> None:
    """Serve this cache locally, starting a shim unless one already serves it.

    The lock is the cache directory itself, held by the shim through a descriptor it inherits. A
    flock belongs to the open file description rather than to a process, so the kernel drops it
    however the shim dies and liveness never rests on a pid this would have to trust. The directory
    rather than a file in it, because there is then nothing to create and nothing to leave behind.
    """
    import errno
    import fcntl

    cache.dir.mkdir(parents=True, exist_ok=True)
    lock = os.open(cache.dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno != errno.EWOULDBLOCK:
            raise
        os.close(lock)
        _check_shareable(cache, binary)
        return
    try:
        start(cache, binary, lock)
    finally:
        os.close(lock)
