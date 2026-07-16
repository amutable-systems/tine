# Tine architecture

This document describes the architecture implemented in this repository, the decisions that shaped it,
and the work that remains. It is a living architecture document, not a chronological implementation plan.

Statements under **Current architecture** describe code that exists. **Roadmap** describes accepted or
possible future work and labels open questions explicitly. When implementation and this document disagree,
the implementation is authoritative and this document should be corrected.

## Purpose and scope

Tine uses Buck2 to build native packages and compose operating-system images. The long-term goal is a
monorepo in which a useful core package set is rebuilt from source, scheduled in dependency order, cached
by content, and suitable for remote execution. The current implementation already provides:

- pinned RPM repositories and a repository-owned RPM artifact pool;
- bootstrap engine roots containing the pinned userspace used by build actions;
- configured package managers and shared buildroots for Fedora and CentOS Stream;
- RPM builds from imported spec/source metadata, including self-hosted buildroot dependencies;
- layered filesystem images, deterministic archives, bootable GPT disks, and a VM runner.

The system does not yet claim complete source provenance, production signing, remote execution, or a full
release pipeline. Images currently mix packages built in this repository with pinned upstream packages.

## Current architecture

### Repository and cell layout

The root project consumes reusable machinery from the `tine` cell:

```text
root//distribution/       independently versioned package specs, sources, and generated BUCK files
root//examples/image/     image smoke targets
tine//package/            package-system-neutral providers and installation flow
tine//package_system/rpm/ RPM repository, resolver, installer, extractor, and builder
tine//engine/             engine bootstrap and sandbox command construction
tine//rootfs/             bind/overlay mounting and stored-delta translation
tine//image/              layers, boot artifacts, composition macros, and VM runners
tine//image_format/       archive, directory, and raw-disk output rules and drivers
tine//catalog/            default repositories, locks, releases, package managers, and buildroots
tine//tools/              pinned development and catalog-refresh commands
```

The `catalog` cell is consumer-overridable. The active catalog owns its release selection, mirrors, engine
choice, repository additions, and policy overrides. Reusable RPM-family macros provide Tine-maintained
repository layouts and package policy, while the low-level rules remain available for unfamiliar or heavily
customized distributions. The `buildroots` cell maps importer-generated names such as
`buildroots//fedora:rawhide` to catalog targets.

Catalog targets use `<family>.<release>[.<component>].<role>` names. A rolling channel such as Rawhide
occupies the release segment. Singular `.repository` targets own remotes, plural `.repositories` targets
define universes, and release, engine, package-manager, and buildroot targets use their corresponding
suffixes. Low-level declaration macros require the suffix appropriate to their role. The family catalog
macros instead take a `<family>.<release>` prefix and declare the complete repository, universe, release,
package-manager, and buildroot bundle. An engine's identity describes its provenance rather than every
release that may consume it.

The package source tree at `distribution/` is a separate Git repository. It is intentionally not part of
the reusable `tine` cell: package policy and imported source data change independently of build machinery.

### Component model

The package model separates identity and policy from the exact inputs used by an action:

```text
PackageSystemInfo (RPM drivers)
        │
        ├── PackageRepositoryInfo ──┐
        │                           ├── RepositoryUniverseInfo
        │                           │          │
        └───────────────────────────┴── OsReleaseInfo
                                               ├── engine lock ── EngineInfo ──┬── rpm_package
                                               │                              ├── image tooling
                                               │                              │
                                               └──────────────────────────────┴── PackageManagerInfo
                                                                            ├── image installation
                                                                            └── BuildrootInfo ── rpm_package
```

The providers have deliberately narrow roles:

- `PackageSystemInfo` bundles the drivers for one native binary-package ecosystem: snapshot, extract,
  install, `createrepo`, plan, and build. RPM is the only implementation today.
- `PackageRepositoryInfo` represents one repository and binds it to a package system. Its target name is
  the repository ID; remote declarations also expose their pinned directory and base URL. Priority is
  configuration policy, not an intrinsic repository property. A repository is not inherently owned by an
  OS release.
- `LocalPackageRepositoryInfo` identifies a repository assembled from package artifacts in the build graph.
  It carries package directories, not the engine that produced them; a consuming package manager generates
  repodata with its own engine.
- `RepositoryUniverseInfo` defines one homogeneous solve universe: required repositories, named optional
  groups, and groups enabled by default. Selection preserves declaration order, de-duplicates identical
  targets, and rejects conflicting repository IDs.
- `OsReleaseInfo` associates named native package sets with one repository universe and supplies the base
  of an engine. Its target label carries the release identity.
- `PackageManagerInfo` is an immutable solve environment. Each ordered `ConfiguredPackageRepositoryInfo`
  record carries the repository ID, materialized directory, effective priority, base URL, and optional
  declaration dependency. The dependency is analysis-only and is absent for an inline repository made from
  package-build inputs. The manager also carries reusable solver caches, chooses an engine, and carries its
  release's package sets. A derived manager inherits this state and can add repositories without repeating
  or rematerializing inherited release policy.
- `BuildrootInfo` materializes the shared base root from explicit packages or a release package set.
- `EngineInfo` contains a runnable root filesystem, its resolution architecture, and the sandbox used to
  enter it. Its target label establishes provenance; the engine may serve compatible package managers for
  other releases.

