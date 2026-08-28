"""Enter Linux namespaces and assemble filesystems for Tine sandboxes."""

import ctypes
import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

AT_EMPTY_PATH = 0x1000
AT_FDCWD = -100
AT_NO_AUTOMOUNT = 0x800
AT_RECURSIVE = 0x8000
AT_SYMLINK_NOFOLLOW = 0x100
CAP_CHOWN = 0
CAP_DAC_OVERRIDE = 1
CAP_DAC_READ_SEARCH = 2
CAP_FOWNER = 3
CAP_FSETID = 4
CAP_SETGID = 6
CAP_SETUID = 7
CAP_SETPCAP = 8
CAP_NET_BIND_SERVICE = 10
CAP_NET_ADMIN = 12
CAP_SYS_CHROOT = 18
CAP_SYS_PTRACE = 19
CAP_SYS_ADMIN = 21
CAP_SYS_RESOURCE = 24
CAP_SETFCAP = 31
CLONE_NEWNET = 0x40000000
CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
LINUX_CAPABILITY_U32S_3 = 2
LINUX_CAPABILITY_VERSION_3 = 0x20080522
MNT_DETACH = 2
MOUNT_ATTR_RDONLY = 0x00000001
MOUNT_ATTR_SIZE_VER0 = 32
MOVE_MOUNT_F_EMPTY_PATH = 0x00000004
MS_BIND = 4096
MS_MOVE = 8192
MS_REC = 16384
MS_SLAVE = 1 << 19
OPEN_TREE_CLONE = 1
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_IS_SET = 1
PR_CAP_AMBIENT_RAISE = 2
PR_CAP_AMBIENT_LOWER = 3
PR_CAPBSET_DROP = 24
PR_SET_DUMPABLE = 4
SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO = 0x00050000
SCMP_ARCH_X86 = 0x40000003
SCMP_ARCH_X86_64 = 0xC000003E
SCMP_ARCH_X32 = 0x4000003E
SCMP_ARCH_ARM = 0x40000028
SCMP_ARCH_AARCH64 = 0xC00000B7
SCMP_ARCH_PPC = 0x14
SCMP_ARCH_PPC64 = 0x80000015
SCMP_ARCH_PPC64LE = 0xC0000015
SCMP_ARCH_S390 = 0x16
SCMP_ARCH_S390X = 0x80000016
SCMP_FLTATR_ACT_BADARCH = 2
SCMP_FLTATR_API_SYSRAWRC = 9

_NR_OPEN_TREE = 428
_NR_MOVE_MOUNT = 429
_NR_MOUNT_SETATTR = 442
_OPEN_TREE_CLOEXEC = os.O_CLOEXEC
_ROOT = Path("/")
_COMPAT_ARCHITECTURES = {
    SCMP_ARCH_X86_64: (SCMP_ARCH_X86, SCMP_ARCH_X32),
    SCMP_ARCH_AARCH64: (SCMP_ARCH_ARM,),
    SCMP_ARCH_PPC64: (SCMP_ARCH_PPC,),
    SCMP_ARCH_PPC64LE: (SCMP_ARCH_PPC,),
    SCMP_ARCH_S390X: (SCMP_ARCH_S390,),
}
_KEPT_CAPABILITIES = (
    CAP_CHOWN,
    CAP_DAC_OVERRIDE,
    CAP_DAC_READ_SEARCH,
    CAP_FOWNER,
    CAP_FSETID,
    CAP_SETGID,
    CAP_SETUID,
    CAP_SETPCAP,
    CAP_SYS_CHROOT,
    CAP_SYS_PTRACE,
    CAP_SYS_ADMIN,
    CAP_SYS_RESOURCE,
    CAP_SETFCAP,
)
_CHOWN_SYSCALLS = (
    b"chown",
    b"chown32",
    b"fchown",
    b"fchown32",
    b"fchownat",
    b"lchown",
    b"lchown32",
)
_SYNC_SYSCALLS = (
    b"fdatasync",
    b"fsync",
    b"msync",
    b"sync",
    b"sync_file_range",
    b"sync_file_range2",
    b"syncfs",
)


class _CapabilityHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapabilityData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


class _MountAttributes(ctypes.Structure):
    _fields_ = [
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    ]


_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.capget.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
_LIBC.capset.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
_LIBC.mount.argtypes = (
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_ulong,
    ctypes.c_char_p,
)
_LIBC.pivot_root.argtypes = (ctypes.c_char_p, ctypes.c_char_p)
_LIBC.prctl.argtypes = (
    ctypes.c_int,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
)
_LIBC.umount2.argtypes = (ctypes.c_char_p, ctypes.c_int)
_LIBC.unshare.argtypes = (ctypes.c_int,)
_LIBC.syscall.restype = ctypes.c_long


