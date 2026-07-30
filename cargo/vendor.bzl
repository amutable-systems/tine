"""Turn a loaded Cargo.lock's crate list into the vendored tree an offline build reads."""

load("//:specs.bzl", "spec_args")

# The attributes one crate-graph vendoring costs a rule. `assemble_vendor` names its outputs
# unconditionally, so a rule may call it once.
VENDOR_ATTRS = {
    "_vendor": attrs.exec_dep(providers = [RunInfo], default = "tine//cargo:vendor"),
}

def assemble_vendor(ctx: AnalysisContext, crates: list[dict[str, str]]) -> Artifact:
    """Declare the vendored crate tree one project's loaded Cargo.lock pins.

    The lock's checksum is the tarball's, so buck verifies each download itself.
    """
    crate_files = {}
    for crate in crates:
        name = "{}-{}.crate".format(crate["name"], crate["version"])

        # These per-download names must stay singular: the plural ones are the directories
        # collecting them.
        artifact = ctx.actions.declare_output("crate", name, has_content_based_path = True)
        ctx.actions.download_file(artifact, crate["url"], sha256 = crate["sha256"])
        crate_files[name] = artifact

    vendor = ctx.actions.declare_output("vendor", dir = True)
    ctx.actions.run(
        cmd_args(
            ctx.attrs._vendor[RunInfo],
            spec_args(
                ctx,
                "cargo-vendor.spec.json",
                {
                    "crates": ctx.actions.copied_dir("crates", crate_files),
                    "out": vendor.as_output(),
                },
            ),
        ),
        category = "cargo_vendor",
    )
    return vendor
