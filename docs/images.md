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
engine. The default catalog is [`tine//catalog`](../catalog/BUCK) and currently declares two releases:

- `fedora.rawhide`: pinned to an rpmrepo compose snapshot, so packages never vanish underneath the pins
- `fedora.44`

Every one of them is pinned to a mirror that publishes immutable snapshots, which is what a release has to
have to be buildable from a committed pin at all.

Each release consists of targets named `<family>.<release>.<role>`, for example
`tine//catalog:fedora.rawhide.package-manager` or `tine//catalog:fedora.44.release`. The pins live
as committed snapshots under [`catalog/snapshot/`](../catalog/snapshot/): repository metadata in
`snapshot/repo/*.json` and frozen engine transactions in `snapshot/engine/*.json`. Normal builds therefore
never touch the network; `refresh-catalog` (below) advances the pins. A project can instead declare its
own `//catalog` package with the same macros. The naming scheme and the pinning mechanism are described in
[design.md](design.md).

An **engine** is a pinned, reproducible execution environment that runs every build action. It supplies
rpm, Python, libdnf5, `createrepo_c`, core utilities, and the image assembly and VM tools; these tools
stay in the engine and out of the built images. All current releases share
`tine//catalog:fedora.rawhide.engine`. An engine's base release only records where its userspace came
from: the Rawhide engine also serves Fedora 44. How an engine bootstraps itself is
described in [design.md](design.md).

## Commands

The wrapper commands work from anywhere in the root project:

```sh
tools/buck run tine//tools:refresh-catalog
tools/buck run tine//tools:verify-catalog
tools/buck run tine//tools:fmt
tools/buck run tine//tools:check
```

`refresh-catalog` refreshes the default `tine//catalog` package. Pass another catalog package after `--`,
for example `tools/buck run tine//tools:refresh-catalog -- my_project//catalog`. `verify-catalog`
performs the same generation and fails when the committed JSON differs. The pinning and refresh mechanism
is described in [design.md](design.md).

## Declaring an image

Everything below is re-exported from one facade, so a `BUCK` file needs a single load:

```Starlark
load("@tine//image:defs.bzl", "image", "install_packages", "rootfs_archive", "run")
```

The modules behind the facade (`image.bzl`, `compose.bzl`, and the `image_format` package) are
implementation structure and may be rearranged; load them directly only from inside the cell.

An initial image fixes one package manager for its whole lifetime; every derived image and terminal output
inherits it and its engine. Take a catalog package manager, optionally extend it with project repositories,
and create the initial image. For example, a project can expose locally built packages without adding them
to its OS release:

```Starlark
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
    ops = [install_packages(["project"])],
)

image(
    name = "project-configured.image",
    parent = ":project.image",
    ops = [run(["/usr/bin/project", "configure"], chroot = True)],
)
```

A package manager may instead attach a branch's generated local-packages universe:

```Starlark
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

- `install_packages([...])` installs native packages; `install_package_set("...")` resolves a symbolic package set
  through the image's package manager. One operation sequence may contain one install, at any position.
- `run([...])` executes a command against the image. By default the engine supplies the userspace and
  the image is mounted at `/buildroot`; with `chroot = True` the command runs inside the image with its
  own binaries instead. Either takes an `env` argument that overlays variables on that command's
  environment. An argument names a build artifact by being one, or by spelling `$(location //target)` in
  a BUCK file, and it resolves the same in both modes: a chrooted command gets the project bind-mounted
  at a fixed path under `/run`, which is also its working directory. Running a script the repository
  owns is therefore just `run(["/usr/bin/bash", "$(location :setup.sh)"], chroot = True)`. Buck's macro
  parser claims `$(...)`, so a shell substitution has to be written `\$(...)`; an unescaped one fails to
  parse rather than silently reaching the shell.
- `python([...])` runs a python script against the image without the image needing python: it is a `run`
  of the relocatable interpreter Buck already pins for its own bootstrap, named through the project like
  any other artifact. It takes the same `env` and `chroot` arguments, so the script either sees the image
  at `/buildroot` or has it as its own root; in the chrooted case nothing of the interpreter reaches the
  delta, because the project bind carrying it lives under `/run`.
- `copy` introduces a declared Buck artifact at an absolute image path; `mkdir`, `symlink`, and `remove`
  mutate the same root.
- `depmod()`, `hwdb()` and `locale_gen()` build the state installed packages only describe (below).
- `install_from` applies the operations another target attaches to itself (see below).

`image()` recursively flattens operation lists, allowing reusable helpers to return ordered groups of
operations; `rootfs_archive`, `sysext_image`, and `bootable_disk_image` accept the same nested groups.
Materialize a complete logical image explicitly with `image_directory`.

### Generating what packages only describe

Installing a package leaves state described but not built: modprobe reads depmod's binary indexes,
udev reads a compiled hardware database and never the sources beside it, and some distributions
generate their locale archive from a list. On a running system scriptlets and boot-time units produce
that; an image being assembled has neither, so each generator is an operation of its own, placed after
whatever it acts on. The compositions run all three for you (below); an `image()` places them itself:

```Starlark
image(
    name = "appliance",
    package_manager = ":image.package-manager",
    ops = [
        install_package_set("bootable"),
        install_from(":project.install"),
        depmod(),
        hwdb(),
    ],
)
```

- `depmod()` rebuilds `modules.dep` and its `.bin` indexes for every kernel the image installs, and
  does nothing for an image that installs none. It runs the image's own `depmod`, because the tool
  reads its search-order configuration from absolute paths that `--basedir` does not move, and writes
  index files whose compatibility is its own kmod's business. An image with modules but no `depmod`
  fails the build by name rather than shipping a stale index.
- `hwdb(usr = True, strict = True)` compiles `hwdb.d` into the binary database udev actually reads. It
  writes `/usr/lib/udev/hwdb.bin`, where the image ships it and nothing writable shadows it, and drops
  the `/etc` copy; `usr = False` writes that copy instead. `strict = False` accepts a source file the
  image cannot parse. An image with no `hwdb.d` is left alone.
- `locale_gen()` runs the image's own `locale-gen` when `/etc/locale.gen` asks for something, which is
  how Debian and Arch generate locales; a distribution that ships them as packages has no such file and
  the operation does nothing.

`hwdb()` is the one that runs an engine tool against the mounted image, exactly as `run()` does by
default, so an image that installs no systemd of its own still gets a database; the other two must be
the image's own. What they write is captured by the layer that runs them, so it is built once and
cached rather than repeated by every terminal output.

`rootfs_archive` and `bootable_disk_image` (its root filesystem and its default initrd both) end their
operations with `depmod()`, `hwdb()` and `locale_gen()` at their defaults, because a composition builds a
whole product rather than one layer: the three cover everything a package can leave described, and each
does nothing on an image carrying none of it. Naming one in `ops` overrides its copy rather than adding
a second, so a composition takes `hwdb(usr = False)`, or a `depmod()` placed before the operations that
strip modules, exactly as written. `sysext_image` runs none of them: an extension merges onto a system it
does not own, where a database built from the extension's own tree would shadow that system's while
describing only what the extension carries.

### Attaching install operations to a target

How a project installs is a property of the project, not of each image carrying it. `image_install()`
attaches operations to a target, and an image applies them with one `install_from()`:

```Starlark
image_install(
    name = "project.install",
    ops = [
        copy(":project[project-cli]", "/usr/bin/project-cli"),
        copy(":project.checkout[tmpfiles.d]", "/usr/lib/tmpfiles.d"),
    ],
)

bootable_disk_image(
    name = "os",
    ops = [
        install_packages([...]),
        install_from(":project.install"),
    ],
    ...
)
```

Any operation may be attached, not only copies, and the operations are spliced in place, so the image
still decides where in its own order they land. An `image_install` target may itself `install_from()`
another, which composes; a cycle is rejected by Buck as a target cycle.

### Installing a file a project ships as a template

A project that expects its build system to fill in a prefix or a port commits the file with markers and a
`sed` in its install recipe. `substitute()` runs that expansion as a build action, so the values live in
the declaration that installs the file and the result is an artifact like any other:

```Starlark
substitute(
    name = "project-http.service",
    src = ":project.checkout[contrib/project-http.service.in]",
    replacements = {"@bindir@": "/usr/bin", "@port@": "555"},
)

copy(":project-http.service", "/usr/lib/systemd/system/project-http.service")
```

The output is named after the target. Each placeholder has to appear in the template, so a project
renaming one fails the build rather than leaving a marker in an installed file, and the template's mode
carries over, so a substituted script stays executable.

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
  systemd-sysupdate UKI transfers match. After those dependencies it appends one further initrd of its
  own, holding the kernel modules `initrd_modules` selects, so that none of the initrds handed to it has
  to carry modules for the kernel in question (see "Kernel modules in the UKI"). Alternative kernel
  command lines are `uki_profile()` descriptors,
  which add boot profiles as separate sd-boot menu entries, each appending its arguments to the base kernel
  command line; with a `secure_boot_key`, the UKI and its embedded kernel are signed for Secure Boot, and
  with a `sign_expected_pcr_key` they are sealed with a signed expected-PCR 11 policy per profile (opt out
  per profile with `sign_expected_pcr`);
- `repart` renders ordered Starlark partition definitions and uses offline `systemd-repart` to create a
  GPT disk and independent partition artifacts in one `RepartInfo`; its disk field is absent for a
  split-only invocation, `output_size` composes the disk with free space behind its partitions, and
  `strip_pkgdb` leaves the package database out of them, and `mkfs_options` tunes the filesystems it
  creates;
- `disk_convert` re-encodes a raw disk with an explicitly selected engine and provides `DiskConversionInfo`;
- `bootable` selects a kernel and matching initrd from a logical image, exposed as `[uki]`, `[kernel]`,
  and `[initrd]` subtargets;
- `image_sysext` builds a systemd-sysext(8) DDI with `systemd-repart`, containing `/usr`, `/opt`, and
  `extension-release.<name>`, and provides `SysextImageInfo`; with `base`, only the delta layered above that
  image is packaged, and the extension-release pins the base's `ID`/`VERSION_ID`; with `verity_key` (a
  target providing `SigningKeyInfo`), the DDI carries a signature over its verity root hash, which a host
  validates against the key's certificate in its `/usr/lib/verity.d/` (enforced only where the host's
  sysext image policy says so, see "Example targets" below);
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
- `verity_key` (target providing `SigningKeyInfo`): Signs the verity signature partition; passed on to
  `repart()`.