class SandboxOSError(OSError):
    def __init__(self, number: int, message: str) -> None:
        super().__init__(number, os.strerror(number))
        self.message = message


def _error(call: str, path: Path | None = None, number: int = 0) -> None:
    number = number or ctypes.get_errno()

    raise OSError(number, f"{call}: {os.strerror(number)}", path)


def mount(
    source: Path | None,
    target: Path,
    filesystem: str | None = None,
    flags: int = 0,
    options: str | None = None,
) -> None:
    if (
        _LIBC.mount(
            None if source is None else os.fsencode(source),
            os.fsencode(target),
            None if filesystem is None else os.fsencode(filesystem),
            flags,
            None if options is None else os.fsencode(options),
        )
        < 0
    ):
        _error("mount", target)


def umount2(path: Path, flags: int = 0) -> None:
    if _LIBC.umount2(os.fsencode(path), flags) < 0:
        _error("umount2", path)


def unshare(flags: int) -> None:
    if _LIBC.unshare(flags) < 0:
        _error("unshare")


def _prctl(option: int, argument: int) -> int:
    result = _LIBC.prctl(option, argument, 0, 0, 0)

    if result < 0:
        _error("prctl")

    return result


def _has_capability(capability: int) -> bool:
    header = _CapabilityHeader(LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapabilityData * LINUX_CAPABILITY_U32S_3)()

    if _LIBC.capget(ctypes.addressof(header), ctypes.addressof(data)) < 0:
        _error("capget")

    effective = (data[1].effective << 32) | data[0].effective

    return bool(effective & (1 << capability))


def _capability_mask(capabilities: tuple[int, ...]) -> int:
    return sum(1 << capability for capability in capabilities)


def fix_user_namespace_capabilities(*, network: bool) -> None:
    """Keep the sandbox's mount capabilities across exec."""

    capabilities = _KEPT_CAPABILITIES
    if network:
        capabilities += (CAP_NET_BIND_SERVICE, CAP_NET_ADMIN)

    header = _CapabilityHeader(LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapabilityData * LINUX_CAPABILITY_U32S_3)()
    if _LIBC.capget(ctypes.addressof(header), ctypes.addressof(data)) < 0:
        _error("capget")

    permitted = ((data[1].permitted << 32) | data[0].permitted) & _capability_mask(capabilities)
    last = int(Path("/proc/sys/kernel/cap_last_cap").read_text(encoding="ascii"))

    for capability in range(last + 1):
        if not permitted & (1 << capability):
            _prctl(PR_CAPBSET_DROP, capability)

    for index in range(LINUX_CAPABILITY_U32S_3):
        word = (permitted >> (index * 32)) & 0xFFFFFFFF
        data[index].permitted = word
        data[index].effective = word
        data[index].inheritable = word

    if _LIBC.capset(ctypes.addressof(header), ctypes.addressof(data)) < 0:
        _error("capset")

    for capability in range(last + 1):
        if permitted & (1 << capability):
            _ambient_capability_raise(capability)
        elif _ambient_capability_is_set(capability):
            _ambient_capability_lower(capability)


def _ambient_capability_is_set(capability: int) -> bool:
    result = _LIBC.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_IS_SET, capability, 0, 0)

    if result < 0:
        _error("prctl")

    return bool(result)


def _ambient_capability_raise(capability: int) -> None:
    if _LIBC.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_RAISE, capability, 0, 0) < 0:
        _error("prctl")


def _ambient_capability_lower(capability: int) -> None:
    if _LIBC.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_LOWER, capability, 0, 0) < 0:
        _error("prctl")


def unprivileged_user_namespace(*, become_root: bool) -> None:
    """Map only the caller into a fresh user namespace."""

    _prctl(PR_SET_DUMPABLE, 1)

    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    parent = os.getpid()
    pid = os.fork()
    if pid == 0:
        os.close(write_fd)

        try:
            os.read(read_fd, 1)
            Path(f"/proc/{parent}/setgroups").write_text("deny\n", encoding="ascii")

            gid = os.getgid()
            uid = os.getuid()
            mapped_gid = 0 if become_root else gid
            mapped_uid = 0 if become_root else uid

            Path(f"/proc/{parent}/gid_map").write_text(f"{mapped_gid} {gid} 1\n", encoding="ascii")
            Path(f"/proc/{parent}/uid_map").write_text(f"{mapped_uid} {uid} 1\n", encoding="ascii")
        except OSError as error:
            os._exit(error.errno or 1)
        except BaseException:
            os._exit(1)

        os._exit(0)

    os.close(read_fd)

    namespace_error: OSError | None = None
    try:
        unshare(CLONE_NEWUSER)
    except OSError as error:
        namespace_error = error

    try:
        os.write(write_fd, b"1")
    finally:
        os.close(write_fd)

    result = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])

    if namespace_error is not None:
        if namespace_error.errno == errno.EPERM:
            raise SandboxOSError(
                errno.EPERM,
                "tine cannot create an unprivileged user namespace; check the host's user-namespace policy",
            ) from namespace_error
        raise namespace_error

    if result:
        raise OSError(result, os.strerror(result))