This split is visible in the default catalog. Fedora 44, Rawhide, and CentOS Stream 10 are separate OS
releases. Their package managers solve against their own repositories while sharing the Rawhide engine.
CentOS models BaseOS as required, AppStream as a default repository group, and CRB as an optional group
enabled by the current package manager. `fedora_release()` and `centos_stream_release()` declare these
standard target bundles and package sets while accepting overrides for mirrors, repositories, priorities,
and package policy. Their buildroots resolve the release's `buildroot` package set rather than duplicating
native package names in the buildroot declaration.

An image bootstrap target normally fixes one package manager for the lifetime of the logical image and
derives its engine from that manager. Every layer and terminal output inherits both. Engine-only images are
also supported, but cannot install native packages. A package target names a buildroot because its shared
base root, not OS identity alone, is its relevant input.

### Catalog pinning and refresh

Normal builds do not resolve against live network repositories. The catalog contains two generated forms
of committed lock data:

- `snapshot/repo/<name>.json` pins filtered `repomd.xml`, the primary/filelists/group streams needed by
  libdnf5, and the complete primary-metadata package inventory keyed by SHA-256 `pkgid`;
- `snapshot/engine/<name>.json` pins the engine transaction. Remote records contain
  `{source, repo, pkgid, nevra, url, size}`: `pkgid` verifies the bytes, while `url` and `size` record the
  last known transport after rolling repository metadata stops advertising that package. The target's
  `.repository` or `.engine` suffix is not repeated in the snapshot filename.

`tine/tools/buck run tine//tools:refresh-catalog` refreshes them in three phases:

0. Advance every repository pinned to an rpmrepo mirror (declared through the release macro's
   `rpmrepo_mirror`/`rpmrepo_snapshot` and carried on the target as `rpmrepo.*` metadata) to the
   newest snapshot its gateway enumerates, by rewriting the declared `rpmrepo_snapshot` in place.
   `verify-catalog` skips this phase and checks the committed pins.
1. Run every remote repository's `[snapshot]` sub-target on the host. `snapshot.py` downloads and verifies
   repodata, drops unused streams, validates package locations, and atomically writes deterministic, pure
   snapshot JSON. It does not carry packages forward from an earlier snapshot.
2. Run the selected engines' `[resolve]` sub-targets against the freshly pinned repository trees and
   atomically replace their transactions. The target engine's release, repository selection, package list,
   and architecture define the solve.

The catalog tool discovers the active `catalog` cell with Buck and always reads and writes snapshots in
that cell's directory. `--engine` limits which engine transactions are resolved; repository snapshots are
always refreshed together.

A committed engine lock itself retains any package transport needed to build that engine. The repository
package pool combines those retained transports with its current snapshot, so every intermediate refresh
state remains buildable and an interrupted refresh can simply be re-run. Transitional repository snapshots
are unnecessary.

Transport retention does not turn a rolling mirror into an archive. A URL may eventually disappear; a
clean-cache rebuild then needs a durable archive/content store, while an already fetched artifact can still
come from Buck's content-addressed cache. The lock preserves the identity, expected size, and last route so
that availability can be supplied independently without changing the solve.

`verify-catalog` performs the same generation and fails when committed JSON differs. Repository snapshots
are ordinary Buck source inputs, so changes invalidate only consumers of the changed data.

`rpm_remote_repository()` derives its optional snapshot by stripping `.repository` from the target name and
looking under `snapshot/repo/`. This lets a new repository target analyze before its first refresh;
consuming its empty package pool fails with an explicit instruction to refresh the catalog. An established
engine that resolves itself needs a usable committed bootstrap transaction. A new engine may instead seed
an internal empty lock by setting `resolver_engine` to a working predecessor. The predecessor supplies only
the execution environment for `plan.py`; the new engine's release, repositories, packages, and architecture
still define the resulting transaction. Refreshing the catalog creates the derived lock path.

The refresh convention keeps repository and engine declarations plus their generated JSON in the active
catalog's root Buck package, with generated data grouped under `snapshot/{repo,engine}/`. This makes
target-name-derived paths and the package-local `snapshot/engine/*.json` retention inputs agree.

### Authoritative repository package pools

Each `rpm_remote_repository()` target owns separate dynamic values for its pinned repodata and package pool.
The pool expands the union of the current snapshot inventory and remote transports retained by committed
engine locks into:

- one digest-checked raw RPM artifact per `pkgid`;
- one decompressed cpio payload representation per RPM.

The raw RPM and derived payload are alternative representations of the same `PackageArtifactInfo` record. The
repository target is their canonical action owner, so engines, buildroots, and images share its downloads
and decompression actions. A package removed from the latest snapshot remains in the pool while a committed
engine lock references its pinned URL and size.

`select_package_artifacts()` reads a resolved transaction, looks up each `(repository, pkgid)` in the
authoritative pool, and creates a symlinked directory containing the requested representation. It never
creates a second download. Buck materializes only artifacts selected by a consuming transaction, while every
consumer shares their owning actions.

