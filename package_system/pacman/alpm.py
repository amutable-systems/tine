"""Read and write alpm's formats, and resolve through alpm's library.

Both halves are here because they describe one package system and share one `Package`.

The library resolves: `Alpm` binds libalpm through ctypes, the way `kmod.py` binds libkmod, so
resolution, provider selection and version comparison are alpm's own and none of them are
reimplemented. The formats are read and written without it, which is not a duplicate of what the
library does but a consequence of where it runs: the snapshot and extract drivers are host tools,
and the host contract is a pinned Buck and a pinned Python, so a driver that runs there cannot
ask for a shared library. Everything the library is used for happens inside a box.
"""

import ctypes
import gzip
import io
import tarfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import IO, NamedTuple, NoReturn, Self, cast

import util

# A tar under whichever compressor its era used. The build graph names a selected package `.zst`;
# a repository may still serve an older one, which only has to be told apart from a signature.
PACKAGE_STEM = ".pkg.tar"
_COMPRESSORS = ("", ".zst", ".xz", ".gz", ".bz2")

# Capabilities a solve is told are already provided. `linux` requires an initramfs generator, but
# tine builds the initrd itself and the disk it ships carries no /boot, so letting one be chosen
# installs a generator whose install hook then writes an initramfs the image discards.
ASSUME_INSTALLED = ("initramfs",)

DBPATH = "var/lib/pacman"
LOCAL_DB = f"{DBPATH}/local"


def is_package(name: str) -> bool:
    """Whether a file name is a package rather than, say, its detached signature."""
    stem, _, compressor = name.rpartition(PACKAGE_STEM)
    return bool(stem) and compressor in _COMPRESSORS


class Package(NamedTuple):
    """One package, as a repository database describes it or as libalpm resolves it."""

    name: str
    version: str
    repo: str
    filename: str = ""
    sha256: str = ""
    size: int = 0

    @property
    def id(self) -> str:
        return f"{self.name}-{self.version}"


def local_href(position: int, name: str) -> str:
    """Name a locally built package after the input directory it was published from.

    alpm refuses a `%FILENAME%` containing a separator, so unlike rpm's repodata the directory
    index cannot be a path component here. `local_location` turns it back into one.
    """
    return f"{position}-{name}"


def local_location(href: str, what: str) -> str:
    """Recover the `<directory>/<file>` location a locally built package is recorded under."""
    directory, separator, name = href.partition("-")
    if not separator or not directory.isdigit():
        util.fail(f"{what}: {href!r} was not published by this package system's indexer")
    return f"{directory}/{name}"


def parse_desc(text: str) -> dict[str, list[str]]:
    """Read one `%KEY%`-delimited database entry into its lists of values."""
    entry: dict[str, list[str]] = {}
    values: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if len(line) > 2 and line.startswith("%") and line.endswith("%"):
            values = entry.setdefault(line[1:-1], [])
        elif line:
            values.append(line)
    return entry


def package_from_desc(entry: dict[str, list[str]], repo: str, what: str) -> Package:
    """Build a package from one parsed database entry, naming `what` in any complaint."""

    def one(key: str, default: str | None = None) -> str:
        values = entry.get(key, [])
        if not values and default is not None:
            return default
        if len(values) != 1:
            util.fail(f"{what}: entry has {len(values)} %{key}% values, expected one")
        return values[0]

    size = one("CSIZE", "0")
    if not size.isdigit():
        util.fail(f"{what}: entry has invalid %CSIZE% {size!r}")
    return Package(
        name=one("NAME"),
        version=one("VERSION"),
        repo=repo,
        filename=one("FILENAME", ""),
        sha256=one("SHA256SUM", "").lower(),
        size=int(size),
    )


def read_db(path: Path, repo: str) -> list[Package]:
    """Read every package a repository database describes."""
    packages = []
    with tarfile.open(path, mode="r:gz") as db:
        for member in db:
            if not member.isfile() or Path(member.name).name != "desc":
                continue
            source = db.extractfile(member)
            if source is None:
                continue
            entry = parse_desc(source.read().decode("utf-8"))
            packages.append(package_from_desc(entry, repo, f"{path.name}:{member.name}"))
    packages.sort()
    return packages