- `secure_boot_key` (target providing `SigningKeyInfo`): Signs the UKIs and systemd-boot; see "Secure
  Boot signing" below.
- `output_size` (size string such as `"20G"`): Ships the disk at this size instead of at the size its
  partitions need, so an installed system finds room past them and does not have to resize its medium
  before first boot; passed on to `repart()`. The partitions keep the sizes their definitions ask for and
  the composed file is enlarged behind the last one, so the added room costs nothing on disk and, as with
  `image_vm`'s `grow`, sits past the GPT backup header until something rewrites the table. A size the
  partitions do not fit in fails the build.
- `mkfs_options` (dict of filesystem to option list, default `{}`): Options `mkfs` is given when
  creating a partition of that filesystem, passed on to `repart()`, which hands each list to
  `systemd-repart` as `SYSTEMD_REPART_MKFS_OPTIONS_<FSTYPE>`. Naming a filesystem the disk never formats
  fails the build rather than going nowhere, and an option holding whitespace is refused because repart
  splits the variable on it. This is where a disk's compression is really decided: a `partition()`'s
  `compression` only picks the algorithm, so an erofs partition left at the defaults comes out
  substantially larger than one built the way `image_sysext` builds its own, which is
  `["-zzstd,level=3", "-C524288", "-Efragments,ztailpacking,dedupe"]`. `["--invariant"]` for vfat makes an
  ESP byte-stable across rebuilds, which `mkfs.fat` otherwise is not, because it stamps the volume label
  entry with the wall clock even under `SOURCE_DATE_EPOCH`.
- `strip_pkgdb` (bool, default `False`): Leaves the package database out of the system partitions, for a
  system that ships without its package manager and never resolves a package again; passed on to
  `repart()`. The logical image keeps it, so `[pkgdb]` still captures the database and `[sbom]` still
  reports every installed package rather than what a binary scan can guess. The ESP carries no database
  either way.
- `sign_expected_pcr_key` (target providing `SigningKeyInfo`): Seals the expected-PCR policy; without one
  the policy is not sealed. See "Secure Boot signing" below.
