"""Interactive virtual-machine image runners."""

load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//distribution:defs.bzl", "distributed")
load("//image_format:disk.bzl", "RepartInfo", "SIZE_PATTERN")
load("//image_format:sysext.bzl", "SysextImageInfo")

def _image_vm_impl(ctx: AnalysisContext) -> list[Provider]:
    disk = ctx.attrs.image[RepartInfo]
    if disk.disk == None:
        fail("image_vm: RepartInfo does not contain a composed disk")
    run = cmd_args(
        box_run(
            box = ctx.attrs.box[BoxInfo],
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
        if not regex_match(SIZE_PATTERN, ctx.attrs.grow):
            fail("image_vm: invalid grow size {!r}".format(ctx.attrs.grow))

        # vmspawn grows the file itself, before the ephemeral overlay is stacked on top of it, so
        # the guest sees the larger disk while its writes still go nowhere.
        run.add("--grow-image={}".format(ctx.attrs.grow))

    if ctx.attrs.ram != None:
        if not regex_match(SIZE_PATTERN, ctx.attrs.ram):
            fail("image_vm: invalid RAM size {!r}".format(ctx.attrs.ram))
        run.add("--ram={}".format(ctx.attrs.ram))

    if ctx.attrs.cpus != None:
        if ctx.attrs.cpus < 1:
            fail("image_vm: cpus must be positive, got {}".format(ctx.attrs.cpus))
        run.add("--cpus={}".format(ctx.attrs.cpus))

    if ctx.attrs.secure_boot:
        # OVMF variable store starts in setup mode and sd-boot enrolls the
        # image's loader/keys/auto keys on first boot.
        run.add("--secure-boot=yes")

    # Secure Boot brings one for the expected-PCR policy to be measured into, but an image needs a
    # TPM whenever it seals anything to one, which a repart definition asking for Encrypt=tpm2 does
    # on first boot, Secure Boot or not.
    if ctx.attrs.secure_boot or ctx.attrs.tpm:
        run.add("--tpm=yes")

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

    # vmspawn takes extra kernel command line arguments as its own trailing arguments, and picks how
    # to deliver them from what it is booting: an SMBIOS OEM string each for the stub and the boot
    # loader for a UKI, `-append` for a bare kernel image. Handing it the arguments rather than the
    # OEM strings also lets it read them, which is how it knows not to add a root= of its own.
    for argument in ctx.attrs.cmdline_extra:
        if not argument:
            fail("image_vm: cmdline_extra arguments cannot be empty")
        run.add(argument)
    return [DefaultInfo(), RunInfo(args = run)]

_image_vm = rule(
    impl = _image_vm_impl,
    attrs = {
        "autologin": attrs.option(
            attrs.string(),
            default = None,
            doc = "user to log in automatically without authentication",
        ),
        "box": attrs.dep(providers = [BoxInfo], doc = "execution environment supplying the VM stack"),
        "cmdline_extra": attrs.list(
            attrs.string(),
            default = [],
            doc = "kernel command line arguments appended to the ones the image boots with",
        ),
        "cpus": attrs.option(
            attrs.int(),
            default = None,
            doc = "number of virtual CPUs exposed to the guest",
        ),
        "credentials": attrs.dict(
            key = attrs.string(),
            value = attrs.string(),
            default = {},
            doc = "non-secret system credentials passed to systemd-vmspawn",
        ),
        "grow": attrs.option(
            attrs.string(),
            default = None,
            doc = 'size to grow the disk file to before booting, e.g. "8G"; the built image itself grows',
        ),
        "image": attrs.dep(providers = [RepartInfo], doc = "the raw disk image to boot ephemerally"),
        "ram": attrs.option(
            attrs.string(),
            default = None,
            doc = 'guest RAM size passed to vmspawn, e.g. "4G"',
        ),
        "secure_boot": attrs.bool(
            default = False,
            doc = "boot with Secure Boot capable firmware",
        ),
        "sysexts": attrs.list(
            attrs.dep(providers = [SysextImageInfo]),
            default = [],
            doc = "sysext DDIs exposed to the guest under /var/lib/extensions",
        ),
        "tpm": attrs.bool(
            default = False,
            doc = "attach a software TPM, which Secure Boot implies",
        ),
    },
)

def image_vm(name: str, distribution: str | None = None, visibility: list[str] | None = None, **kwargs) -> None:
    """Declare one, compatible with the distributions its package serves."""
    _image_vm(name = name, **(distributed(name, distribution, visibility) | kwargs))