def open_package(stack: ExitStack, package: Path) -> tarfile.TarFile:
    """Open a package's tar, whatever it was compressed with."""
    raw = stack.enter_context(package.open("rb"))
    magic = raw.read(util.MAGIC)
    raw.seek(0)
    open_compressed = util.decompressor(magic)
    # An alpm package is a tar under whichever compressor its era used, or none at all.
    stream: io.BufferedIOBase = raw if open_compressed is None else stack.enter_context(open_compressed(raw))
    # A decompressor is a file object that typeshed does not declare as one. `r|` reads the tar
    # as a stream, so what it actually needs of `stream` is `read`.
    return stack.enter_context(tarfile.open(fileobj=cast(IO[bytes], stream), mode="r|"))


def write_db(entries: list[tuple[str, dict[str, list[str]]]], out: Path, epoch: int) -> None:
    """Write a repository database whose bytes depend only on the entries it carries."""
    with out.open("wb") as raw:
        # gzip stores an mtime and the source name of its own; pin the first and omit the second.
        with gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as db:
                names = [name for name, _ in entries]
                if len(set(names)) != len(names):
                    util.fail(f"write_db: {out} would carry a package twice")
                for name, entry in sorted(entries, key=lambda entry: entry[0]):
                    directory = tarfile.TarInfo(name)
                    directory.type = tarfile.DIRTYPE
                    directory.mode = 0o755
                    directory.mtime = epoch
                    db.addfile(directory)

                    text = "".join(
                        f"%{key}%\n" + "".join(f"{value}\n" for value in values) + "\n"
                        for key, values in entry.items()
                        if values
                    )
                    body = text.encode("utf-8")
                    desc = tarfile.TarInfo(f"{name}/desc")
                    desc.size = len(body)
                    desc.mode = 0o644
                    desc.mtime = epoch
                    db.addfile(desc, io.BytesIO(body))


SONAME = "libalpm.so.16"

# alpm_question_type_t's provider question, and the transaction's default flags.
_SELECT_PROVIDER = 1 << 5
_NO_FLAGS = 0


class _List(ctypes.Structure):
    """alpm_list_t, whose nodes are what every libalpm collection is made of."""


_List._fields_ = [
    ("data", ctypes.c_void_p),
    ("prev", ctypes.POINTER(_List)),
    ("next", ctypes.POINTER(_List)),
]
_LIST = ctypes.POINTER(_List)


class _SelectProvider(ctypes.Structure):
    """The prefix of alpm_question_select_provider_t this reads and answers."""

    _fields_ = [
        ("type", ctypes.c_int),
        ("use_index", ctypes.c_int),
        ("providers", _LIST),
        ("depend", ctypes.c_void_p),
    ]


class _DepMissing(ctypes.Structure):
    """alpm_depmissing_t, which a failed prepare returns one of per unsatisfied dependency."""

    _fields_ = [
        ("target", ctypes.c_char_p),
        ("depend", ctypes.c_void_p),
        ("causingpkg", ctypes.c_char_p),
    ]


_QUESTION = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)