def _single_user_namespace() -> bool:
    try:
        lines = Path("/proc/self/uid_map").read_text(encoding="ascii").splitlines()
    except FileNotFoundError:
        return False

    return len(lines) == 1 and int(lines[0].split()[-1]) == 1


def _acquire_privileges(*, become_root: bool, network: bool) -> bool:
    if _has_capability(CAP_SYS_ADMIN) and (not become_root or (os.getuid() == 0 and os.getgid() == 0)):
        return False

    unprivileged_user_namespace(become_root=become_root)
    fix_user_namespace_capabilities(network=network)

    return True


def _suppress_syscalls(*, chown: bool, sync: bool) -> None:
    if not chown and not sync:
        return

    library = ctypes.CDLL("libseccomp.so.2")

    library.seccomp_init.argtypes = (ctypes.c_uint32,)
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = (ctypes.c_void_p,)
    library.seccomp_attr_set.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32)
    library.seccomp_arch_native.argtypes = ()
    library.seccomp_arch_native.restype = ctypes.c_uint32
    library.seccomp_arch_add.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    library.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint)
    library.seccomp_load.argtypes = (ctypes.c_void_p,)

    context = library.seccomp_init(SCMP_ACT_ALLOW)
    if not context:
        raise OSError(errno.ENOMEM, os.strerror(errno.ENOMEM))

    try:
        result = library.seccomp_attr_set(context, SCMP_FLTATR_API_SYSRAWRC, 1)
        if result < 0:
            _error("seccomp_attr_set", number=-result)

        for architecture in _COMPAT_ARCHITECTURES.get(library.seccomp_arch_native(), ()):
            result = library.seccomp_arch_add(context, architecture)
            if result < 0 and result not in (-errno.EEXIST, -errno.EDOM):
                _error("seccomp_arch_add", number=-result)

        result = library.seccomp_attr_set(context, SCMP_FLTATR_ACT_BADARCH, SCMP_ACT_ALLOW)
        if result < 0:
            _error("seccomp_attr_set", number=-result)

        syscalls = (_CHOWN_SYSCALLS if chown else ()) + (_SYNC_SYSCALLS if sync else ())
        for syscall in syscalls:
            number = library.seccomp_syscall_resolve_name(syscall)
            if number != -1:
                result = library.seccomp_rule_add(context, SCMP_ACT_ERRNO, number, 0)
                if result < 0:
                    _error("seccomp_rule_add", number=-result)

        result = library.seccomp_load(context)
        if result < 0:
            _error("seccomp_load", number=-result)
    finally:
        library.seccomp_release(context)


def _open_tree(path: Path, *, recursive: bool) -> int:
    flags = AT_NO_AUTOMOUNT | AT_SYMLINK_NOFOLLOW | OPEN_TREE_CLONE | _OPEN_TREE_CLOEXEC
    if recursive:
        flags |= AT_RECURSIVE

    try:
        function = _LIBC.open_tree
        function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        result = function(AT_FDCWD, os.fsencode(path), flags)
    except AttributeError:
        result = _LIBC.syscall(
            ctypes.c_long(_NR_OPEN_TREE),
            ctypes.c_int(AT_FDCWD),
            ctypes.c_char_p(os.fsencode(path)),
            ctypes.c_uint(flags),
        )

    if result < 0:
        _error("open_tree", path)

    return result


def _mount_setattr(fd: int, *, readonly: bool, recursive: bool) -> None:
    flags = AT_EMPTY_PATH | (AT_RECURSIVE if recursive else 0)
    attributes = _MountAttributes(attr_set=MOUNT_ATTR_RDONLY if readonly else 0)

    try:
        function = _LIBC.mount_setattr
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_size_t,
        )
        result = function(fd, b"", flags, ctypes.addressof(attributes), MOUNT_ATTR_SIZE_VER0)
    except AttributeError:
        result = _LIBC.syscall(
            ctypes.c_long(_NR_MOUNT_SETATTR),
            ctypes.c_int(fd),
            ctypes.c_char_p(b""),
            ctypes.c_uint(flags),
            ctypes.c_void_p(ctypes.addressof(attributes)),
            ctypes.c_size_t(MOUNT_ATTR_SIZE_VER0),
        )

    if result < 0:
        _error("mount_setattr")


