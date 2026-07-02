"""rpm_package_json: build an rpm from generated <package>.json metadata.

This is the only module that knows the JSON schema, as the hand-over point
between the package importer and buck build system.

The generated per-distro/release BUCK loads each <package>.json and hands them all to
`rpm_branch`, which projects each onto `rpm_package_json`; `SrcpkgMetadata(**meta)`
validates each against the schema at load time (missing/extra/mistyped fields fail the
parse).

"""

load("@prelude//:native.bzl", "native")
load(":rpm.bzl", "rpm_package")

# Mirrors importer's SrcpkgMetadata TypedDict (the generated <package>.json), keep in sync.
# buildifier: disable=name-conventions  (a record *type*, conventionally UpperCamelCase)
SrcpkgMetadata = record(
    build_requires = list[str],
    # only carried for schema parity, unused
    binaries = dict,
    sources = list[dict[str, str]],
    version = str,
    release = str,
    dist = str,
    subpackages = list[str],
    source_date_epoch = int,
)

# buildifier: disable=unnamed-macro  (fan-out macro: an rpm_package + its source http_files)
def rpm_package_json(package: str, distribution: str, meta: dict) -> None:
    """Project a generated <package>.json onto rpm_package.

    `meta` is the natively-decoded json dict, matching SrcpkgMetadata (validated on load).
    The spec + committed Source/Patch files live in the package's <package>/ dist-git subdir;
    each upstream `sources` entry becomes an http_file dropped into SOURCES under its URL
    basename (http_file needs the SHA256; the lookaside itself is keyed by SHA512).
    """

    # fails on schema mismatch
    m = SrcpkgMetadata(**meta)
    spec = "{}/{}.spec".format(package, package)
    srcs = []
    for s in m.sources:
        out = s["url"].rsplit("/", 1)[-1]
        target = "{}--{}".format(package, out)
        native.http_file(name = target, out = out, urls = [s["url"]], sha256 = s["sha256sum"])
        srcs.append(":" + target)
    srcs += native.glob(["{}/*".format(package)], exclude = [spec])
    rpm_package(
        name = package,
        package = package,
        spec = spec,
        distribution = distribution,
        srcs = srcs,
        version = m.version,
        release = m.release,
        dist = m.dist,
        source_date_epoch = m.source_date_epoch,
        subpackages = m.subpackages,
        build_requires = m.build_requires,
    )

# buildifier: disable=unnamed-macro  (fan-out macro: an rpm_package_json per branch package)
def rpm_branch(distribution: str, packages: dict) -> None:
    """Declare every package in a distro/release branch.

    The importer emits only the data (this `packages` map of name -> loaded <package>.json);
    branch-wide derivations live here in buck. All packages share one distribution."""
    for name in sorted(packages):
        rpm_package_json(package = name, distribution = distribution, meta = packages[name])