def _load() -> ctypes.CDLL:
    try:
        lib = ctypes.CDLL(SONAME, use_errno=True)
    except OSError as error:
        util.fail(f"plan: resolving alpm packages needs {SONAME}: {error}")

    ptr, text, cint = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
    for name, restype, argtypes in (
        ("alpm_initialize", ptr, (text, text, ctypes.POINTER(cint))),
        ("alpm_release", cint, (ptr,)),
        ("alpm_errno", cint, (ptr,)),
        ("alpm_strerror", text, (cint,)),
        ("alpm_option_add_architecture", cint, (ptr, text)),
        ("alpm_option_set_questioncb", cint, (ptr, _QUESTION, ptr)),
        ("alpm_register_syncdb", ptr, (ptr, text, cint)),
        ("alpm_get_syncdbs", _LIST, (ptr,)),
        ("alpm_get_localdb", ptr, (ptr,)),
        ("alpm_db_get_name", text, (ptr,)),
        ("alpm_db_get_pkg", ptr, (ptr, text)),
        ("alpm_find_dbs_satisfier", ptr, (ptr, _LIST, text)),
        ("alpm_find_group_pkgs", _LIST, (_LIST, text)),
        ("alpm_add_pkg", cint, (ptr, ptr)),
        ("alpm_trans_init", cint, (ptr, cint)),
        ("alpm_trans_prepare", cint, (ptr, ctypes.POINTER(_LIST))),
        ("alpm_trans_release", cint, (ptr,)),
        ("alpm_trans_get_add", _LIST, (ptr,)),
        ("alpm_trans_get_remove", _LIST, (ptr,)),
        ("alpm_pkg_get_name", text, (ptr,)),
        ("alpm_pkg_get_version", text, (ptr,)),
        ("alpm_pkg_get_db", ptr, (ptr,)),
        ("alpm_pkg_get_filename", text, (ptr,)),
        ("alpm_pkg_get_sha256sum", text, (ptr,)),
        ("alpm_pkg_get_size", ctypes.c_int64, (ptr,)),
        ("alpm_pkg_vercmp", cint, (text, text)),
        ("alpm_dep_compute_string", ptr, (ptr,)),
        ("alpm_dep_from_string", ptr, (text,)),
        ("alpm_option_add_assumeinstalled", cint, (ptr, ptr)),
    ):
        function = getattr(lib, name)
        function.restype = restype
        function.argtypes = argtypes
    return lib


class Ambiguous(NamedTuple):
    """A capability more than one package provides, which nobody is here to choose between."""

    providers: tuple[str, ...]


