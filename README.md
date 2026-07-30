# Tine

Tine builds native packages and composes operating-system images with [Buck2](https://buck2.build/).
Package repositories are pinned by committed snapshots. Every build action runs in an unprivileged
hermetic sandbox (no root, no containers, no network). Filesystem images are stacks of overlay deltas.
The outputs are reproducible and content-cached: deterministic archives, unified kernel images, and
dm-verity protected GPT disks.

This repository is the reusable build machinery only. Images, packages and examples live in the projects
that consume it.

## Requirements

The host needs Linux with unprivileged user namespaces, `jq`, `curl`, `sha256sum`, and `zstd`. Also
`/dev/kvm` for running VMs. The first invocation bootstraps the pinned Buck2 binary; everything else is
fetched and cached by Buck itself.

## Consuming the cell

Register Tine as a [Buck2 external cell](https://buck2.build/docs/users/advanced/external_cells/) in the
consuming project's root `.buckconfig`:

```ini
[cells]
tine = tine
toolchains = toolchains

[external_cells]
tine = git

[external_cell_tine]
git_origin = <url of this repository>
commit_hash = <sha1 to pin>
```

Buck fetches the pinned commit into its own cache; nothing is checked into the consuming repo. The `tine`
path is only the location the cell would occupy. Buck2 forbids nested cells inside an external cell, so the
consuming project owns its own `toolchains` cell; copy [`toolchains/BUCK`](toolchains/BUCK) as a starting
point.

Images are then declared in ordinary `BUCK` files:

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

A consuming OS monorepo ("OS.git" in these docs) additionally holds package sources under
`packages/<distro>/<branch>/<package>`, imported and updated from upstream dist-gits (Fedora, or CentOS
Stream) by the [importer](docs/importer.md). Those build as ordinary Buck targets and feed images, so an
image build rebuilds exactly the affected packages.

## Documentation

User guides:

- [Building images](docs/images.md): host requirements, declaring images and layers, output formats, the
  VM runner
- [Maintaining packages](docs/importer.md): importing and updating packages from upstream distributions,
  local modifications, branch curation
- [Development boxes](docs/box.md): pinned interactive development environments
- [Shared workspaces](docs/workspace.md): registering several repositories as one Buck project

Design:

- [Architecture](docs/design.md): component model, decision record, current limitations, roadmap
- [Package import machinery](docs/packages.md): branch layout, metadata, consistency checks, rebuild
  strategy
- [Self-hosting approaches](docs/self-host-approaches.md): future design for the BuildRequires cycle

## Development

Standalone, this repository builds its own targets only: the catalog, the tooling, and the test suite.

```sh
tools/buck run tine//tools:check           # lint, type-check, unit tests
tools/buck run tine//tools:fmt             # auto-format and auto-fix
tools/buck run tine//tools:verify-catalog  # assert the committed catalog lock matches
```

The full command list is in [AGENTS.md](AGENTS.md).
