"""Package a logical image's /usr and /opt as a systemd system-extension DDI.

The driver authors `usr/lib/extension-release.d/extension-release.<name>`, drops the base
os-release, and runs `systemd-repart --make-ddi=sysext` for a GPT image holding an erofs data
partition plus its verity hash (unsigned for now). With `base`, only the delta layered above
that image is packaged, and the extension-release pins the base's ID/VERSION_ID.
"""

load("//image:actions.bzl", "terminal_image_command")
load("//image:layer.bzl", "ImageInfo")
load(":rpmdb.bzl", "RpmdbInfo")

SysextImageInfo = provider(
    doc = "A systemd system-extension DDI generated from a logical image.",
    fields = {
        "engine": provider_field(Dependency),
        "extension": provider_field(str),
        "image": provider_field(Artifact),
        "source": provider_field(Dependency),
    },
)

def _image_sysext_impl(ctx: AnalysisContext) -> list[Provider]:
    image = ctx.attrs.image[ImageInfo]
    extension = ctx.attrs.extension_name or ctx.label.name
    out = ctx.actions.declare_output(extension + ".raw")

    # systemd-sysext refuses images without these fields. Without a base there is nothing to
    # match strictly, so accept any host; with one, the driver pins the base's ID/VERSION_ID.
    release = {}
    if ctx.attrs.base == None:
        release["ID"] = "_any"
    release["SYSEXT_SCOPE"] = "system"
    release["EXTENSION_RELOAD_MANAGER"] = "1"
    release.update(ctx.attrs.release)

    cmd = terminal_image_command(image, ctx.attrs._driver)
    if ctx.attrs.base != None:
        base = ctx.attrs.base[ImageInfo]
        if len(base.layers) >= len(image.layers):
            fail("image_sysext: image must layer a delta on top of base")
        cmd.add("--base", str(len(base.layers)))
    cmd.add("--identity", str(ctx.label))
    if ctx.attrs.seed != None:
        cmd.add("--seed", ctx.attrs.seed)
    cmd.add("--name", extension)
    for key in release:
        cmd.add("--release", "{}={}".format(key, release[key]))
    cmd.add("--out", out.as_output())
    ctx.actions.run(cmd, category = "image_sysext")

    # The rpm database is dropped from the DDI; the sidecar rides along like on image_archive.
    providers = []
    sidecars = []
    sub_targets = {}
    if ctx.attrs.rpmdb != None:
        info = ctx.attrs.rpmdb[DefaultInfo]
        sidecars.extend(info.default_outputs)
        sub_targets["rpmdb"] = [info]
        providers.append(ctx.attrs.rpmdb[RpmdbInfo])

    return [
        DefaultInfo(default_output = out, other_outputs = sidecars, sub_targets = sub_targets),
        SysextImageInfo(
            engine = image.engine,
            extension = extension,
            image = out,
            source = ctx.attrs.image,
        ),
    ] + providers

image_sysext = rule(
    impl = _image_sysext_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image supplying /usr and /opt"),
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
        "release": attrs.dict(
            key = attrs.string(),
            value = attrs.string(),
            default = {},
            doc = "extension-release fields, overriding the defaults",
        ),
        "rpmdb": attrs.option(attrs.dep(providers = [RpmdbInfo]), default = None, doc = "rpmdb artifact to ride along"),
        "seed": attrs.option(
            attrs.string(),
            default = None,
            doc = "explicit GPT/partition UUID seed; by default derive one from the target identity",
        ),
        "_driver": attrs.dep(providers = [RunInfo], default = "tine//image_format:sysext"),
    },
)
