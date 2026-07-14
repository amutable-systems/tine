"""Interactive virtual-machine image runners."""

load("//engine:rules.bzl", "EngineInfo", "chroot_run")
load("//image_format:disk.bzl", "DiskImageInfo")

def _image_vm_impl(ctx: AnalysisContext) -> list[Provider]:
    disk = ctx.attrs.image[DiskImageInfo]
    run = cmd_args(
        chroot_run(
            engine = disk.engine[EngineInfo],
            exe = "systemd-vmspawn",
            relaxed = True,
        ),
        "--image",
        disk.image,
        "--console=native",
        "--ephemeral",
        "--kvm=yes",
        "--network-user-mode",
    )

    if ctx.attrs.autologin != None:
        if not ctx.attrs.autologin:
            fail("image_vm: autologin user cannot be empty")
        for name in ("agetty.autologin", "login.noauth", "passwd.hashed-password.root"):
            if name in ctx.attrs.credentials:
                fail("image_vm: credential {!r} is managed by autologin".format(name))
        run.add(
            "--set-credential=agetty.autologin:{}".format(ctx.attrs.autologin),
            "--set-credential=login.noauth:yes",
            "--set-credential=passwd.hashed-password.root:!*",
        )

    for name in sorted(ctx.attrs.credentials):
        if not name or ":" in name:
            fail("image_vm: invalid credential name {!r}".format(name))
        run.add("--set-credential={}:{}".format(name, ctx.attrs.credentials[name]))
    return [DefaultInfo(), RunInfo(args = run)]

image_vm = rule(
    impl = _image_vm_impl,
    attrs = {
        "autologin": attrs.option(
            attrs.string(),
            default = None,
            doc = "user to log in automatically without authentication",
        ),
        "credentials": attrs.dict(
            key = attrs.string(),
            value = attrs.string(),
            default = {},
            doc = "non-secret system credentials passed to systemd-vmspawn",
        ),
        "image": attrs.dep(providers = [DiskImageInfo], doc = "the raw disk image to boot ephemerally"),
    },
)
