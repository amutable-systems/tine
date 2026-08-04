"""Signing credentials, and the ways an image can obtain them.

A private key and its certificate are one credential, so a signing role takes one key. Every producer
hands the signing tools a `SigningKeyInfo`, which is all a call site needs to know about a key.

Do not load image.bzl here: it consumes the provider, so that would close a load cycle.
"""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")

SigningKeyInfo = provider(
    doc = "A private key and the certificate that goes with it.",
    fields = {
        "certificate": provider_field(Artifact),
        "private_key": provider_field(Artifact),
    },
)

def resolve_signing_key(dep: Dependency | None) -> SigningKeyInfo | None:
    """Read an optional signing key attribute, None meaning the role is unsigned."""
    return dep[SigningKeyInfo] if dep != None else None

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
