"""Package a logical image's /usr and /opt as a systemd system-extension DDI.

The driver authors `usr/lib/extension-release.d/extension-release.<name>`, drops the base
os-release, and runs `systemd-repart --make-ddi=sysext` for a GPT image holding an erofs data
partition plus its verity hash (unsigned for now). With `base`, only the delta layered above
that image is packaged, and the extension-release pins the base's ID/VERSION_ID.
"""

load(
    "//image:image.bzl",
    "IMAGE_TOOLS_ATTR",
    "ImageInfo",
    "ImageToolsInfo",
    "terminal_image_command",
)
load("//package:manager.bzl", "PackageManagerInfo")
load("//package:system.bzl", "PackageSystemInfo")

SysextImageInfo = provider(
    doc = "A systemd system-extension DDI generated from a logical image.",
    fields = {
        "engine": provider_field(Dependency),
        "extension": provider_field(str),
        "image": provider_field(Artifact),
    },
)

# buildifier: disable=function-docstring-args
# buildifier: disable=function-docstring-return
def declare_image_sysext(
        ctx: AnalysisContext,
        *,
        image: ImageInfo,
        extension: str,
        base: ImageInfo | None = None,
        release: dict[str, str] = {},
        seed: str | None = None) -> SysextImageInfo:
    """Declare a system-extension DDI from resolved logical images."""
    out = ctx.actions.declare_output(extension + ".raw")

    # systemd-sysext refuses images without these fields. Without a base there is nothing to
    # match strictly, so accept any host; with one, the driver pins the base's ID/VERSION_ID.
    release_fields = {}
    if base == None:
        release_fields["ID"] = "_any"
    release_fields["SYSEXT_SCOPE"] = "system"
    release_fields["EXTENSION_RELOAD_MANAGER"] = "1"
    release_fields.update(release)

    cmd = terminal_image_command(image, ctx.attrs._tools[ImageToolsInfo].sysext)
    if base != None:
        if len(base.layers) >= len(image.layers):
            fail("image_sysext: image must layer a delta on top of base")
        cmd.add("--base", str(len(base.layers)))

    # A merged extension must not shadow the host's package database, so strip wherever the
    # image's package system keeps it. An image without one installed no packages.
    if image.package_manager != None:
        system = image.package_manager[PackageManagerInfo].package_system[PackageSystemInfo]
        for path in system.database_paths:
            cmd.add("--pkgdb-path", path)
    cmd.add("--identity", str(ctx.label))
    if seed != None:
        cmd.add("--seed", seed)
    cmd.add("--name", extension)
    for key in sorted(release_fields):
        cmd.add("--release", "{}={}".format(key, release_fields[key]))
    cmd.add("--out", out.as_output())
    ctx.actions.run(cmd, category = "image_sysext")

    return SysextImageInfo(engine = image.engine, extension = extension, image = out)

def _image_sysext_impl(ctx: AnalysisContext) -> list[Provider]:
    info = declare_image_sysext(
        ctx,
        base = ctx.attrs.base[ImageInfo] if ctx.attrs.base != None else None,
        extension = ctx.attrs.extension_name or ctx.label.name,
        image = ctx.attrs.image[ImageInfo],
        release = ctx.attrs.release,
        seed = ctx.attrs.seed,
    )
    return [DefaultInfo(default_output = info.image), info]

SYSEXT_ATTRS = {
    "release": attrs.dict(
        key = attrs.string(),
        value = attrs.string(),
        default = {},
        doc = "extension-release fields, overriding the defaults",
    ),
    "seed": attrs.option(
        attrs.string(),
        default = None,
        doc = "explicit GPT/partition UUID seed; by default derive one from the target identity",
    ),
}

image_sysext = rule(
    impl = _image_sysext_impl,
    attrs = SYSEXT_ATTRS | {
        "base": attrs.option(
            attrs.dep(providers = [ImageInfo]),
            default = None,
            doc = "base image the extension overlays; image must extend it, and only the delta is packaged",
        ),
        "extension_name": attrs.option(
            attrs.string(),
            default = None,
            doc = "extension name; defaults to the target name",
        ),
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image supplying /usr and /opt"),
    } | IMAGE_TOOLS_ATTR,
)
