"""Something to ask a running shim.

The shim is started in the background by whichever `tine buck` first needs it, so a later command
holds no handle to the process. Before starting another, the launcher has to know whether one already
serves the store, and `tine cache-status` wants its counters; a log can say that a shim started, not
that it is still there. So a running shim answers JSON over a unix socket, which needs no port and no
address to agree on.

The socket is named after the store rather than placed inside it. A unix socket path has about a
hundred bytes to spend, which a store under a cache directory several levels down can exceed on its
own, so a caller says which store it means and both sides derive the same short name. It lives in
the runtime directory when there is one, because that is per-user, private, and cleaned up on
logout.

The socket existing and accepting is itself the liveness answer: it goes away with the process.
"""

import hashlib
import http.server
import json
import logging
import os
import socket
import socketserver
import struct
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, override

log = logging.getLogger("status")

Report = Callable[[], dict[str, Any]]
Attend = Callable[[int], None]

# Where a caller says "this process is building": `POST /clients/<pid>`, answered with the report
# like any other request, so starting a build is one round trip.
CLIENTS = "/clients/"


def socket_path(root: Path) -> Path:
    """Where the shim serving `root` listens.

    Both sides compute this, so nothing has to pass a socket path around, and it stays short
    however deep the store itself is. It is never in a shared directory: another user could bind
    the name there first, and then answer `ask` with whatever they liked.
    """
    named = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / f"tine-cache-{named}.sock"
    private = Path(tempfile.gettempdir()) / f"tine-cache-{os.getuid()}"
    private.mkdir(mode=0o700, exist_ok=True)
    if private.stat().st_uid != os.getuid():
        raise OSError(f"{private} belongs to someone else")
    return private / f"{named}.sock"


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answers any GET with the whole report. There is only one thing to ask.

    A POST to `CLIENTS` says a build is starting in that process, and is answered with the same
    report: whoever registers also wants to know whether this shim is the one they meant.
    """

    protocol_version = "HTTP/1.1"

    # Not an override: http.server dispatches on the method name rather than declaring one.
    def do_GET(self) -> None:  # noqa: N802  (the name is http.server's, not ours)
        self._report()

    def do_POST(self) -> None:  # noqa: N802
        server = self.server
        assert isinstance(server, _Server)
        pid = self.path.removeprefix(CLIENTS)
        if not self.path.startswith(CLIENTS) or not pid.isdigit():
            self.send_error(404, "no such thing to post to")
            return
        server.attend(int(pid))
        self._report()

    def _report(self) -> None:
        server = self.server
        assert isinstance(server, _Server)
        body = json.dumps(server.report(), indent=2, sort_keys=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @override
    def address_string(self) -> str:
        """A unix peer has no address, and the base class would go looking for one."""
        return "local"

    @override
    def log_message(self, format: str, *args: object) -> None:
        log.debug(format, *args)


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, report: Report, attend: Attend) -> None:
        self.report = report
        self.attend = attend
        super().__init__(path, _Handler)


@contextmanager
def serving(root: Path, report: Report, attend: Attend = lambda pid: None) -> Iterator[Path]:
    """A socket answering for `root` for as long as the block runs.

    A socket file left behind by a killed shim would refuse the next bind, so it is removed first.
    That is safe because the store directory is already held exclusively: getting this far means
    nothing else is serving it.
    """
    path = socket_path(root)
    path.unlink(missing_ok=True)
    server = _Server(str(path), report, attend)
    # Nothing secret in a report, but there is no reason for anyone else to read it either, and
    # the fallback location is shared.
    path.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield path
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        path.unlink(missing_ok=True)


def ask(root: Path, timeout: float = 5, client: int | None = None) -> dict[str, Any] | None:
    """What the shim serving `root` says about itself, or None if nothing is.

    Here rather than in a caller because the wire format is this module's business, and because
    "nothing is serving it" has to be an answer rather than an exception.

    `client` is a process about to build, which the shim must outlive.
    """
    path = socket_path(root)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(path))
    except OSError:
        # No socket, or one left behind by a shim that is gone. Both mean the same thing.
        connection.close()
        return None
    with connection, connection.makefile("rwb") as stream:
        # Whoever answers has to be us: the directory is private, but a report that a build tool
        # may one day act on is worth one more check than a directory mode.
        _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            raise OSError(f"{path} is served by uid {uid}, not us")
        request = "GET /" if client is None else f"POST {CLIENTS}{client}"
        stream.write(f"{request} HTTP/1.0\r\n\r\n".encode())
        stream.flush()
        _, _, body = stream.read().partition(b"\r\n\r\n")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError(f"status is {type(parsed).__name__}, not an object")
    return parsed
