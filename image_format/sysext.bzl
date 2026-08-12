"""Package a logical image's /usr and /opt as a systemd system-extension DDI.

The driver authors `usr/lib/extension-release.d/extension-release.<name>`, drops the base
os-release, and runs `systemd-repart --make-ddi=sysext` for a GPT image holding an erofs data
partition plus its verity hash. With `verity_key`, a third partition carries a signature over
the verity root hash, which a host can verify against the key's certificate in its `verity.d`
before merging. With `base`, only the delta layered above that image is packaged, and the
extension-release pins the base's ID/VERSION_ID.
"""

load(
    "//image:image.bzl",
    "ARCHES",
    "IMAGE_TOOLS_ATTR",
    "ImageInfo",
    "ImageToolsInfo",
    "check_version",
    "pkgdb_paths",
    "terminal_image_command",
)
load("//image:publish.bzl", "PublishedInfo")
load(
    "//image:sign.bzl",
    "SigningKeyInfo",
    "VERITY_KEY_ATTR",
    "external_signing_execution",
    "merge_signing_access",
    "resolve_signing_key",
    "signing_key_spec",
)

SysextImageInfo = provider(
    doc = "A systemd system-extension DDI generated from a logical image.",
    fields = {
        "box": provider_field(Dependency),
        "extension": provider_field(str),
        "image": provider_field(Artifact),
    },
)

def declare_image_sysext(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    image: ImageInfo,
    extension: str,
    arch: str,
    version: str,
    base: ImageInfo | None = None,
    release: dict[str, str] = {},
    seed: str | None = None,
    verity_key: SigningKeyInfo | None = None,
) -> SysextImageInfo:
    """Declare a system-extension DDI from resolved logical images."""
    # The version names the published DDI, which systemd-sysupdate matches transfers against.
    check_version("sysext_image version", version)
    systemd_arch = ARCHES[arch].systemd

    # The DDI leaves the build as an update artifact, so it is named the way it is published:
    # systemd-sysupdate matches an extension transfer against <extension>_<version>_<arch>.sysext.raw.
    out = ctx.actions.declare_output("{}_{}_{}.sysext.raw".format(extension, version, systemd_arch))

    # systemd-sysext refuses images without these fields. Without a base there is nothing to
    # match strictly, so accept any host; with one, the driver pins the base's ID/VERSION_ID.
    release_fields = {}
    if base == None:
        release_fields["ID"] = "_any"
    release_fields["SYSEXT_SCOPE"] = "system"
    release_fields["EXTENSION_RELOAD_MANAGER"] = "1"

    # What the rule already knows, rather than fields a caller restates: the extension names itself,
    # and its version and architecture are the ones its DDI is published under. `release` overrides
    # every one of them.
    release_fields["ARCHITECTURE"] = systemd_arch
    release_fields["SYSEXT_ID"] = extension
    release_fields["SYSEXT_VERSION_ID"] = version
    release_fields["IMAGE_VERSION"] = version
    release_fields.update(release)

    layers = 0
    if base != None:
        if len(base.layers) >= len(image.layers):
            fail("image_sysext: image must layer a delta on top of base")
        layers = len(base.layers)

    signing_access = merge_signing_access([verity_key])
    cmd = terminal_image_command(
        ctx,
        signing_access = signing_access,
        # The DDI is named after the extension, so one composition can declare several.
        driver = "sysext-" + extension,
        exe = tools.sysext,
        image = image,
        spec = {
            "base": layers,
            "identity": str(ctx.label),
            "name": extension,
            "out": out.as_output(),
            # A merged extension must not shadow the host's package database.
            "pkgdb_paths": pkgdb_paths(image),
            "release": {key: release_fields[key] for key in sorted(release_fields)},
            "seed": seed,
            "signing": signing_key_spec(verity_key),
        },
    )
    ctx.actions.run(
        cmd,
        category = "image_sysext",
        # The category must be unique per target and a composition can declare several extensions.
        identifier = extension,
        **external_signing_execution(signing_access),
    )

    return SysextImageInfo(box = image.box, extension = extension, image = out)

def _image_sysext_impl(ctx: AnalysisContext) -> list[Provider]:
    info = declare_image_sysext(
        ctx,
        tools = ctx.attrs._tools[ImageToolsInfo],
        arch = ctx.attrs.arch,
        base = ctx.attrs.base[ImageInfo] if ctx.attrs.base != None else None,
        extension = ctx.attrs.extension_name or ctx.label.name,
        image = ctx.attrs.image[ImageInfo],
        release = ctx.attrs.release,
        seed = ctx.attrs.seed,
        verity_key = resolve_signing_key(ctx.attrs.verity_key),
        version = ctx.attrs.version,
    )
    return [
        DefaultInfo(default_output = info.image),
        PublishedInfo(artifacts = {info.image.basename: info.image}),
        info,
    ]

SYSEXT_ATTRS = {
    "arch": attrs.enum(
        ARCHES.keys(),
        default = "x86_64",
        doc = "architecture the extension merges on, in its name and its extension-release",
    ),
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
    "verity_key": VERITY_KEY_ATTR,
}

image_sysext = rule(
    impl = _image_sysext_impl,
    attrs = SYSEXT_ATTRS
    | {
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
        # sysext_image takes the same name from IMAGE_ATTRS, where it also stamps the SBOM.
        "version": attrs.string(default = "0", doc = "version the DDI is published under"),
    }
    | IMAGE_TOOLS_ATTR,
)