def _move_mount(fd: int, target: Path) -> None:
    try:
        function = _LIBC.move_mount
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        result = function(fd, b"", AT_FDCWD, os.fsencode(target), MOVE_MOUNT_F_EMPTY_PATH)
    except AttributeError:
        result = _LIBC.syscall(
            ctypes.c_long(_NR_MOVE_MOUNT),
            ctypes.c_int(fd),
            ctypes.c_char_p(b""),
            ctypes.c_int(AT_FDCWD),
            ctypes.c_char_p(os.fsencode(target)),
            ctypes.c_uint(MOVE_MOUNT_F_EMPTY_PATH),
        )

    if result < 0:
        _error("move_mount", target)


def _bind_mount(source: Path, target: Path, *, readonly: bool, recursive: bool = True) -> None:
    fd = _open_tree(source, recursive=recursive)

    try:
        _mount_setattr(fd, readonly=readonly, recursive=recursive)
        _move_mount(fd, target)
    finally:
        os.close(fd)


@contextmanager
def chroot(root: Path) -> Iterator[None]:
    """Temporarily change the process root and restore it afterward."""

    previous = Path.cwd()
    root_fd = os.open(_ROOT, os.O_CLOEXEC | os.O_PATH | os.O_DIRECTORY)
    changed = False

    try:
        os.chroot(root)
        changed = True
        os.chdir(_ROOT)
        yield
    finally:
        if changed:
            os.fchdir(root_fd)
            os.chroot(".")
            os.chdir(previous)
        os.close(root_fd)


def _under(root: Path, path: Path) -> Path:
    if root == _ROOT:
        return path

    if path.is_absolute():
        return root.joinpath(*path.parts[1:])

    return root / path


def _resolve(root: Path, path: Path, *, nofollow: bool = False) -> Path:
    if root == _ROOT:
        return path.parent.resolve() / path.name if nofollow else path.resolve()

    if not path.is_absolute():
        raise ValueError(f"sandbox path must be absolute: {path}")

    with chroot(root):
        resolved = path.parent.resolve() / path.name if nofollow else path.resolve()

    return _under(root, resolved)


@dataclass(frozen=True)
class Bind:
    source: Path
    target: Path
    readonly: bool = False
    nofollow: bool = False

    def mount(self, old_root: Path = _ROOT, new_root: Path = _ROOT) -> None:
        source = _resolve(old_root, self.source, nofollow=self.nofollow)
        source_is_link = self.nofollow and source.is_symlink()
        source_is_directory = source.is_dir() and not source_is_link
        unresolved_target = _under(new_root, self.target)

        if not source_is_directory and unresolved_target.is_symlink():
            _bind_mount(source, unresolved_target, readonly=self.readonly)
            return

        target = _resolve(new_root, self.target)
        if not target.exists():
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            if source_is_link or source.is_file():
                target.touch(mode=0o644, exist_ok=False)
            else:
                target.mkdir(mode=0o755)

        _bind_mount(source, target, readonly=self.readonly)


@dataclass(frozen=True)
class Devices:
    target: Path
    tty: Path | None = None

    def mount(self, old_root: Path = _ROOT, new_root: Path = _ROOT) -> None:
        target = _resolve(new_root, self.target)
        target.mkdir(mode=0o755, parents=True, exist_ok=True)
        mount(Path("tmpfs"), target, "tmpfs", options="mode=0755")

        for name in ("null", "zero", "full", "random", "urandom", "tty", "fuse"):
            source = _under(old_root, Path("/dev") / name)
            if name == "fuse" and not source.exists():
                continue

            destination = target / name
            destination.touch(exist_ok=False)
            mount(source, destination, flags=MS_BIND)

        for descriptor, name in enumerate(("stdin", "stdout", "stderr")):
            (target / name).symlink_to(f"/proc/self/fd/{descriptor}")

        (target / "fd").symlink_to("/proc/self/fd")
        (target / "core").symlink_to("/proc/kcore")
        (target / "shm").mkdir(mode=0o1777)
        (target / "pts").mkdir(mode=0o755)
        mount(Path("devpts"), target / "pts", "devpts", options="newinstance,ptmxmode=0666,mode=620")
        (target / "ptmx").symlink_to("pts/ptmx")

        if self.tty is not None:
            destination = target / "console"
            destination.touch(exist_ok=False)
            mount(_under(old_root, self.tty), destination, flags=MS_BIND)


