# Tine

Tine builds native packages and composes operating-system images with [Buck2](https://buck2.build/).
Package repositories are pinned by committed snapshots. Every build action runs in an unprivileged
hermetic sandbox (no root, no containers, no network). Filesystem images are stacks of overlay deltas.
The outputs are reproducible and content-cached: deterministic archives, unified kernel images, and
dm-verity protected GPT disks.

## Requirements

The host needs Linux with unprivileged user namespaces, `jq`, `curl`, `sha256sum`, and `zstd`. Also
`/dev/kvm` for running VMs. The first invocation bootstraps the pinned Buck2 binary; everything else is
fetched and cached by Buck itself.

## Quick start

Build a minimal tar image with a few packages and custom files:

```sh
tools/buck build //examples/image:demo
```

Build a bootable GPT disk with a unified kernel image and dm-verity protected `/usr`:

```sh
tools/buck build //examples/image:boot-demo
```

Boot it in an ephemeral VM:

```sh
tools/buck run //examples/image:boot-demo-vm
```

Images are declared in ordinary `BUCK` files ([examples/image/BUCK](examples/image/BUCK) has the complete
demos):

```python
load("@tine//image:defs.bzl", "chroot", "install", "rootfs_archive")

rootfs_archive(
    name = "demo",
    package_manager = ":image.package-manager",
    ops = [
        install(["bash", "coreutils"]),
        chroot(["/usr/bin/bash", "-c", "echo built-by-tine > /etc/tine/marker"]),
    ],
    format = "tar",
)
```

## Consuming the cell

Register Tine as a [Buck2 external cell](https://buck2.build/docs/users/advanced/external_cells/) in the
consuming project's root `.buckconfig`:

```ini
[cells]
root = .
tine = tine
toolchains = toolchains
prelude = prelude
none = none

[cell_aliases]
config = prelude
# The cell's own cell_aliases are honoured even as an external cell, and it aliases fbsource to
# satisfy the bundled prelude, so `none` has to resolve here too.
fbsource = none

[external_cells]
prelude = bundled
tine = git

[external_cell_tine]
git_origin = <url of this repository>
commit_hash = <sha1 to pin>

[parser]
target_platform_detector_spec = target:root//...->prelude//platforms:default target:tine//...->prelude//platforms:default

[build]
execution_platforms = prelude//platforms:default
```

Buck fetches the pinned commit into its own cache; nothing is checked into the consuming repo. The `tine`
path is only the location the cell would occupy, and no `tine/` or `prelude/` or `none/` directory needs to
exist. Buck2 forbids nested cells inside an external cell, so the consuming project owns its own
`toolchains` cell; copy [`toolchains/BUCK`](toolchains/BUCK) as a starting point.

The consuming project also needs a Buck2 binary before it can fetch any cell, so vendor a copy of
[`tools/buck`](tools/buck) and the `buck2` entry of [`tools/tools.json`](tools/tools.json), and keep that
pin in step with this repository's.

To edit the cell in place instead of pinning it, run `tools/buck expand-external-cell tine` and comment out
the `[external_cells] tine` entry.

A consuming OS monorepo ("OS.git" in these docs) additionally holds package sources under
`packages/<distro>/<branch>/<package>`, imported and updated from upstream dist-gits (Fedora, or CentOS
Stream) by the [importer](docs/importer.md). Those build as ordinary Buck targets and feed images, so an
image build rebuilds exactly the affected packages.

## Documentation

User guides:

- [Building images](docs/images.md): host requirements, declaring images and layers, output formats, the
  VM runner
- [Building Rust projects](docs/cargo.md): building a checked-out Rust project offline, with its
  crate graph in the executables and image SBOM
- [Building Go projects](docs/go.md): the same for a checked-out Go project, whose module list go
  itself embeds in the executables
- [Maintaining packages](docs/importer.md): importing and updating packages from upstream distributions,
  local modifications, branch curation
- [Development boxes](docs/box.md): pinned interactive development environments

Design:

- [Architecture](docs/design.md): component model, decision record, current limitations, roadmap
- [Package import machinery](docs/packages.md): branch layout, metadata, consistency checks, rebuild
  strategy
- [Self-hosting approaches](docs/self-host-approaches.md): future design for the BuildRequires cycle

## Development

```sh
tools/buck build //...                     # the examples, the catalog, and the tooling
tools/buck run tine//tools:check           # lint, type-check, unit tests
tools/buck run tine//tools:fmt             # auto-format and auto-fix
tools/buck run tine//tools:verify-catalog  # assert the committed catalog lock matches
tools/ci.sh                                # the whole CI pipeline: checks, image builds, boot smokes
```

The full command list is in [AGENTS.md](AGENTS.md).
