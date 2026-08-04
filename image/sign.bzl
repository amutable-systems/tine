"""Signing credentials, and the ways an image can obtain them.

A private key and its certificate are one credential, so a signing role takes one key. Every producer
hands the signing tools a `SigningKeyInfo`, which is all a call site needs to know about a key. Material
in the build graph makes signing a function of declared inputs, and therefore cacheable; an external
PKCS#11 key (https://p11-glue.github.io/p11-glue/p11-kit/manual/remoting.html) does not.

Do not load image.bzl here: it consumes the provider, so that would close a load cycle.
"""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")

# The sandbox forwards PKCS#11 calls rather than holding a backend module, so this is the only module
# it ever loads, whatever the key is held by.
_CLIENT_MODULE = "/usr/lib64/pkcs11/p11-kit-client.so"

# Labels are pasted into URIs verbatim, so restrict them to characters no PKCS#11 URI encodes: a URI
# then always reads as the label was written.
_LABEL_PATTERN = "^[A-Za-z0-9._-]+$"

# Host paths are pasted into a PKCS#11 URI as pin-source= and into P11_KIT_SERVER_ADDRESS. Both are
# attribute lists, so a delimiter in a path truncates the value there, and '%' reads as an escape;
# ':' separates a systemd key source from its argument. All of them fail as a wrong path at signing
# time rather than as a bad path here, so reject them while the path is still named after its option.
_PATH_DELIMITERS = [":", ";", "?", "&", ",", "%"]

# PKCS#11 client (socket and PIN) sandbox mount.
_SIGNING_CONFIG_DIR = "/run/signing"

SigningKeyInfo = provider(
    doc = "A signing credential, as local PEM artifacts or external URIs",
    fields = {
        # Each a PEM artifact, or a URI when source is set
        "certificate": provider_field(Artifact | str),
        "private_key": provider_field(Artifact | str),
        # Host → r/o sandbox paths for external signing
        "ro_binds": provider_field(dict[str, str], default = {}),
        # Additional sandbox environment for external signing
        "setenv": provider_field(dict[str, str], default = {}),
        # OpenSSL key source in systemd's spelling, None for local Artifacts
        "source": provider_field(str | None, default = None),
    },
)

def resolve_signing_key(dep: Dependency | None) -> SigningKeyInfo | None:
    """Read an optional signing key attribute, None meaning the role is unsigned."""
    return dep[SigningKeyInfo] if dep != None else None

SigningAccess = record(
    # What the sandbox running the signing tool must be given.
    ro_binds = field(dict[str, str]),
    setenv = field(dict[str, str]),
    # True when any key lives outside the build graph; the action is then non-hermetic.
    external = field(bool),
)

def merge_signing_access(keys: list[SigningKeyInfo | None]) -> SigningAccess:
    """Merge what one action's keys need from the host."""
    ro_binds = {}
    setenv = {}
    external = False
    for key in keys:
        if key == None:
            continue
        # Bind destinations derive from the host path, so shared paths merge identically. The
        # environment can genuinely conflict: keys served over different sockets disagree on
        # P11_KIT_SERVER_ADDRESS, and one action reaches one server.
        ro_binds |= key.ro_binds
        for name, value in key.setenv.items():
            if setenv.get(name, value) != value:
                fail("signing: the keys disagree on {}: {!r} and {!r}".format(name, setenv[name], value))
            setenv[name] = value
        # Any of the three makes the action depend on this host, and a key that needs a bind or a
        # variable without naming a source would otherwise keep the action cacheable and remotable.
        external = external or key.source != None or bool(key.ro_binds or key.setenv)
    return SigningAccess(ro_binds = ro_binds, setenv = setenv, external = external)

def _signing_key_impl(ctx: AnalysisContext) -> list[Provider]:
    key = ctx.actions.declare_output("signing.key")
    certificate = ctx.actions.declare_output("signing.crt")
    ctx.actions.run(
        cmd_args(
            chroot_run(engine = ctx.attrs.engine[EngineInfo]),
            "ukify",
            "genkey",
            cmd_args(key.as_output(), format = "--secureboot-private-key={}"),
            cmd_args(certificate.as_output(), format = "--secureboot-certificate={}"),
        ),
        category = "signing_key",
    )
    return [
        DefaultInfo(
            sub_targets = {
                "cert": [DefaultInfo(default_output = certificate)],
                "key": [DefaultInfo(default_output = key)],
            }
        ),
        SigningKeyInfo(certificate = certificate, private_key = key),
    ]

