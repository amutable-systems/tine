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
        # What the logical image is published under: the DDI adds the suffixes systemd matches a
        # transfer against, the metadata views the ones that say what each of them is.
        "basename": provider_field(str),
        "box": provider_field(Dependency),
        "extension": provider_field(str),
        "image": provider_field(Artifact),
        # What the DDI carries, as a UAPI.16 file manifest: the /usr and /opt of the delta alone,
        # unlike the manifest of the logical image, which is the whole tree it merges onto.
        "manifest": provider_field(Artifact),
        # The DDI's verity root hash, hex plus newline. A host reports it for the merged
        # extension (/usr/.systemd-sysext/origin)
        "root_hash": provider_field(Artifact),
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

    # The DDI leaves the build as an update artifact, so it is named the way it is published:
    # systemd-sysupdate matches an extension transfer against <extension>_<version>_<arch>.sysext.raw.
    # arch "_any" opts out of a specific architecture: the DDI is published under the "all" arch (a
    # literal "_any" would double the name separator and break vpick's version parse), and its
    # extension-release ARCHITECTURE is "_any". systemd-vpick treats the unknown "all" as the any
    # bucket, and later systemd-sysext skips the arch check for "_any".
    if arch == "_any":
        systemd_arch = "_any"
        architecture = "all"
    else:
        systemd_arch = ARCHES[arch].systemd
        architecture = systemd_arch
    basename = "{}_{}_{}".format(extension, version, architecture)
    stem = basename + ".sysext"
    out = ctx.actions.declare_output(stem + ".raw")

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

    # Written by the driver from the tree it packages, and named after the DDI so a release
    # publishes the listing under a name that says which image it belongs to.
    manifest = ctx.actions.declare_output(stem + ".Uapi16Manifest")

    # The verity root hash, named like the DDI it identifies.
    root_hash = ctx.actions.declare_output(stem + ".roothash")

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
            "manifest": manifest.as_output(),
            "name": extension,
            "out": out.as_output(),
            # A merged extension must not shadow the host's package database.
            "pkgdb_paths": pkgdb_paths(image),
            "release": {key: release_fields[key] for key in sorted(release_fields)},
            "root_hash_out": root_hash.as_output(),
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

    return SysextImageInfo(
        basename = basename,
        box = image.box,
        extension = extension,
        image = out,
        manifest = manifest,
        root_hash = root_hash,
    )

def sysext_published(info: SysextImageInfo) -> PublishedInfo:
    """What a release carries for one extension.

    The DDI, its verity root hash, and the listing of what it holds."""
    return PublishedInfo(
        artifacts = {
            info.image.basename: info.image,
            info.manifest.basename: info.manifest,
            info.root_hash.basename: info.root_hash,
        },
        image = info.basename,
        kinds = {
            info.image.basename: "sysext",
            info.manifest.basename: "listing",
            info.root_hash.basename: "roothash",
        },
    )

def sysext_subtargets(info: SysextImageInfo) -> dict[str, list[Provider]]:
    """The extension's own views, beside whichever ones the rule exposes for its logical image."""
    return {
        "ddi-manifest": [DefaultInfo(default_output = info.manifest)],
        "roothash": [DefaultInfo(default_output = info.root_hash)],
    }

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
        DefaultInfo(default_output = info.image, sub_targets = sysext_subtargets(info)),
        sysext_published(info),
        info,
    ]

SYSEXT_ATTRS = {
    "arch": attrs.enum(
        ARCHES.keys() + ["_any"],
        default = "x86_64",
        doc = "architecture the extension merges on, in its name and its extension-release, "
        + "\"_any\" publishes the DDI as '..._all' and sets ARCHITECTURE=_any",
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