Local RPMs produced by this repository use transaction entries with `source = "local"` and a location into
an input RPM directory. They are projected directly from the producing target rather than copied into a
second pool.

Why repository ownership matters:

- one digest and one action graph node define each upstream RPM;
- raw, verified, decompressed, or future representations have a natural shared owner;
- engine, buildroot, and image closures become cheap selectors;
- repository snapshot skew fails at the lookup boundary instead of silently downloading different bytes.

### Engine bootstrap

An engine is a pinned execution environment built from one base OS release. It supplies rpm, Python,
libdnf5, `createrepo_c`, core utilities, sandbox dependencies, and currently the image-building/VM tools.
The base release identifies where this userspace came from, not the only release it may operate on.

An engine normally uses its own completed root to run its `[resolve]` command. During a bootstrap or tooling
transition, `resolver_engine` can point at a predecessor root instead. This edge is deliberately one-way:
it changes where resolution executes, not the repositories, requested packages, architecture, or root built
for the new engine. Engine resolution does not consume package-manager priority policy; its repositories use
the native default priority until bootstrap needs an explicit policy of its own.

Bootstrapping breaks the dependency on host RPM tooling in two stages:

1. The repository pool supplies pre-decompressed payload cpio artifacts for the locked engine transaction.
   The minimal `extract.py`/`cpio.py` path unpacks them into `chroot1` without running scriptlets or creating
   an rpmdb.
2. The package-system installer runs from `chroot1` and properly installs the raw RPM closure into
   `chroot2`, including scriptlets and the rpmdb. `chroot2` becomes the reusable `EngineInfo` root.

The bootstrap extractor currently supports the RPM v4/newc form used by the pinned Fedora repository. It
does not implement RPM v6's index-based payload metadata. The second-stage install is authoritative for
package metadata, ownership behavior available through the unprivileged sandbox, and scriptlets.

The host contract is intentionally small:

- the pinned Buck2 binary and its bundled prelude;
- the pinned bootstrap Python used to run the minimal extractor and development tools;
- unprivileged user namespaces and the filesystem/kernel facilities required by mkosi-sandbox/overlayfs;
- `/dev/kvm` only when running the VM target.

### Execution isolation and target roots

All build actions run through `chroot_run()` and `tine/engine/sandbox.py`. The sandbox binds the engine's
userspace read-only over an otherwise isolated namespace, supplies API and temporary filesystems, clears the
host environment, disables network by default, and uses mkosi-sandbox's unprivileged fakeroot behavior
(`--suppress-chown`, `--suppress-sync`, and `--become-root`).

The sandbox only creates the execution environment. Drivers own their target-root layout through
`rootfs.rootfs()`:

- a fresh install or image layer binds an output directory at `/buildroot`;
- an incremental install or image layer mounts an ordered lower stack plus a persisted upper;
- an RPM build mounts its buildroot stack with an ephemeral upper and binds action scratch at `/build`;
- pack/disk operations merge a stack with an ephemeral upper so cleanup does not modify stored layers.

This division keeps one namespace boundary while letting each driver express the root it needs. Nesting a
second sandbox inside an engine would duplicate isolation, complicate mounts, and make remote execution
harder.

`chroot_run(relaxed = True)` is reserved for interactive leaves. The engine still supplies userspace, but
devices, `/run`, environment, current directory, and network come from the host, and the command remains the
invoking user. The engine's `nss-systemd` reads native identities from the host's UserDB services under
`/run`. This avoids importing host NSS modules or shadow databases, which may be incompatible with the
pinned userspace. `image_vm` is the only current consumer; build actions never use relaxed mode.

### Native package installation

Native package installation has three phases shared by buildroots and images:

1. **Plan.** Run `PackageSystemInfo.plan` against each configured repository's materialized directory,
   effective priority, and solver cache. Weak dependencies are disabled. Existing lower layers are mounted
   read-only so installed packages can satisfy an incremental request.
2. **Select.** Use the resulting transaction to select raw RPMs from repository pools. A local repository is
   materialized by an anonymous `createrepo` target using the consuming package manager's engine. The same
   path handles package-build inputs and lets the libdnf5 solve choose between local and upstream packages.
3. **Install.** Run `PackageSystemInfo.install` over the exact RPM directory. `install_packages()` owns a
   fresh root or incremental buildroot delta. An image layer instead invokes the same installer against its
   already-mounted root so package and filesystem operations have one output owner.

The package manager assigns default priorities when it configures repositories: local repositories use 50
and remote repositories use 99, with target-specific overrides applied by `package_manager()`. Planner
actions receive an artifact-aware JSON manifest containing `[id, directory, priority, baseurl]` tuples.
`write_repository_manifest()` projects those tuples from `ConfiguredPackageRepositoryInfo`; the record's
`dependency` is deliberately stripped because Buck dependencies are analysis-only and not JSON-serializable.
`plan.py` parses each tuple into its `Repository` named tuple. The directory selects pinned local repodata,
while the base URL records the transport for remote packages selected into a transaction.

Package-specific `buildroot_deps` use the same representation. Their ordered RPM directories are
materialized as an anonymous repository with ID `extra`, local priority, no base URL, and no declaration
dependency. Transaction selection maps its local locations directly back to the producing package outputs.