generate_signing_key = rule(
    impl = _signing_key_impl,
    attrs = {
        "engine": attrs.dep(
            providers = [EngineInfo],
            default = "tine//catalog:fedora.rawhide.engine",
            doc = "engine providing ukify",
        ),
    },
    doc = """Mint a development signing key pair as a build artifact.

    `ukify genkey` produces the pair per workspace into buck-out, so nothing secret is committed, and
    anyone who wants one by hand can run `ukify genkey` directly. `:<name>[cert]` and `:<name>[key]`
    are the PEM artifacts.
    """,
)

def _pem_signing_key_impl(ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(),
        SigningKeyInfo(certificate = ctx.attrs.certificate, private_key = ctx.attrs.private_key),
    ]

pem_signing_key = rule(
    impl = _pem_signing_key_impl,
    attrs = {
        "certificate": attrs.source(doc = "PEM certificate"),
        "private_key": attrs.source(doc = "PEM private key"),
    },
    doc = """Adopt a committed PEM pair as a signing key.

    The material is a stable build input, so the whole signed image graph stays cacheable and every
    build enrols the same certificate. Such a key is public to everyone with repository access: use it
    for test images only, and never enrol it on real hardware.
    """,
)

# Local check rather than image.bzl's check_name, which the load cycle puts out of reach.
def _check_label(what: str, value: str) -> str:
    if not regex_match(_LABEL_PATTERN, value):
        fail("pkcs11_signing_key: {} must match {}, got {!r}".format(what, _LABEL_PATTERN, value))
    return value

def _check_host_path(what: str, path: str) -> str:
    """Reject a host path a bind cannot carry or an action should not depend on."""
    if not path.startswith("/"):
        fail("pkcs11_signing_key: {} must be an absolute host path, got {!r}".format(what, path))
    for char in _PATH_DELIMITERS:
        if char in path:
            fail("pkcs11_signing_key: {} cannot contain {!r}, got {!r}".format(what, char, path))
    if "buck-out" in path.split("/"):
        fail("pkcs11_signing_key: {} must not name a build artifact, got {!r}".format(what, path))
    return path

def _pkcs11_signing_key_impl(ctx: AnalysisContext) -> list[Provider]:
    token = _check_label("token", ctx.attrs.token)
    object = _check_label("object", ctx.attrs.object or token)
    pin_file = _check_host_path("pin_file", ctx.attrs.pin_file)
    socket = _check_host_path("socket", ctx.attrs.socket)

    # A bind destination is created as a directory unless the source is a regular file, so a socket
    # can only be exposed by way of the directory holding it.
    host_directory, _, socket_name = socket.rpartition("/")
    if not host_directory or not socket_name:
        fail("pkcs11_signing_key: socket must name a file in a directory below /, got {!r}".format(socket))

    sandbox_socket = _SIGNING_CONFIG_DIR + socket
    sandbox_pin = _SIGNING_CONFIG_DIR + pin_file
    uri = "pkcs11:token={};object={};type=".format(token, object)

    return [
        DefaultInfo(),
        SigningKeyInfo(
            certificate = uri + "cert",
            # pin-source rather than pin-value: the URI lands in spec files and tool command lines,
            # neither secret. RFC 7512 puts it in the query component, hence the '?'.
            private_key = uri + "private?pin-source=" + sandbox_pin,
            ro_binds = {host_directory: _SIGNING_CONFIG_DIR + host_directory, pin_file: sandbox_pin},
            setenv = {
                "P11_KIT_SERVER_ADDRESS": "unix:path={}".format(sandbox_socket),
                "PKCS11_PROVIDER_MODULE": _CLIENT_MODULE,
            },
            source = "provider:pkcs11",
        ),
    ]

pkcs11_signing_key = rule(
    impl = _pkcs11_signing_key_impl,
    attrs = {
        "object": attrs.option(
            attrs.string(),
            default = None,
            doc = "the key's CKA_LABEL; defaults to the token label",
        ),
        "pin_file": attrs.string(doc = "host path to a file with the token PIN on its first line"),
        "socket": attrs.string(doc = "host path of the p11-kit server socket"),
        "token": attrs.string(doc = "the token label"),
    },
    doc = """Address a key held by a PKCS#11 token reachable over a p11-kit server socket.

    The paths are host state, so they come from build configuration rather than the graph, and the
    invoker resolves them.
    """,
)