- `initrd` (target label providing `ImageInfo`): A logical image whose tree becomes the initrd, replacing
  the default initrd package image. The rule consumes the resolved provider, archives it into the
  zstd-compressed cpio itself, and republishes the package database and SBOM that image already carries.
  The cpio removes the package database, since nothing in an initrd reads it; `[initrd][pkgdb]` still
  captures it from the image's own tree. It needs no kernel modules: `initrd_modules` selects those and
  the UKI carries them in an initrd of its own.
- `cmdline` (string list): Kernel command line arguments, default
  `["root=tmpfs", "mount.usr=dissect", "rw"]`; passed on to `uki()`.
- `initrd_modules` (glob pattern list): The kernel modules the UKI carries, default
  `DEFAULT_INITRD_MODULES`; see "Kernel modules in the UKI"; passed on to `uki()`.
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
│   └── [modules]
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

### Kernel modules in the UKI

A UKI carries the kernel, the initrds it was built from, and one further initrd the `uki` rule assembles
for that kernel alone, holding kernel modules. `initrd_modules` selects those modules from the image's own
`/usr/lib/modules/<kver>`, and the build adds what they depend on and the firmware they ask for. This
initrd is appended after the ones passed in, so no initrd has to be built for a particular kernel: an
image given to `bootable_disk_image` as `initrd` needs no kernel modules of its own, and gets the ones
selected here.

The default is `DEFAULT_INITRD_MODULES`, exported from `//image:defs.bzl`. It is a core set rather than
every module the kernel package ships, because only the path to `/usr` has to work from the UKI: `/usr`
keeps the complete set, the erofs partition compresses it, and the system loads any other module from
there once it has switched root. The core set covers the usual ways a machine reaches its root
filesystem, meaning AHCI, NVMe, SCSI and USB storage, the virtio devices a VM is given, device-mapper
including dm-verity, the filesystems these images use, and keyboard and console input so that a rescue
shell works. It does not cover enterprise storage controllers such as SAS and RAID adapters, a root
filesystem reached over the network, or any device whose driver needs firmware. An appliance that boots
through one of those has to name it:

```Starlark
bootable_disk_image(
    # The core set, plus a controller this appliance boots from, minus a filesystem it never mounts.
    initrd_modules = DEFAULT_INITRD_MODULES + ["mpt3sas", "-btrfs"],
)
```

Patterns written without `DEFAULT_INITRD_MODULES` replace it rather than extending it: `["*"]` carries
every module the image installs, and `[]` carries none.

A pattern that matches no module is reported rather than fatal, counted on stderr and named in the
manifest below, because one pattern list meets kernels that ship different sets of modules and an entry
naming something a given kernel does not have is ordinary. Naming a module the kernel builds in counts
as a match and packs nothing, since the kernel already holds it.

Each pattern is matched against a module's path below `/usr/lib/modules/<kver>`, with its `.ko`, `.ko.gz`,
`.ko.xz` or `.ko.zst` suffix removed and `_` and `-` treated as one character, as kmod treats them:

| pattern              | matches                                                            |
|----------------------|--------------------------------------------------------------------|
| `loop`               | the basename                                                       |
| `block/loop`         | a trailing run of path components                                  |
| `/kernel/block/loop` | the whole path, anchored at the root of the module directory       |
| `crypto/`            | everything below that directory                                    |
| `raid[0-9]*`         | shell globs (`*` crosses `/`)                                      |
| `-nouveau`           | excludes; patterns are evaluated in order and the last match wins  |

Firmware follows the modules: whatever a selected module declares is packed alongside it, symlinks
included, as is the firmware of modules the kernel has built in, since those load it from the initrd too.
An image that installs no firmware therefore ships none, and one that installs `linux-firmware` and asks
for every module gets all of it.

`[uki][modules]` is the record of that whole decision, as JSON, and it is where to start when a UKI boots
to no root. Alongside the kernel version and the patterns the target asked for, it names the ones that
matched nothing, the dependencies the image does not install and the firmware nothing satisfies, then
totals the modules, the firmware, the content bytes and the size of the archive itself. Every packed path is listed with what it is (`module`, `firmware`, `index`, `vdso` or `directory`),
its size, and why it is in there: `selected` for one a pattern named, `needed_by` for one the closure
pulled in, and `declared_by` for firmware, each naming the modules responsible. The driver prints only
counts as it builds, since the names are all here.

### Image versioning

A version derived from the current commit or any other dynamic query cannot be computed inside the build
graph, so it gets passed as explicit build configuration. An image's BUCK file reads a config key like:

```Starlark
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

`bootable_disk_image()` accepts a `secure_boot_key`, which names a target providing `SigningKeyInfo` (the
private key and its certificate). It signs the UKIs and the systemd-boot binaries with `systemd-sbsign`, and
`bootctl` places `loader/keys/auto/{PK,KEK,db}.auth` enrollment variables on the ESP: firmware in setup mode
enrolls the certificate on first boot and then enforces Secure Boot. That key covers PE signing and
enrollment.

`sign_expected_pcr_key` seals the expected-PCR policy the UKIs carry (see the `uki` rule above). It requires
Secure Boot signing, which signs the UKI whose measurements it seals. Each role names its own key, so
sealing a policy with the Secure Boot key is something a caller spells out rather than gets by default. The
two authorize different things: one says which boot binaries firmware may load, the other which measured
boot states may unseal TPM secrets. Keeping them apart bounds a compromise of either one, and means only the
policy key has to be reachable to re-seal a policy. For a key in the build graph its certificate goes
unused, because ukify derives the `.pcrpkey` section from the private key; a key behind a provider needs
one, as [signing-pkcs11.md](signing-pkcs11.md) describes.

The systemd-boot binary is signed inside the image tree, as a `.signed` sibling under
`/usr/lib/systemd/boot/efi`, sealed under the verity root hash where the booted system's `bootctl update`
finds it after an OS update; [design.md](design.md) explains why it must live there.

Development images can source a key in two ways:

- `generate_signing_key()` mints a pair at build time with `ukify genkey`, into buck-out; see
  `//examples/image-secureboot`. Nothing is committed and no manual step is needed. Every workspace, and
  every build after `buck clean`, mints a different key, so all signed artifacts rebuild and each
  workspace's images enroll a different certificate.
- `pem_signing_key()` adopts PEM files committed in the consuming project. Stable inputs keep the whole
  signed image graph cacheable, and every build enrolls the same certificate. Such a key is public to
  everyone with repository access: use it for test images only, and never enroll it on real hardware.

A production build should sign with `pkcs11_signing_key()` or `config_signing_key()`, where the key is
held by a PKCS#11 token and the build reaches it over a socket without ever seeing the key material. See
[signing-pkcs11.md](signing-pkcs11.md).

## Running the image in a VM

Runtime and execution policy live on the `image_vm` target, not in the disk provider: its explicit `engine`
supplies the VM stack, its `autologin` option provisions a locked root password and runtime `login.noauth`,
and arbitrary non-secret system credentials configure settings such as first-boot locale and timezone.

