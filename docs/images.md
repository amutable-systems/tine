# Building images

How to declare, build, and run OS images. This is the user guide. The architecture behind it (the
provider vocabulary, the delta layer model, and disk composition internals) is documented in
[design.md](design.md).

## Host requirements

The host contract is intentionally small:

- the pinned Buck2 binary and its bundled prelude;
- the pinned bootstrap Python used to run the minimal extractor and development tools;
- unprivileged user namespaces and the filesystem/kernel facilities required by mkosi-sandbox/overlayfs;
- `/dev/kvm` only when running a VM target.

The Buck bootstrap needs `jq`, `curl`, `sha256sum`, and `zstd`. It verifies and caches the pinned Buck binary
under `${XDG_CACHE_HOME:-$HOME/.cache}/tine/buck2`; cached invocations work offline.

## Concepts

A **catalog** is a Buck package that declares which OS releases are available to build against. For each
release it bundles the repository definitions, the release identity and its package sets, a package
manager (the pinned solve environment that images start from), a buildroot for package builds, and an
engine. The default catalog is [`tine//catalog`](../catalog/BUCK) and currently declares three releases:

- `fedora.rawhide`: pinned to an rpmrepo compose snapshot, so packages never vanish underneath the pins
- `fedora.44`
- `centos.10-stream`

Each release consists of targets named `<family>.<release>.<role>`, for example
`tine//catalog:fedora.rawhide.package-manager` or `tine//catalog:centos.10-stream.release`. The pins live
as committed snapshots under [`tine/catalog/snapshot/`](../catalog/snapshot/): repository metadata in
`snapshot/repo/*.json` and frozen engine transactions in `snapshot/engine/*.json`. Normal builds therefore
never touch the network; `refresh-catalog` (below) advances the pins. A project can instead declare its
own `//catalog` package with the same macros. The naming scheme and the pinning mechanism are described in
[design.md](design.md).

An **engine** is a pinned, reproducible execution environment that runs every build action. It supplies
rpm, Python, libdnf5, `createrepo_c`, core utilities, and the image assembly and VM tools; these tools
stay in the engine and out of the built images. All current releases share
`tine//catalog:fedora.rawhide.engine`. An engine's base release only records where its userspace came
from: the Rawhide engine also serves Fedora 44 and CentOS Stream. How an engine bootstraps itself is
described in [design.md](design.md).

## Commands

The wrapper commands work from anywhere in the root project:

```sh
tine/tools/buck run tine//tools:refresh-catalog
tine/tools/buck run tine//tools:verify-catalog
tine/tools/buck run tine//tools:fmt
tine/tools/buck run tine//tools:check
```

`refresh-catalog` refreshes the default `tine//catalog` package. Pass another catalog package after `--`,
for example `tine/tools/buck run tine//tools:refresh-catalog -- my_project//catalog`. `verify-catalog`
performs the same generation and fails when the committed JSON differs. The pinning and refresh mechanism
is described in [design.md](design.md).

## Declaring an image

An image normally fixes one package manager for its whole lifetime; every layer and terminal output
inherits it and its engine. Take a catalog package manager, optionally extend it with project
repositories, bootstrap the image, and apply layers. For example, a project can expose locally built
packages without adding them to its OS release:

```python
local_repository(
    name = "project.repository",
    packages = ["//packages:project"],
)

package_manager(
    name = "project.package-manager",
    base = "tine//catalog:fedora.rawhide.package-manager",
    additional_repositories = [":project.repository"],
)

image(
    name = "project.image",
    package_manager = ":project.package-manager",
)

image_layer(
    name = "project.layer",
    parent = ":project.image",
    ops = [install(["project"])],
)
```

A package manager may instead attach a branch's generated local-packages universe:

```python
package_manager(
    name = "image.package-manager",
    base = "tine//catalog:fedora.rawhide.package-manager",
    local_packages = "//packages/fedora/rawhide:_local_packages",
)
```

Each install then builds exactly the locally built packages in its runtime closure and offers them to the
solver ahead of the upstream repositories; requested capabilities without a local provider continue to
resolve upstream (details in [design.md](design.md)).

## Layers and operations

`image` bootstraps an empty logical image. `image_layer` applies one ordered operation sequence in one
action and persists exactly one delta:

- `install([...])` installs native packages; `install_package_set("...")` resolves a symbolic package set
  through the parent image's package manager. A layer may contain one install operation, at any position.
- `run([...])` executes a command with the image's own binaries in a chroot; `chroot = False` instead
  executes engine tooling with the image available at `/buildroot`. Its `env` argument overlays variables
  on the engine or image environment for that command.
- `copy` introduces a declared Buck artifact at an absolute image path; `mkdir`, `symlink`, and `remove`
  mutate the same root.

Operation lists are recursively flattened, allowing reusable helpers to return ordered groups of
operations. Every `image_layer` exposes a `directory` subtarget which lazily materializes the complete
logical image at that point.

## Terminal outputs

Logical images and terminal outputs are separate rule families. Terminal rules merge the layer stack only
when needed:

