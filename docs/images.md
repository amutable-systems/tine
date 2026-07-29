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

Everything below is re-exported from one facade, so a `BUCK` file needs a single load:

```python
load("@tine//image:defs.bzl", "image", "install", "rootfs_archive", "run")
```

The modules behind the facade (`image.bzl`, `compose.bzl`, and the `image_format` package) are
implementation structure and may be rearranged; load them directly only from inside the cell.

An initial image fixes one package manager for its whole lifetime; every derived image and terminal output
inherits it and its engine. Take a catalog package manager, optionally extend it with project repositories,
and create the initial image. For example, a project can expose locally built packages without adding them
to its OS release:

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
    ops = [install(["project"])],
)

image(
    name = "project-configured.image",
    parent = ":project.image",
    ops = [chroot(["/usr/bin/project", "configure"])],
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

## Images and operations

`image` has two construction modes. An initial image supplies `package_manager` or `engine`; a derived image
supplies `parent` and inherits that image's package manager and engine. A call with operations applies one
ordered operation sequence in one action and persists exactly one delta:

- `install([...])` installs native packages; `install_package_set("...")` resolves a symbolic package set
  through the image's package manager. One operation sequence may contain one install, at any position.
- `chroot([...])` executes a command with the image's own binaries, chrooted into it; `run([...])`
  executes engine tooling with the image available at `/buildroot`. Both take an `env` argument that
  overlays variables on that command's environment. Only `run` can name a build artifact, since build
  outputs are not visible inside the image: pass a declared artifact from a rule, or write
  `$(location //target)` in a BUCK file. `chroot` takes plain strings, so a shell substitution spelled
  `$(...)` reaches the shell rather than Buck's macro parser.
- `copy` introduces a declared Buck artifact at an absolute image path; `mkdir`, `symlink`, and `remove`
  mutate the same root.

`image()` recursively flattens operation lists, allowing reusable helpers to return ordered groups of
operations; `rootfs_archive`, `sysext_image`, and `bootable_disk_image` accept the same nested groups.
Materialize a complete logical image explicitly with `image_directory`.

Every `ImageInfo` carries its canonical lazy SBOM artifacts, and a package database whenever the image has
a package manager. The same
artifacts are exposed as subtargets, and `ImageSbomInfo` remains available for consumers that need only the
SBOM formats:

```text
//examples/image:chained-base
├── [pkgdb]
└── [sbom]
    ├── [spdx]
    └── [cyclonedx]
```

The `version` attribute sets the SBOM source version and defaults to `"0"`. Merely building the image still
produces only its latest delta. Selecting `[pkgdb]` runs the image's package system's capture driver;
selecting either nested SBOM format runs one shared scan of the completed stack.

`image` also takes `install_langs`: keep translated files only for these languages, instead of all of them.
Nothing matches a value that is not a language, so `install_langs = ["C.UTF-8"]` installs no translations
at all. `install_docs = False` likewise installs no documentation, keeping the licenses that packages ship.
The default initrd sets both. Both configure an install, so a layer that installs nothing ignores them.

## Terminal outputs

Logical images and terminal outputs are separate rule families. Terminal rules merge the layer stack only
when needed:

- `image_archive` writes deterministic tar or newc cpio archives, optionally zstd-compressed
  (`compression = "zstd"`), and provides `ImageArchiveInfo`, which names the format alongside the artifact;
- `image_directory` materializes a Buck directory artifact and provides `ImageDirectoryInfo`;
- `uki` builds the unified kernel image for the image's single installed kernel from one or more cpio
  `ImageArchiveInfo` dependencies and provides `UkiInfo`, named `<image_id>_<version>_<arch>.efi`
  (defaults: target name and `0`; systemd architecture spelling, e.g. `x86-64`), the shape
  systemd-sysupdate UKI transfers match. Alternative kernel command lines are `uki_profile()` descriptors,
  which add boot profiles as separate sd-boot menu entries, each appending its arguments to the base kernel
  command line; with declared `secure_boot_*` key material, the UKI and its embedded kernel are signed for
  Secure Boot and sealed with a signed expected-PCR 11 policy per profile (opt out per profile with
  `sign_expected_pcr`);
- `repart` renders ordered Starlark partition definitions and uses offline `systemd-repart` to create a
  GPT disk and independent partition artifacts in one `RepartInfo`; its disk field is absent for a
  split-only invocation;
- `disk_convert` re-encodes a raw disk with an explicitly selected engine and provides `DiskConversionInfo`;
- `bootable` selects a kernel and matching initrd from a logical image, exposed as `[uki]`, `[kernel]`,
  and `[initrd]` subtargets;
- `image_sysext` builds a systemd-sysext(8) DDI (unsigned for now) with `systemd-repart`, containing
  `/usr`, `/opt`, and `extension-release.<name>`, and provides `SysextImageInfo`; with `base`, only the
  delta layered above that image is packaged, and the extension-release pins the base's `ID`/`VERSION_ID`;
- `image_vm` runs the raw image ephemerally with its explicitly selected engine's `systemd-vmspawn`, QEMU,
  and OVMF stack,
  and binds all given `sysexts` DDIs into the guest at `/var/lib/extensions`, where systemd-sysext merges
  them at boot. With `secure_boot`, vmspawn picks Secure Boot capable firmware without pre-enrolled keys,
  so an image carrying `loader/keys/auto` enrollment files enrolls them on first boot and then boots with
  Secure Boot enforced, and attaches a software TPM so the UKI's signed expected-PCR policy is measured.

`rootfs_archive` is the composition rule for building one logical image from operations and emitting an
archive; `image_archive` remains the terminal rule for archiving an existing logical image. `sysext_image`
is the equivalent composition for a system-extension DDI. Every composition takes `install_docs` and passes
it to the image it builds.

Each composition publishes one product target that also provides its logical filesystem as `ImageInfo`.
`rootfs_archive` defaults to its archive and provides `ImageArchiveInfo`. `sysext_image` defaults to its DDI
and provides `SysextImageInfo`. Both expose their supply-chain
artifacts without conventionally named helper targets:

```text
//examples/image:demo
├── [pkgdb]
└── [sbom]
    ├── [spdx]
    └── [cyclonedx]

//examples/image:demo-ext
├── [pkgdb]
└── [sbom]
    ├── [spdx]
    └── [cyclonedx]
```

The product targets provide `ImageSbomInfo` for typed consumers. Buck builds an optional artifact only when
it is requested, so declaring these views costs nothing on a default build.

### bootable_disk_image

`bootable_disk_image()` composes the default initrd, versioned UKIs, the ESP, and a verity-protected
`/usr` into a GPT disk. Most attributes parameterize the terminal rules described above.

Required attributes:

- `package_manager` (target label): See "Declaring an image" above.
- `ops` (operation list) and `tmpfiles` (list of tmpfiles.d lines): Build the root filesystem layer;
  passed on to `image`.
- `definitions` (list of `partition()` descriptors): Partition layout; `DEFAULT_ROOT_PARTITIONS`,
  `DEFAULT_USR_VERITY_PARTITIONS`, and `DEFAULT_SIGNED_USR_VERITY_PARTITIONS` are reusable conventional
  layouts; it must contain system and ESP partitions; passed on to `repart()`.

Optional attributes:

- `disk_seed` (string): Seeds stable partition UUIDs; passed on to `repart()`.
- `verity_private_key` / `verity_certificate` (string): PEM files signing the verity signature
  partition; passed on to `repart()`.
- `secure_boot_private_key` / `secure_boot_certificate` (string): PEM pair signing the UKIs and
  systemd-boot; see "Secure Boot signing" below.
- `initrd` (target label providing `ImageInfo`): A logical image whose tree becomes the initrd, replacing
  the default initrd package image. The rule consumes the resolved provider, archives it into the
  zstd-compressed cpio itself, and republishes the package database and SBOM that image already carries.
- `cmdline` (string list): Kernel command line arguments, default
  `["root=tmpfs", "mount.usr=dissect", "rw"]`; passed on to `uki()`.
- `profiles` (`uki_profile()` descriptor list): Alternative sd-boot menu entries, passed on to `uki()`.
- `arch` (string): Architecture; only `x86_64` is supported right now; passed on to `uki()`.
- `esp_files` (dict): Map from an absolute image path (under `/boot` or `/efi`, the trees the ESP
  partition carries) to a source target copied onto the ESP.
- `install_docs` (boolean): Passed to the root filesystem layer; the default initrd never installs
  documentation regardless.
- `image_id` (string): The image identity, stamped into the image's os-release as `IMAGE_ID`.
  Defaults to the target name; a product should set it explicitly so that renaming a Buck target
  cannot re-identify the installed OS (systemd-sysupdate matches partitions and UKIs by this
  identity at run time).
- `version` (string): Declared image version. Default `"0"`; stamped into the image's os-release as
  `IMAGE_VERSION` and used as the SBOM source version. Together with `image_id`
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

The requested target name is one bootable-image result whose default output is the raw disk. Other terminal
views and supply-chain artifacts are lazy subtargets:

```text
//examples/image:boot-demo
├── [uki]
├── [directory]
├── [qcow2]
├── [raw.zst]
├── [pkgdb]
├── [sbom]
│   ├── [spdx]
│   └── [cyclonedx]
├── [initrd]
│   ├── [pkgdb]
│   └── [sbom]
│       ├── [spdx]
│       └── [cyclonedx]
├── [roothash]
└── [partitions]
    ├── [usr]
    ├── [usr-verity]
    └── [esp]
```

The disk and its re-encodings are files named `<image_id>_<version>_<arch>.<ext>`, so they keep the image
identity when copied out of the build, exactly matching the UKI's `<image_id>_<version>_<arch>.efi`.
The `[qcow2]` and `[raw.zst]` subtargets re-encode the raw disk into a compact qcow2 or a compressed raw on
demand, each publishing one `DiskConversionInfo`; the encodings are reachable only through those subtargets,
because one result cannot carry the same provider type twice. The target has no aggregate bootable-image
provider: it returns `ImageInfo`, `RepartInfo`, `InitrdInfo`, `ImageDirectoryInfo`, and `UkiInfo`
independently.
`RepartInfo` contains the optional `RootHashInfo` when verity is enabled. The completed `ImageInfo` includes
the ESP layer, so another image can use the bootable image as its parent without relying on a generated
helper label. The same `InitrdInfo`, containing its logical `ImageInfo` and derived `ImageArchiveInfo`, is
the sole typed provider published by `[initrd]`. Merely carrying an artifact in a provider does not build it;
optional actions run only when a consumer uses the artifact or a user selects its subtarget.
`[directory]` has the same representability limit as `image_directory`: it fails when the completed tree
contains a path, such as a systemd-escaped unit name, that Buck directory artifacts cannot store.

The nested metadata describes the initrd, which resolves its own package closure and may therefore contain
packages that the root filesystem does not install. Nothing scans the initrd once it is a cpio inside the
UKI's PE, so it carries its own artifacts rather than being folded into the root filesystem's. Keeping them
separate also preserves the distinction a vulnerability triage needs: a package reachable only during early
boot is not exposed the way the same package in the running system is. The union of the two accounts for
everything in the UKI, provided the image keeps the kernel package installed in its own tree, which is where
the UKI's kernel and modules come from.

### Image versioning

A version derived from the current commit or any other dynamic query cannot be computed inside the build
graph, so it gets passed as explicit build configuration. An image's BUCK file reads a config key like:

```python
version = read_config("demo", "image-version", "unversioned")
```

and the invoker computes the value outside the graph and injects it. `tine//tools:version` derives one
from git state, in three shapes against the latest `v*` tag (or 0.0.0 if there is no tag):

| build    | git state                            | version          |
|----------|--------------------------------------|------------------|
| release  | clean checkout of v1.4.2             | `1.4.2`          |
| snapshot | clean, 3 commits past the tag        | `1.4.2^3-08f2c4` |
| dev      | dirty, 86400 s after the last commit | `1.4.2^3^86400`  |

systemd version comparison orders these correctly (`tag` < `tag^count-hash` < `tag^count^seconds` <
`nexttag`), which systemd-sysupdate needs to recognize an update. The hash names the commit for tracking; it
shrinks (never below 4 characters) until the longest partition label still fits GPT's 36-character limit. A
dev build carries the seconds since the last commit instead, so every rebuild of a dirty tree gets a strictly
increasing version; `-` sorts below `^`, so dev builds are always newer than snapshot builds.

```sh
buck build -c demo.image-version="$(buck run tine//tools:version -- \
    'myos_{version}')" //your:image
```

Images without versioned labels pass plain `{version}`. That applies to the `DEFAULT_ROOT_PARTITIONS`
layout: versioned labels only serve sysupdate's A/B slot matching, and a single writable root has no
slots and mutates in place, so its labels stay systemd-repart's type defaults and the version only lands
in os-release and the UKI name.

The injected per-commit version rebuilds only the artifacts that embed it, never package installation.

## Secure Boot signing

`bootable_disk_image()` accepts a `secure_boot_private_key`/`secure_boot_certificate` PEM pair. It signs the
UKIs (including a signed expected-PCR policy, see the `uki` rule above) and the systemd-boot binaries with
`systemd-sbsign`, and `bootctl` places `loader/keys/auto/{PK,KEK,db}.auth` enrollment variables on the ESP:
firmware in setup mode enrolls the certificate on first boot and then enforces Secure Boot. The same
self-signed pair covers PE signing, the PCR policy, and enrollment.

The systemd-boot binary is signed inside the image tree, as a `.signed` sibling under
`/usr/lib/systemd/boot/efi`, sealed under the verity root hash where the booted system's `bootctl update`
finds it after an OS update; [design.md](design.md) explains why it must live there.

Development images can source the pair in two ways:

- Generated per workspace: the `signing_key` rule mints the pair at build time with `ukify genkey`, into
  buck-out; see `//examples/image-secureboot`, which consumes `:signing[cert]`/`:signing[key]`. Nothing is
  committed and no manual step is needed, but every workspace (and every build after `buck clean`) mints a
  different key, so all signed artifacts rebuild instead of coming out of caches, and each workspace's
  images enroll a different certificate.
- Committed in the consuming project: point `secure_boot_private_key`/`_certificate` at PEM files in git
  (the pattern of mkosi's `mkosi.key`/`mkosi.crt`); the attributes accept plain source files. Stable
  inputs keep the whole signed image graph cacheable, and every build enrolls the same certificate. Such
  a key is public to everyone with repository access: use it for test images only, and never enroll it on
  real hardware.

Either way, keep production signing behind a dedicated boundary (see the design plan).

## Running the image in a VM

Runtime and execution policy live on the `image_vm` target, not in the disk provider: its explicit `engine`
supplies the VM stack, its `autologin` option provisions a locked root password and runtime `login.noauth`,
and arbitrary non-secret system credentials configure settings such as first-boot locale and timezone.

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
tine/tools/buck build //examples/image:boot-demo
tine/tools/buck build '//examples/image:boot-demo[uki]'
tine/tools/buck build '//examples/image:boot-demo[partitions][usr]'
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