`grow` enlarges the disk file to a given size before boot (vmspawn's `--grow-image`), which is how an
ephemeral guest is given room the built partitions do not occupy; the guest claims that room itself, for
example with `systemd-repart`. vmspawn grows the built artifact in place, and the added space sits past the
GPT backup header until something rewrites the table. Use it to give a guest more room than the shipped
disk carries; a disk that should always carry that room sets `output_size` on the image instead.

```sh
tools/buck run //examples/image:boot-demo-vm.fedora
```

The example image uses a tmpfs root with `mount.usr=dissect`; SELinux is disabled because the build does not
yet produce filesystem labels. Ephemeral mode preserves the Buck disk artifact.

## Example targets

Representative builds are:

```sh
tools/buck build //examples/image:demo.fedora
tools/buck build //examples/image:layered-install.fedora
tools/buck build //examples/image:boot-demo.fedora
tools/buck build '//examples/image:boot-demo.fedora[uki]'
tools/buck build '//examples/image:boot-demo.fedora[partitions][usr]'
tools/buck build //examples/image:demo-ext.fedora
```

These validate package installation and commands sharing one delta, incremental layering, archive packing,
versioned UKI creation, semantic boot-artifact extraction, ESP-layer assembly, and disk composition.
`demo-ext` is a system-extension DDI on top of `boot-demo`. Running `boot-demo-vm` validates the interactive
VM runner; it exposes `demo-ext` under `/var/lib/extensions` in the guest, which validates the sysext merge
at boot.

The signed variant lives in `//examples/image-secureboot`: its `demo-ext` signs the verity root hash with a
generated key, the image ships that key's certificate in `/usr/lib/verity.d/` and a
`/usr/lib/systemd/sysext.conf` whose `ImagePolicy` grants `signed` only, and its `vm-smoke` proves both
directions — the signed extension merges, and the unsigned `demo-ext-unsigned` DDI is refused. Without such
a policy the signature is decorative: systemd-sysext's default policy merges unsigned images, and falls back
to plain verity when a signature fails to validate.

### Choosing a distribution

An image's distribution is a configuration its target carries, so one declaration serves every
distribution the catalog offers. An image rule names itself once per distribution its package
serves, so declaring `boot-demo` also declares `boot-demo.fedora` with nothing further to write. A
rule tine does not own says so itself:

```Starlark
distribution_alias(
    name = "boot-demo-vm-smoke.fedora",
    actual = ":boot-demo-vm-smoke",
    distribution = "//catalog:<family>.<release>.distribution",
)
```

An image can also name its own distribution instead of being aliased into one:

```Starlark
bootable_disk_image(
    name = "appliance",
    distribution = "//catalog:<family>.<release>.distribution",
    ops = [install_package_set("bootable")],
    ...
)
```

Either way the package manager comes from a `select()` on that distribution, and the package names come
from the release's package sets, so moving an image between distributions changes neither its operations
nor the rules underneath. The mechanism is described in [design.md](design.md#selecting-a-distribution).

The examples deliberately have no default. `//examples/image:boot-demo` is declared for no distribution
in particular, so building it by that name fails as incompatible and `//examples/image/...` skips it;
the per-distribution aliases such as `:boot-demo.fedora` are what build. A default would make whichever distribution it
named the only one anybody builds, and the other one would rot. A package gets that behaviour by
saying once, in its `PACKAGE` file, which distributions its images serve:

```Starlark
load("@tine//distribution:defs.bzl", "set_distributions_for_package")

set_distributions_for_package({
    "<name>": {
        "distribution": "//catalog:<family>.<release>.distribution",
        "package_manager": "//catalog:<family>.<release>.package-manager",
    },
})
```

Only the `distribution` key is tine's business. Everything beside it is whatever the package needs
to know per distribution, read back with `distributions_for_package()`, so a BUCK file selects its
package manager and names its aliases from the same table and one place adds a distribution.

Every image rule then defaults its `target_compatible_with` to them, so no target repeats it and none
can forget it: a target that is itself unconstrained while its image is incompatible is an error
rather than a skip, which is exactly the mistake the per-package declaration prevents. A rule tine
does not own, such as a prelude `command_alias` over an image, has no macro to inherit through and
asks with `distribution_compatibility()`.

Building one of those targets without choosing says so by name:

```text
tine//examples/image:boot-demo is incompatible with prelude//platforms:default
    (tine//distribution:no-distribution-chosen unsatisfied)
```

A distribution is also a platform, so a developer who builds one of them all day can name it as the
default for unqualified targets in `.buckconfig.local`, which is git-ignored and belongs to the
checkout rather than the repository:

```ini
[parser]
target_platform_detector_spec = target:tine//...->//catalog:<family>.<release>.distribution
```

That platform is the base one plus the distribution's constraint, so choosing a distribution does not
drop the cpu and os the base platform carries. Nothing in the repository sets it: an unqualified target
means "no distribution chosen" everywhere except a checkout that has said otherwise.

A consuming project adds `//packages/fedora/rawhide:zlib-ng`, which validates package import,
package-manager selection, buildroot assembly and RPM collection (packages with `buildroot_deps`
additionally exercise local-package preference), and `//examples/image-local-packages:image`, which builds
an image from those packages. Neither target exists in this repository.

## Updating pinned tools

`bump` refreshes the pinned tool releases against their upstream GitHub releases.

Update one or more pinned tools by name (`buck2`, `starlark-fmt`, `python3`, `ruff`, `ty`, `syft`):

```sh
tools/buck run tine//tools:bump -- --tool ruff --tool ty
```

Update all tools:

```sh
tools/buck run tine//tools:bump -- --all
```

Each tool is resolved to its latest upstream release and its `url` and `sha256` are rewritten in place.
Add `--commit` to record the result as a git commit whose message itemizes each update.

`buck2` and `starlark-fmt` ship from the same release of the same fork, so bump them together (`--all`
does) to keep them on one tag.

python3 minor version stays pinned in pyproject.toml; updating to a new minor release stays a deliberate
manual change.