- `image_archive` writes deterministic tar or newc cpio archives, optionally zstd-compressed
  (`compression = "zstd"`), and provides `CpioArchiveInfo` for the latter;
- `image_directory` materializes a Buck directory artifact and provides `DirectoryImageInfo`;
- `uki` builds the unified kernel image for the image's single installed kernel from one or more
  `CpioArchiveInfo` dependencies, named `<image_id>_<version>_<arch>.efi` (defaults: target name and
  `0`; systemd architecture spelling, e.g. `x86-64`), the shape systemd-sysupdate UKI transfers
  match. Alternative kernel command lines are `uki_profile()` records, which add boot profiles as separate
  sd-boot menu entries, each appending its arguments to the base kernel command line;
- `repart` renders ordered Starlark partition definitions and uses offline `systemd-repart` to create a
  GPT disk with `DiskImageInfo`, independent partition artifacts with `split = True`, or both;
- `bootable` selects a kernel and matching initrd from a logical image and provides `BootableImageInfo`;
- `image_rpmdb` copies the image's rpm database out as a separate artifact, trimmed to the `Packages`
  table alone, and provides `RpmdbInfo`;
- `image_sbom` runs `syft` over the assembled tree in one scan, emitting SPDX and CycloneDX SBOMs and
  providing `SbomInfo`;
- `image_sysext` builds a systemd-sysext(8) DDI (unsigned for now) with `systemd-repart`, containing
  `/usr`, `/opt`, and `extension-release.<name>`, and provides `SysextImageInfo`; with `base`, only the
  delta layered above that image is packaged, and the extension-release pins the base's `ID`/`VERSION_ID`;
- `image_result` aggregates independent facets of the same logical image without creating another
  artifact;
- `image_vm` runs the raw image ephemerally with the engine's `systemd-vmspawn`, QEMU, and OVMF stack,
  and binds all given `sysexts` DDIs into the guest at `/var/lib/extensions`, where systemd-sysext merges
  them at boot. With `secure_boot`, vmspawn picks Secure Boot capable firmware without pre-enrolled keys,
  so an image carrying `loader/keys/auto` enrollment files enrolls them on first boot and then boots with
  Secure Boot enforced, and attaches a software TPM so the UKI's signed expected-PCR policy is measured.

`rootfs_archive()` is the convenience composition for building a single layer from operations and emitting
an archive; `image_archive` remains the terminal rule for archiving an existing logical image.
`sysext_image()` is the equivalent composition for a system-extension DDI.

The rpm database and SBOM facets can also ride along on a normal build: `image_archive` (and
`rootfs_archive`) accept `rpmdb`/`sbom` flags that fold the artifacts into `other_outputs`,
`sysext_image` likewise accepts `rpmdb`, and `bootable_disk_image` exposes both as `image_result` facets,
for the root filesystem and the initrd each.

### bootable_disk_image

`bootable_disk_image()` composes the default initrd, versioned UKIs, the ESP, and a verity-protected
`/usr` into a GPT disk. Most attributes parameterize the terminal rules described above.

Required attributes:

- `package_manager` (target label): See "Declaring an image" above.
- `ops` (operation list) and `tmpfiles` (list of tmpfiles.d lines): Build the root filesystem layer;
  passed on to `image_layer`.
- `definitions` (list of `partition()` records): Partition layout; `DEFAULT_ROOT_PARTITIONS`,
  `DEFAULT_USR_VERITY_PARTITIONS`, and `DEFAULT_SIGNED_USR_VERITY_PARTITIONS` are reusable conventional
  layouts; it must contain system and ESP partitions; passed on to `repart()`.

Optional attributes:

- `disk_seed` (string): Seeds stable partition UUIDs; passed on to `repart()`.
- `verity_private_key` / `verity_certificate` (string): PEM files signing the verity signature
  partition; passed on to `repart()`.
- `initrd` (target label): A logical image whose tree becomes the initrd, replacing the default
  initrd package image. The composition archives it into the cpio itself.
- `cmdline` (string list): Kernel command line arguments, default
  `["root=tmpfs", "mount.usr=dissect", "rw"]`; passed on to `uki()`.
- `profiles` (`uki_profile()` record list): Alternative sd-boot menu entries, passed on to `uki()`.
- `arch` (string): Architecture; only `x86_64` is supported right now; passed on to `uki()`.
- `esp_files` (dict): Map from an absolute image path (under `/boot` or `/efi`, the trees the ESP
  partition carries) to a source target copied onto the ESP.
- `rpmdb` (boolean): Attach the `image_rpmdb` facets, exposed as the `[rpmdb]` and `[initrd.rpmdb]`
  subtargets.
- `sbom` (boolean): Attach the `image_sbom` facets, exposed as the `[sbom]` and `[initrd.sbom]`
  subtargets.
- `image_id` (string): The image identity, stamped into the image's os-release as `IMAGE_ID`.
  Defaults to the target name; a product should set it explicitly so that renaming a Buck target
  cannot re-identify the installed OS (systemd-sysupdate matches partitions and UKIs by this
  identity at run time).
