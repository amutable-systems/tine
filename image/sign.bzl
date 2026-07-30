"""Generate a development Secure Boot signing key as a build artifact.

`ukify genkey` mints the pair per workspace into buck-out, so nothing secret is committed. Anyone who
needs a key by hand can run `ukify genkey` directly; production signing belongs behind a dedicated
boundary instead. `:<name>[cert]` and `:<name>[key]` are the PEM artifacts.
"""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")

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
        )
    ]

signing_key = rule(
    impl = _signing_key_impl,
    attrs = {
        "engine": attrs.dep(
            providers = [EngineInfo],
            default = "tine//catalog:fedora.rawhide.engine",
            doc = "engine providing ukify",
        ),
    },
)
