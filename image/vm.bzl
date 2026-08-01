"""Interactive virtual-machine image runners."""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")
load("//image_format:disk.bzl", "RepartInfo")
load("//image_format:sysext.bzl", "SysextImageInfo")

# A byte count with an optional systemd size suffix, which vmspawn reads base-1024.
_SIZE_PATTERN = "^[0-9]+(\\.[0-9]+)?[KMGTPE]?$"

def _image_vm_impl(ctx: AnalysisContext) -> list[Provider]:
    disk = ctx.attrs.image[RepartInfo]
    if disk.disk == None:
        fail("image_vm: RepartInfo does not contain a composed disk")
    run = cmd_args(
        chroot_run(
            engine = ctx.attrs.engine[EngineInfo],
            exe = "systemd-vmspawn",
            relaxed = True,
        ),
        "--image",
        disk.disk,
        "--console=native",
        "--ephemeral",
        "--kvm=yes",
        "--network-user-mode",
    )

    if ctx.attrs.grow != None:
        if not regex_match(_SIZE_PATTERN, ctx.attrs.grow):
            fail("image_vm: invalid grow size {!r}".format(ctx.attrs.grow))

        # vmspawn grows the file itself, before the ephemeral overlay is stacked on top of it, so
        # the guest sees the larger disk while its writes still go nowhere.
        run.add("--grow-image={}".format(ctx.attrs.grow))

    if ctx.attrs.secure_boot:
        # OVMF variable store starts in setup mode and sd-boot enrolls the
        # image's loader/keys/auto keys on first boot.
        run.add("--secure-boot=yes", "--tpm=yes")

    if ctx.attrs.sysexts:
        # each DDI must be named after its extension.
        extensions = {}
        for dep in ctx.attrs.sysexts:
            info = dep[SysextImageInfo]
            if info.extension + ".raw" in extensions:
                fail("image_vm: duplicate sysext {!r}".format(info.extension))
            extensions[info.extension + ".raw"] = info.image
        run.add(
            cmd_args(
                ctx.actions.copied_dir("extensions", extensions),
                # can't use /run/extensions: vmspawn marks binds x-initrd.mount, so the initrd
                # mounts them at /sysroot/<target>; switch-root then moves the initrd's own /run
                # tmpfs onto the new root's /run, burying a mount on that directory.
                format = "--bind={}:/var/lib/extensions",
            ),
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
        "engine": attrs.dep(providers = [EngineInfo], doc = "execution environment supplying the VM stack"),
        "grow": attrs.option(
            attrs.string(),
            default = None,
            doc = 'size to grow the disk file to before booting, e.g. "8G"; the built image itself grows',
        ),
        "image": attrs.dep(providers = [RepartInfo], doc = "the raw disk image to boot ephemerally"),
        "secure_boot": attrs.bool(
            default = False,
            doc = "boot with Secure Boot capable firmware",
        ),
        "sysexts": attrs.list(
            attrs.dep(providers = [SysextImageInfo]),
            default = [],
            doc = "sysext DDIs exposed to the guest under /var/lib/extensions",
        ),
    },
)