- `version` (string): Declared image version. Default `"0"`; stamped into the image's os-release as
  `IMAGE_VERSION` and passed on to `image_sbom` as the SBOM source version. Together with `image_id`
  it also names the UKI (`<image_id>_<version>_<arch>.efi`) and renders partition label placeholders.

The identity stamp is applied in its own thin layer between the root filesystem layer and everything
derived from it, so building the same target with a different `version` re-runs only the artifacts
that embed the version (`/usr` partition and verity, UKI, ESP, disk) while package installation and
the caller's operations stay cached.

The version carries a contract: systemd-sysupdate identifies an update *purely* by the version in the
partition labels and the UKI filename, so every published build must carry a new, higher version. A
rebuild under an unchanged version puts different content behind identical names, which sysupdate
cannot distinguish from the release a device already installed, and so never applies. The release pipeline
that publishes update artifacts must enforce version immutability by rejecting an already-published version.

A final target may return several independent facets; one supplies the
target's default output, and nested subtargets namespace all other views:

```text
//examples/image:boot-demo[bootable][uki]
//examples/image:boot-demo[bootable][kernel]
//examples/image:boot-demo[bootable][initrd]
//examples/image:boot-demo[disk][roothash]
//examples/image:boot-demo[disk][partitions][usr]
//examples/image:boot-demo[disk][partitions][esp]
//examples/image:boot-demo[directory]
//examples/image:boot-demo[qcow2]
//examples/image:boot-demo[raw.zst]
//examples/image:boot-demo[rpmdb]
//examples/image:boot-demo[sbom]
//examples/image:boot-demo[initrd.rpmdb]
//examples/image:boot-demo[initrd.sbom]
```

The `[qcow2]` and `[raw.zst]` subtargets re-encode the raw disk into a compact qcow2 or a compressed raw on
demand; Buck only runs the conversion actually requested, so they add nothing to a default build.

The `initrd.*` subtargets describe the initrd, which resolves its own package closure and may therefore
contain packages that the root filesystem does not install. Nothing scans the initrd once it is a cpio inside
the UKI's PE, so it carries its own artifacts rather than being folded into the root filesystem's. Keeping
them separate also preserves the distinction a vulnerability triage needs: a package reachable only during
early boot is not exposed the way the same package in the running system is. The union of the two accounts
for everything in the UKI, provided the image keeps the kernel rpm installed in its own tree, which is where
the UKI's kernel and modules come from.

## Running the image in a VM

Runtime policy lives on the `image_vm` target, not in the image: its `autologin` option provisions a
locked root password and runtime `login.noauth`, and arbitrary non-secret system credentials configure
settings such as first-boot locale and timezone.

```sh
tine/tools/buck run //examples/image:boot-demo-vm
```

The example image uses a tmpfs root with `mount.usr=dissect`; SELinux is disabled because the build does not
yet produce filesystem labels. Ephemeral mode preserves the Buck disk artifact.

## Example targets

Representative builds are:

```sh
tine/tools/buck build //packages/fedora/rawhide:zlib-ng
tine/tools/buck build //examples/image:demo
tine/tools/buck build //examples/image:layered-install
tine/tools/buck build '//examples/image:layered-install.layer[directory]'
tine/tools/buck build //examples/image:boot-demo
tine/tools/buck build '//examples/image:boot-demo[bootable][uki]'
tine/tools/buck build '//examples/image:boot-demo[disk][partitions][usr]'
tine/tools/buck build //examples/image:demo-ext
tine/tools/buck build //examples/image-local-packages:image
```

The first validates package import, package-manager selection, buildroot assembly, and RPM collection.
Packages with `buildroot_deps` additionally exercise local-package preference. The image targets validate
package installation and commands sharing one delta, incremental layering, archive packing, versioned UKI
creation, semantic boot-artifact extraction, ESP-layer assembly, and disk composition. `demo-ext` is a
system-extension DDI on top of `boot-demo`. Running `boot-demo-vm` validates the interactive VM runner; it
exposes `demo-ext` under `/var/lib/extensions` in the guest, which validates the sysext merge at boot. The
`//packages/...` and `//examples/image-local-packages` targets need the package sources of a vendoring OS
monorepo; the other targets also build from a standalone checkout.

## Updating pinned tools

`bump` refreshes the pinned tool releases against their upstream GitHub releases.

Update one or more pinned tools by name (`buck2`, `python3`, `buildifier`, `ruff`, `ty`, `syft`):

```sh
tine/tools/buck run tine//tools:bump -- --tool ruff --tool ty
```

Update all tools:

```sh
tine/tools/buck run tine//tools:bump -- --all
```

Each tool is resolved to its latest upstream release and its `url` and `sha256` are rewritten in place.
Add `--commit` to record the result as a git commit whose message itemizes each update.

python3 minor version stays pinned in pyproject.toml; updating to a new minor release stays a deliberate
manual change.