Fresh buildroot installs are anonymous targets keyed by package manager, sorted install specs, and local
package inputs. Buck therefore shares the base buildroot analysis/action graph across packages that use the
same build profile. Package-specific BuildRequires layers remain inline and are installed over the shared
base.

After installation, `install.py` checkpoints and vacuums the SQLite rpmdb, removes WAL/SHM/lock files, and
scrubs libdnf5/ldconfig bookkeeping that would otherwise make identical roots differ. The action that owns
the root then captures names and overlay metadata into Buck-storable form. A fresh root receives mkosi's
`uninitialized` machine-id marker; incremental installs preserve any existing machine ID.

### RPM import and build flow

The independent `distribution/` repository contains imported source-package metadata and generated BUCK
files. The importer emits data; the Starlark in `package_system/rpm/generated.bzl` validates that data and
creates targets.

For each branch, `rpm_branch()` currently:

- combines common and `x86_64` BuildRequires;
- indexes each imported binary package's name, `Provides`, and file paths;
- maps BuildRequires capabilities to source-package targets;
- computes strongly connected components and drops ordinary intra-cycle edges to upstream packages;
- retains explicitly configured buildroot-only edges and rejects cycles they reintroduce;
- creates one `rpm_package` target per source package.

This is a static, import-time self-hosting approximation. It is useful today but is not the planned final
dependency lock: rich dependency parsing is intentionally limited, the architecture is fixed to `x86_64`,
and runtime package closures are still delegated to libdnf5 at buildroot-plan time.

An `rpm_package` action:

1. obtains the shared base root from its `BuildrootInfo`;
2. resolves and installs its BuildRequires delta, preferring RPMs from `buildroot_deps` over upstream;
3. overlays the base and delta, stages its spec/sources in Buck action scratch, and runs `rpmbuild -ba`;
4. freezes `%autorelease`, `_buildhost`, the dist tag, and the per-package source date epoch;
5. collects binary RPMs and the source RPM into one output directory, carrying its package-system identity
   in `LocalPackageInfo`;
6. exposes each declared binary subpackage as a Buck sub-target and checks that declared outputs exist.

The build currently uses `--nocheck`. Automatically generated debuginfo/debugsource RPMs are retained in the
directory output but are tolerated rather than exposed as declared sub-targets. Successful build scratch is
discarded; failed scratch remains available for diagnosis.

`LocalPackageInfo` intentionally does not carry the producer's engine. `local_repository` checks that its
packages use one native package system and remains an engine-independent declaration. The consuming package
manager materializes deterministic repodata with its own engine; anonymous materializations with the same
engine, package system, and ordered package directories share one action.

### Filesystem layer representation

Buck directory artifacts cannot faithfully store overlay whiteout devices, opaque-directory xattrs, or a
backslash in a path component. Tine stores filesystem deltas in a regular-file representation:

- `.wh.<name>` represents a whiteout;
- `.wh..wh..opq` represents an opaque directory;
- `.esc.<percent-escaped-name>` represents a path component Buck cannot store.

`rootfs.capture()` translates a native overlay upper into this form after unmounting. Before a later mount,
`rootfs.rootfs()` constructs sparse sidecar layers that translate the stored markers back to native
overlayfs whiteouts/xattrs and thaw escaped names. Stored layers remain ordinary Buck artifacts and can
therefore move through its CAS.

Directory modes are made traversable so Buck can materialize/delete them, and Buck's artifact model does
not preserve general ownership, capabilities, xattrs, or every mode bit. Authored tmpfiles snippets can
recreate paths, modes, and xattrs during terminal assembly. Ownership is deliberately normalized to uid/gid
zero rather than reconstructed. SELinux labels are not currently produced.

### Image construction

The `image` rule bootstraps an empty logical image and normally fixes its package manager. `ImageInfo`
carries that manager, its engine, an ordered stack of filesystem deltas, and deferred tmpfiles snippets.
Every derived image inherits this configuration, so one composition cannot silently switch package sources
or tooling environments between stages. An engine may be supplied directly for an image that never
installs native packages.

`image_layer` applies one ordered operation sequence in one action and persists exactly one overlay upper.
It may contain one `install` or `install_package_set` operation at any position; the selected package-system
installer operates on the already-mounted root, while `run`, `copy`, `mkdir`, `symlink`, and `remove` mutate
the same root. A package-set operation resolves its symbolic name through the parent image's package manager
during analysis, then becomes an ordinary install operation. `copy` introduces a declared Buck artifact at
an absolute image path, preserving its position relative to the other operations. `run` executes the image's
own tools in a chroot by default; `chroot = False` instead executes engine tooling with the image available
at `/buildroot`. Its `env` argument overlays variables on the engine or image environment for that command.
Package installation and copying always run outside the chroot. Operation lists are recursively flattened,
allowing reusable helpers to return ordered groups of operations.

Every `image_layer` exposes a `directory` subtarget which lazily materializes the complete logical image at
that point. The layer's default output remains its persisted delta, and downstream image rules continue to
consume `ImageInfo` rather than the directory artifact.

Package installation and image tooling remain separate concerns:

- the bootstrap `package_manager` determines what native packages can be resolved;
- that manager's `engine` supplies every layer driver and terminal image tool;
- `local_repository` declares compatible package outputs and infers their package system from
  `LocalPackageInfo`; a consuming package manager materializes deterministic repodata with its own engine;
- a derived package manager adds such repositories to a base manager's configured selection.

For example, a project can expose locally built packages without adding them to its OS release:

```python
local_repository(
    name = "project.repository",
    packages = ["//packages:project"],
)

package_manager(
    name = "project.package-manager",
    base = "catalog//:fedora.rawhide.package-manager",
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

Logical images and terminal outputs are separate rule families. Terminal rules merge the stack only when
needed:

- `image_archive` writes deterministic tar or uncompressed newc cpio archives and provides
  `CpioArchiveInfo` for the latter;
- `image_directory` materializes a Buck directory artifact and provides `DirectoryImageInfo`;
- `uki` builds versioned unified kernel images for every installed kernel using one or more
  `CpioArchiveInfo` dependencies;
- `repart` renders ordered Starlark partition definitions and uses offline `systemd-repart` to create a GPT
  disk with `DiskImageInfo`, independent partition artifacts with `split = True`, or both;
- `bootable` selects a kernel and matching initrd from a logical image and provides `BootableImageInfo`;
- `image_result` aggregates independent facets of the same logical image without creating another artifact;
- `image_vm` runs the raw image ephemerally with the engine's `systemd-vmspawn`, QEMU, and OVMF stack.

`rootfs_archive()` is the convenience composition for building a single layer from operations and emitting
an archive. `image_archive` remains the terminal rule for archiving an existing logical image.

Terminal rules leave the rpmdb and other package state intact. Image cleanup is an explicit, configurable
layer so output formats do not silently alter image contents. Terminal rules apply deferred tmpfiles lines
with `systemd-tmpfiles --root`; a missing tool is an error whenever finalization is needed. The directives
run against a disposable overlay upper and can create paths or restore modes and xattrs. Image-shipped
tmpfiles configuration is not applied implicitly; enabling it will be an explicit output option once its
single-UID/GID behavior is defined. Ownership and named ACL entries are deliberately unsupported: all
archive entries use uid/gid zero.

Tar uses deterministic PAX archives and stores Linux xattrs using `SCHILY.xattr.*` headers. The newc cpio
format has no general xattr representation. Tar/cpio entries are ordered and mtimes are clamped to the fixed
assembly epoch. The cpio reader/writer aligns regular-file payloads and uses `copy_file_range` when possible
so large archives can share extents on reflink-capable filesystems.

`repart` deliberately distinguishes `definitions` from `partitions`. Definitions describe new partitions
to populate directly from `ImageInfo`: repart mounts the delta stack with a disposable overlay upper instead
of first copying a directory artifact. Partition inputs are `RepartInfo` outputs from an earlier split call;
their blocks are copied into the new disk with their resolved type and UUID preserved. Calls emit a disk by
default. `split = True` additionally exposes each newly defined partition with normalized metadata alongside
its block artifact; `disk = False` makes such a call partition-only. No partial disk is passed between
actions. `DirectoryImageInfo` is an independent terminal view and is never an input to repart.

Partition layouts are always explicit inputs; neither `repart` nor `bootable_disk_image` chooses one
implicitly. `DEFAULT_ROOT_PARTITIONS`, `DEFAULT_USR_VERITY_PARTITIONS`, and
`DEFAULT_SIGNED_USR_VERITY_PARTITIONS` provide reusable conventional layouts without hiding the choice at
the call site.

Verity data, hash, and optional signature partitions are produced together in the split action. The root or
usr hash is an artifact because its value is known only after execution; a separate provider lets `uki`
consume it without learning about the partition layout. One split call may produce at most one such hash.
This artifact boundary also ensures the final disk contains exactly the partition bytes whose hash was
embedded in the UKI. The hash is also available as the split target's `roothash` subtarget. Signature
partitions require an explicitly declared key and certificate.

`bootable_disk_image()` composes:

```text
base initrd package image (cpio)
              ├──────┐
root filesystem layer ──> split /usr + verity ──> hash ──> versioned UKIs
                                │                            │
                                └──────────────┬─────────────┘
                                               v
                                      ESP layer
                                      │          │          │
                                      │          │          └─> directory facet
                                      │          └─> bootable facet
                                      └─> split ESP + system partitions ─> disk facet