class Alpm:
    """libalpm scoped to one root and the databases staged for it."""

    def __init__(self, root: Path, dbpath: Path, arch: str) -> None:
        self._lib = _load()
        (dbpath / "local").mkdir(parents=True, exist_ok=True)
        error = ctypes.c_int(0)
        handle = self._lib.alpm_initialize(
            str(root).encode(),
            str(dbpath).encode(),
            ctypes.byref(error),
        )
        if not handle:
            util.fail(f"plan: {self._lib.alpm_strerror(error.value).decode()}")
        self._handle = handle
        # Without this libalpm takes the architecture from the build host's uname.
        self._lib.alpm_option_add_architecture(self._handle, arch.encode())
        for capability in ASSUME_INSTALLED:
            depend = self._lib.alpm_dep_from_string(capability.encode())
            if not depend or self._lib.alpm_option_add_assumeinstalled(self._handle, depend) != 0:
                self._fail(f"assuming {capability} installed")

        self.ambiguous: list[Ambiguous] = []
        # Held on self: ctypes would otherwise collect the thunk libalpm still points at.
        self._question = _QUESTION(self._on_question)
        self._lib.alpm_option_set_questioncb(self._handle, self._question, None)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> None:
        self._lib.alpm_release(self._handle)

    def _fail(self, what: str) -> NoReturn:
        util.fail(f"plan: {what}: {self._lib.alpm_strerror(self._lib.alpm_errno(self._handle)).decode()}")

    def _each(self, head: ctypes._Pointer[_List]) -> Iterator[ctypes.c_void_p]:
        node = head
        while node:
            yield cast(ctypes.c_void_p, node.contents.data)
            node = node.contents.next

    def _on_question(self, _context: ctypes.c_void_p, question: ctypes.c_void_p) -> None:
        # Answering is not optional: libalpm needs an index to carry on with. Record the choice
        # and let `resolve` refuse afterwards, rather than raising back through C.
        kind = ctypes.cast(question, ctypes.POINTER(ctypes.c_int)).contents.value
        if kind != _SELECT_PROVIDER:
            return
        select = ctypes.cast(question, ctypes.POINTER(_SelectProvider)).contents
        providers = sorted(self._name(package) for package in self._each(select.providers))
        self.ambiguous.append(Ambiguous(tuple(providers)))
        select.use_index = 0

    def _name(self, package: ctypes.c_void_p) -> str:
        return cast(str, self._lib.alpm_pkg_get_name(package).decode())

    def register(self, name: str) -> None:
        """Make one staged database available to resolve against, in priority order."""
        # The databases are pinned inputs whose bytes are already checksummed, and no key
        # material exists here, so no signature level is requested.
        if not self._lib.alpm_register_syncdb(self._handle, name.encode(), 0):
            self._fail(f"cannot register {name}")

    def _needed(self, package: ctypes.c_void_p) -> bool:
        """Whether the root is missing this package, or carries something older."""
        local = self._lib.alpm_db_get_pkg(
            self._lib.alpm_get_localdb(self._handle),
            self._lib.alpm_pkg_get_name(package),
        )
        if not local:
            return True
        installed = self._lib.alpm_pkg_get_version(local)
        older = self._lib.alpm_pkg_vercmp(installed, self._lib.alpm_pkg_get_version(package))
        return cast(int, older) < 0

    def _target(self, spec: str) -> list[ctypes.c_void_p]:
        """The packages one install spec names, whether it names a package or a group."""
        databases = self._lib.alpm_get_syncdbs(self._handle)
        package = self._lib.alpm_find_dbs_satisfier(self._handle, databases, spec.encode())
        if package:
            return [package]
        members = list(self._each(self._lib.alpm_find_group_pkgs(databases, spec.encode())))
        if not members:
            util.fail(f"plan: nothing provides {spec!r}")
        return members

    def _unsatisfied(self, missing: ctypes._Pointer[_List]) -> list[str]:
        """Name what a failed prepare could not satisfy, and for whom."""
        reasons = []
        for entry in self._each(missing):
            record = ctypes.cast(entry, ctypes.POINTER(_DepMissing)).contents
            wanted = ctypes.cast(self._lib.alpm_dep_compute_string(record.depend), ctypes.c_char_p)
            target = record.target.decode() if record.target else "?"
            reasons.append(f"{target} requires {wanted.value.decode() if wanted.value else '?'}")
        return reasons

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._lib.alpm_trans_init(self._handle, _NO_FLAGS) != 0:
            self._fail("cannot start a transaction")
        try:
            yield
        finally:
            self._lib.alpm_trans_release(self._handle)

    def resolve(self, install: list[str]) -> list[Package]:
        """The packages that installing `install` into this root adds."""
        with self._transaction():
            for spec in install:
                for package in self._target(spec):
                    # An install spec a lower layer already carries adds nothing.
                    if self._needed(package) and self._lib.alpm_add_pkg(self._handle, package) != 0:
                        self._fail(f"cannot add {self._name(package)}")

            missing = _LIST()
            if self._lib.alpm_trans_prepare(self._handle, ctypes.byref(missing)) != 0:
                reasons = self._unsatisfied(missing)
                self._fail("\n  ".join(["cannot resolve", *reasons]) if reasons else "cannot resolve")

            if self.ambiguous:
                util.fail(
                    "plan: "
                    + "; ".join(", ".join(entry.providers) for entry in self.ambiguous)
                    + " each provide a required capability; name the one you want among the "
                    "install specs"
                )

            removals = [
                self._name(package) for package in self._each(self._lib.alpm_trans_get_remove(self._handle))
            ]
            if removals:
                # A transaction is a set of packages to add. Nothing downstream can express a
                # removal, so refuse rather than hand on a closure that silently drops one.
                util.fail(f"plan: installing this would remove {', '.join(sorted(removals))}")

            return [
                self._package(package) for package in self._each(self._lib.alpm_trans_get_add(self._handle))
            ]

    def _package(self, package: ctypes.c_void_p) -> Package:
        checksum = self._lib.alpm_pkg_get_sha256sum(package)
        filename = self._lib.alpm_pkg_get_filename(package)
        name = self._name(package)
        if not checksum or not filename:
            util.fail(f"plan: {name} has no checksum or file name in its repository")
        return Package(
            name=name,
            version=self._lib.alpm_pkg_get_version(package).decode(),
            repo=self._lib.alpm_db_get_name(self._lib.alpm_pkg_get_db(package)).decode(),
            filename=filename.decode(),
            sha256=checksum.decode(),
            size=self._lib.alpm_pkg_get_size(package),
        )