@dataclass(frozen=True)
class Tmpfs:
    target: Path

    def mount(self, old_root: Path = _ROOT, new_root: Path = _ROOT) -> None:
        target = _resolve(new_root, self.target)
        target.mkdir(mode=0o755, parents=True, exist_ok=True)

        options = None if target.name in ("tmp", "var/tmp") else "mode=0755"
        mount(Path("tmpfs"), target, "tmpfs", options=options)


@dataclass(frozen=True)
class Symlink:
    source: Path
    target: Path

    def mount(self, old_root: Path = _ROOT, new_root: Path = _ROOT) -> None:
        target = _under(new_root, self.target)
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)

        try:
            target.symlink_to(self.source)
        except FileExistsError:
            if not target.is_symlink() or target.readlink() != self.source:
                raise


@dataclass(frozen=True)
class Overlay:
    lowerdirs: tuple[Path, ...]
    upperdir: Path
    workdir: Path
    target: Path

    def mount(self, old_root: Path = _ROOT, new_root: Path = _ROOT) -> None:
        lowers = tuple(_resolve(old_root, path) for path in self.lowerdirs)
        upper = _resolve(old_root, self.upperdir)
        work = _resolve(old_root, self.workdir)
        target = _resolve(new_root, self.target)

        for path in (*lowers, upper, work):
            if not path.exists():
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)

        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        target.mkdir(mode=0o755, exist_ok=True)

        options = ",".join(
            (
                f"lowerdir={':'.join(map(str, lowers))}",
                f"upperdir={upper}",
                f"workdir={work}",
                "userxattr",
                "index=off",
                "metacopy=off",
            )
        )

        mount(Path("overlayfs"), target, "overlay", options=options)


Filesystem = Bind | Devices | Tmpfs | Symlink | Overlay


@dataclass(frozen=True)
class Sandbox:
    filesystems: tuple[Filesystem, ...]
    chdir: Path | None = None
    become_root: bool = False
    isolate_network: bool = False
    suppress_chown: bool = False
    suppress_sync: bool = False


def _filesystem_key(filesystem: Filesystem) -> tuple[tuple[str, ...], bool]:
    return filesystem.target.parts, isinstance(filesystem, Bind)


def enter(sandbox: Sandbox) -> None:
    for filesystem in sandbox.filesystems:
        if not filesystem.target.is_absolute():
            raise ValueError(f"sandbox destination must be absolute: {filesystem.target}")

    user_namespace = _acquire_privileges(
        become_root=sandbox.become_root,
        network=sandbox.isolate_network,
    )

    namespaces = CLONE_NEWNS
    if sandbox.isolate_network and _has_capability(CAP_NET_ADMIN):
        namespaces |= CLONE_NEWNET

    _suppress_syscalls(
        chown=sandbox.suppress_chown and (user_namespace or _single_user_namespace()),
        sync=sandbox.suppress_sync,
    )

    try:
        unshare(namespaces)
    except OSError as error:
        if error.errno == errno.EPERM:
            raise SandboxOSError(
                errno.EPERM,
                "tine cannot create the sandbox mount namespace; check the host's namespace policy",
            ) from error
        raise

    if not user_namespace:
        mount(None, _ROOT, flags=MS_SLAVE | MS_REC)

    mount(Path("tmpfs"), Path("/tmp"), "tmpfs")
    os.chdir("/tmp")

    Path("newroot").mkdir(mode=0o755)
    Path("oldroot").mkdir(mode=0o755)
    mount(Path("newroot"), Path("newroot"), flags=MS_BIND | MS_REC)

    if _LIBC.pivot_root(b".", b"oldroot") < 0:
        mount(_ROOT, Path("oldroot"), flags=MS_BIND | MS_REC)
        mount(Path("."), _ROOT, flags=MS_MOVE)
        os.chroot(".")
        os.chdir(".")
        umount2(Path("oldroot/tmp"), MNT_DETACH)

    for filesystem in sorted(sandbox.filesystems, key=_filesystem_key):
        filesystem.mount(Path("/oldroot"), Path("/newroot"))

    os.chdir("newroot")
    if _LIBC.pivot_root(b".", b".") < 0:
        _error("pivot_root")

    umount2(Path("."), MNT_DETACH)

    if sandbox.chdir is not None:
        os.chdir(sandbox.chdir)