```

By default, the base initrd is a separate package image with `/init` pointing to systemd and
`/etc/initrd-release` pointing to `/etc/os-release`. Callers can instead supply any `CpioArchiveInfo` target;
`bootable_disk_image` then skips the default initrd image entirely. Otherwise the default layer installs the
release's `initrd` package set, so family catalog policy supplies concrete native package names. `uki.py`
discovers every installed kernel, appends its kernel-modules cpio, and runs `ukify`. UKIs use the configured
entry prefix and kernel release as their filenames. The ESP layer copies the UKI directory into `EFI/Linux`
and includes the operations returned by `install_systemd_boot()`. Those create the ESP path, run the engine's
`bootctl` with its paths in the command environment, and remove the random seed. The final repart action
creates and exports the ESP while copying the previously split system partitions into the same disk. The
default system partition is a compressed EROFS `/usr` protected by dm-verity; the generated `usrhash=` is
embedded in every UKI. The same copy operation can place device trees, bootloader entries, and future
standalone artifacts.

Kernel command lines remain lists of arguments through the Starlark API and driver invocation. The UKI
driver appends any generated verity hash and joins the arguments only when writing ukify's command-line file.

Bootability and output format are independent capabilities. A final target may return any combination of
`BootableImageInfo`, `DiskImageInfo`, and `DirectoryImageInfo`, while continuing to return the underlying
`ImageInfo`. Each facet records its source dependency, and `image_result` rejects facets derived from
different logical images. One facet supplies the target's default output; nested subtargets namespace all
other views:

```text
//examples/image:boot-demo[bootable][uki]
//examples/image:boot-demo[bootable][kernel]
//examples/image:boot-demo[bootable][initrd]
//examples/image:boot-demo[disk][roothash]
//examples/image:boot-demo[disk][partitions][usr]
//examples/image:boot-demo[disk][partitions][esp]
//examples/image:boot-demo[directory]
```

The bootable facet extracts semantic artifacts lazily from the completed logical image rather than
forwarding whichever intermediate target created them. A shared selection manifest chooses the newest
valid UKI by its embedded kernel release and extracts its `.linux` and `.initrd` sections. Without a UKI, it
chooses the newest standalone kernel. In either case, bootability requires an initrd matching that exact
release. A UKI remains an optional extraction: requesting it fails if the selected image has only standalone
artifacts. Requesting a bootable or directory facet does not assemble the disk, and repart never
materializes the directory facet.

Repart derives stable UUID seeds from target identity and logical configuration; callers can override them
explicitly. VM runners are declared separately from disk composition. Runtime policy is passed to `image_vm`
rather than baked into the image.
Its `autologin` option provisions a locked root password and runtime `login.noauth`; arbitrary non-secret
system credentials configure settings such as first-boot locale and timezone. The smoke image uses a tmpfs
root with `mount.usr=dissect`; SELinux is disabled because the build does not yet produce filesystem labels.

The image build tools live in the engine and are not installed into the image merely to build it. Chrooted
`run` operations intentionally use the image's own binaries; non-chrooted runs explicitly use engine tools
against `/buildroot`.

### Reproducibility and caching

Reproducibility is both a release property and a caching requirement. Current mechanisms include:

- repository metadata, package bytes, and source archives pinned by SHA-256;
- committed engine transactions containing repository/package identities and retained transports;
- a fixed assembly `SOURCE_DATE_EPOCH` for roots that should be shared across consumers;
- per-package source date epochs for RPM output timestamps and build headers;
- a fixed `_buildhost` and frozen rpmautospec macros;
- parked/vacuumed rpmdbs and scrubbed package-manager caches;
- sorted transaction JSON, archive entries, source staging, and output collection;
- target/configuration-derived partition UUID seeding and normalized archive metadata;
- content-based paths for repository-owned RPM and payload artifacts.

These measures make action-cache reuse meaningful and prepare the graph for remote execution. The repository
does not yet run a systematic build-twice reproducibility audit, and raw filesystem image byte-for-byte
reproducibility still needs dedicated validation.

## Decision record

The following decisions remain the rationale for the current design. Detailed source-code research that led
to them belongs in commit history or focused notes; this section records the durable conclusion.

### Use Buck2 as the graph and cache

Buck2 was chosen because package builds benefit from content-addressed artifacts, lazy action execution,
sub-target providers for binary RPM outputs, and a test protocol that can later host the Barrage executor.
Its lack of an implicit local sandbox also lets Tine use the same mkosi-sandbox boundary locally and on
future remote workers. Bazel's broader language-rule ecosystem mattered less than these properties for an
RPM-heavy repository.

Buck cannot add ordinary target dependencies discovered from an action output. Dynamic actions may select
among declared inputs but cannot turn newly discovered BuildRequires into a new static graph. Therefore
dependency discovery/import must produce committed or analysis-time lock data before normal builds.

### Commit catalog and dependency lock data

Repository snapshots and engine transactions are generated data, but committing them makes normal
resolution independent of live repository state and reviewable. Buck may still fetch content-pinned
artifacts. A refresh is an explicit update operation rather than an invisible part of every build. This is
analogous to a language dependency lockfile, only it also pins repository metadata and retains the transport
for packages needed to rebuild an engine after a rolling repository advances.

### Let repositories own upstream packages

Putting downloads in each closure duplicated ownership and left derived operations without a stable home.
The authoritative named pool instead gives every upstream package one action owner per repository. The
current snapshot defines available packages, while committed engine locks retain older packages required to
bootstrap their resolver. Closures select artifacts; they do not fetch or transform them.

### Separate package system, OS release, package manager, and buildroot

The former distribution object bundled repository membership, engine tooling, and buildroot policy. That
made optional repositories awkward, implied that an engine had to match every target release it operated
on, and provided no clean place for request-specific local repositories.

The current vocabulary follows the actual responsibilities:

- the package system defines operations;
- the repository universe defines membership and normal enablement policy;
- the OS release defines identity, selects a repository universe, and may provide an engine's base;
- the package manager defines one exact solve universe and engine; derived managers compose additional
  repositories and priority overrides without changing their inherited release or engine;
- the buildroot materializes the shared base packages.

This is also why a release is not called a distribution target: Fedora 44 and CentOS Stream 10 are release
identities, while repositories and engines can be reused across those identities when compatible.

### Keep native package managers homogeneous

Every repository universe and package manager belongs to one native package system. RPM and a future DEB
system must not participate in one dependency solve. Supplemental content systems such as Flatpak may
eventually coexist with RPM in an image, but compatibility rules are deliberately deferred until a second
system exists. `PackageSystemInfo` is for native binary package ecosystems, not every possible image
content type.

### Separate an engine's base release from its target releases

An engine is a tools root with a concrete OS userspace, so its base release records where its packages and
identity came from. That does not make it part of a package manager's target OS identity: the Rawhide engine
can still operate on Fedora 44 and CentOS Stream. An image normally obtains this explicit engine dependency
through its package manager, making reuse visible and content-keyed while avoiding duplicated compatible
tooling roots. Engine-only images remain available when no native package resolution is needed.

### Use one sandbox boundary and let drivers mount target roots

The engine userspace must be pinned, the host environment must not leak into builds, and package scriptlets
need unprivileged fakeroot semantics. Vendored mkosi-sandbox supplies those properties without host RPM,
mock, bwrap, or a second nested sandbox. Drivers mount their own target roots because install, build, image,
pack, and disk actions need different layouts.

### Store image layers as deltas

Copying a complete root for every image step scales with total image size rather than change size. Ordered
overlay deltas let child layers and terminal outputs reuse their ancestors. Encoding whiteouts and opaque
directories as regular files keeps those deltas compatible with Buck's artifact/CAS model.

### Prefer exact transactions over package-manager network access

Resolution uses pinned local repodata; installation consumes an exact directory of already selected RPMs.
This keeps network out of build actions, makes the transaction an inspectable early-cutoff boundary, and
separates “which packages?” from “apply these packages and scriptlets.” Weak dependencies are disabled to
match buildroot policy and avoid unreviewed closure growth.

### Build each source package once and expose subpackages

One `rpmbuild -ba` naturally emits all binary subpackages and the source RPM. Running it once avoids repeated
work and inconsistent sibling outputs. Buck sub-targets give downstream packages addressable binary outputs
without pretending each subpackage is a separate build action.

## Operating the current system

The Buck bootstrap needs `curl`, `sha256sum`, and `zstd` on first use. It verifies and caches the pinned
Buck binary under `${XDG_CACHE_HOME:-$HOME/.cache}/tine/buck2`; cached invocations work offline.

The wrapper commands work from anywhere in the root project:

```text
tine/tools/buck run tine//tools:refresh-catalog
tine/tools/buck run tine//tools:verify-catalog
tine/tools/buck run tine//tools:fmt
tine/tools/buck run tine//tools:check
```

Representative smoke builds are:

```text
tine/tools/buck build root//distribution/packages/fedora/rawhide:zlib-ng
tine/tools/buck build root//examples/image:demo
tine/tools/buck build root//examples/image:layered-install
tine/tools/buck build 'root//examples/image:layered-install.layer[directory]'
tine/tools/buck build root//examples/image:boot-demo
tine/tools/buck build 'root//examples/image:boot-demo[bootable][uki]'
tine/tools/buck build 'root//examples/image:boot-demo[disk][partitions][usr]'
```

The first validates package import, package-manager selection, buildroot assembly, and RPM collection.
Packages with `buildroot_deps` additionally exercise local-package preference. The image targets validate
package installation and commands sharing one delta, incremental layering, archive packing, versioned UKI
creation, semantic boot-artifact extraction, ESP-layer assembly, and disk composition. Running
`boot-demo-vm` validates the interactive VM runner; ephemeral mode preserves the Buck disk artifact.

## Current limitations

These are properties of the implementation today, not merely ideas for future optimization:

- RPM is the only package system, and generated package metadata is fixed to `x86_64`.
- The imported self-host dependency graph is inferred from stored BuildRequires/Provides/file metadata; it
  does not run RPM's dynamic BuildRequires protocol.
- Build cycles fall back to upstream RPMs for ordinary intra-SCC edges, so the package set is not a fully
  self-hosted fixed point.
- There is no generic `PackageInfo`/runtime-closure provider selecting between upstream and source-built
  packages for arbitrary image closures.
- RPM builds use `--nocheck`; package test policy is not implemented.
- Debuginfo/debugsource outputs are not first-class declared sub-targets.
- Upstream package signatures are not verified. SHA-256 pinning gives integrity after refresh, not
  authenticity at refresh time.
- The bootstrap extractor supports the pinned RPM v4/newc payload form, not RPM v6 metadata.
- Archive ownership is intentionally normalized to uid/gid zero. Capabilities, xattrs, and SELinux labels do
  not survive as Buck directory metadata; deferred tmpfiles can restore xattrs at terminal assembly, and tar
  preserves them in PAX headers, but newc cpio cannot represent general xattrs.
- Directory image output cannot represent backslashes in names; archive outputs should be used instead.
- The default `/usr`-only disk has a volatile root. Package and authored state outside `/usr` is not yet
  translated into factory defaults or another persistent partition.
- Bootable images currently disable SELinux. Verity signing accepts declared development key material, but
  production signing boundaries and Secure Boot signing are not implemented.
- Remote execution, Barrage integration, release publishing, and systematic reproducibility audits are not
  wired into CI.

## Roadmap

The roadmap is organized by architectural capability rather than old numbered phases. Ordering within a
section is approximate and should follow the next concrete product need.

### Package graph and self-hosting

1. Replace the current metadata intersection with a generated package lock that records precise direct
   BuildRequires and binary/runtime relationships. Use RPM's exit-11 dynamic BuildRequires protocol for
   packages that generate requirements during `%prep`.
2. Introduce the source/prebuilt package-provider model only when an image or buildroot needs to choose
   backing per package. Preserve one coherent version pin so upstream and source-built variants have the
   same dependency graph.
3. Model runtime closures at binary-subpackage granularity and make debuginfo/debugsource outputs explicit
   where consumers or publishing require them.
4. Extend importer/build configuration beyond the fixed `x86_64` slice.
5. Add RPM v6 bootstrap extraction when a pinned repository requires it.
6. Decide and implement `%check` policy. Because successful build scratch is discarded, checks most likely
   belong in the primary RPM action with per-package opt-outs for broken or prohibitively expensive suites.

The durable self-hosting rule remains: invoked build tools may come from the pinned seed, while libraries
linked into shipped outputs should come from source-built packages once their graph is available. Cycles
must be explicit; silently pretending a cyclic source graph is acyclic is not acceptable.

### Supply-chain authenticity and release output

The accepted direction for upstream authenticity is:

1. Pin reviewed distribution signing keys in repository snapshots.
2. Add a repository-owned verified RPM representation using libdnf5/rpm signature verification.
3. Make installation select verified artifacts while preserving raw and payload representations.
4. Handle engine trust inductively: an existing trusted engine verifies the inputs of its successor rather
   than allowing a new engine to vouch for itself.

The exact Rawhide key policy and first-trust/bootstrap procedure remain open. HTTPS plus committed SHA-256
locks currently provides reviewable integrity but is not a substitute for signature verification.

A later release pipeline needs repository composition, comps metadata, source/debuginfo publication policy,
provenance/attestations, and signing. Secure Boot signing should use deterministic RSA PKCS#1 v1.5 without
timestamps. Development keys can be declared/cacheable inputs; production keys should be exposed through a
restricted signing service/PKCS#11 boundary and run as non-cacheable release actions.

### Image hardening and formats

Near-term image gaps are:

- offline SELinux labeling instead of `selinux=0`;
- deterministic ext4/FAT byte-level validation and any required normalization;
- measured boot, production verity signing, and Secure Boot integration;
- OCI, sysext/confext, ESP, and other terminal formats as real consumers require them;
- richer ordered operations for setting file metadata directly;
- deciding whether package installation and image tooling eventually need distinct compatible engines.

The layer model should remain ordered operations captured as deltas. A provides/requires feature solver is
unnecessary unless real composition requirements appear.

### Scale, configuration, and testing

- Configure remote cache/execution only after the local action graph and host contract are stable. Engine
  roots and ordinary filesystem artifacts are intended to be CAS inputs; relaxed VM actions remain local.
- Add build-twice reproducibility audits because early cutoff is useful only when rebuilt outputs are
  byte-identical. Track known exceptions explicitly rather than weakening all comparisons.
- Integrate Barrage through Buck2's external test executor so many image/integration tests can share one
  streamed process while still reporting per-test results.
- Add leaf-selected OS/package-manager transitions when consumers need one target graph to build against
  multiple releases. The transition must select existing provider boundaries rather than reintroduce a
  monolithic distribution object.
- Add native language toolchains backed by package/image roots only when in-repository C/C++/Go/Rust builds
  need them.
- Define the upstream-update workflow: import Fedora changes, rebase local patches, refresh snapshots and
  generated metadata, and verify that version skew has not invalidated source/upstream interchangeability.

## Reference points

Useful implementation entry points:

- `tine/package/{system,repository,release,manager,buildroot,install}.bzl`
- `tine/package_system/rpm/rules.bzl` and
  `tine/package_system/rpm/{snapshot,plan,install,createrepo,build,extract,decompress}.py`
- `tine/engine/{rules.bzl,sandbox.py}` and `tine/rootfs/rootfs.py`
- `tine/image/{layer,uki,boot,compose,vm}.bzl` and `tine/image_format/{archive,disk}.bzl`
- `tine/tools/catalog.py` and `tine/catalog/BUCK`
- `distribution/apt` and `tine/package_system/rpm/generated.bzl`

External projects that informed the design:

- Buck2 for action/dynamic-dependency semantics, sub-targets, content-based paths, and test execution;
- rpm and libdnf5 for build, resolution, transaction, and signature behavior;
- mkosi/mkosi-sandbox for user-namespace isolation, root mounting, UKIs, and repart-based images;
- Antlir2 for repository/package-selection ideas and as a comparison point for image feature graphs;
- Barrage for the planned streamed integration-test executor;
- Siguldry for a possible production PKCS#11 signing boundary.
