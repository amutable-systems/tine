# Tine architecture

This document describes the architecture implemented in this repository, the decisions that shaped it,
and the work that remains. It is a living architecture document, not a chronological implementation plan.

Statements under **Current architecture** describe code that exists. **Roadmap** describes accepted or
possible future work and labels open questions explicitly. When implementation and this document disagree,
the implementation is authoritative and this document should be corrected.

User-facing guides live alongside this document: [images.md](images.md) covers building and running
images, and [importer.md](importer.md) covers maintaining packages with the importer.

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
//packages/               independently versioned package specs, sources, and generated BUCK files
//examples/image/         image smoke targets
//examples/image-local-packages/  bootable image from self-built packages
tine//package/            package-system-neutral providers and installation flow
tine//package_system/rpm/ RPM repository, resolver, installer, extractor, and builder
tine//engine/             engine bootstrap and sandbox command construction
tine//rootfs/             bind/overlay mounting and stored-delta translation
tine//image/              layers, boot artifacts, composition macros, and VM runners
tine//image_format/       archive, directory, and raw-disk output rules and drivers
tine//catalog/            default repositories, locks, releases, package managers, and buildroots
tine//tools/              pinned development and catalog-refresh commands
```

The default `tine//catalog` package owns its release selection, mirrors, engine choice, repository additions,
and policy overrides. Projects can instead declare their own catalog package with Tine's reusable RPM-family
macros or low-level rules. The project's `//buildroots` package maps importer-generated names such as
`//buildroots/fedora:rawhide` to catalog targets.

Catalog targets use `<family>.<release>[.<component>].<role>` names. A rolling channel such as Rawhide
occupies the release segment. Singular `.repository` targets own remotes, plural `.repositories` targets
define universes, and release, engine, package-manager, and buildroot targets use their corresponding
suffixes. Low-level declaration macros require the suffix appropriate to their role. The family catalog
macros instead take a `<family>.<release>` prefix and declare the complete repository, universe, release,
package-manager, and buildroot bundle. An engine's identity describes its provenance rather than every
release that may consume it.

The package source tree lives in the OS.git repository (which vendors this `tine` cell) under `packages/`.
It is intentionally not part of the reusable `tine` cell: package policy and imported source data change
independently of build machinery.

### Component model

The package model separates identity and policy from the exact inputs used by an action:

```text
PackageSystemInfo (RPM drivers)
        │
        ├── PackageRepositoryInfo ──┐
        │                           ├── RepositoryUniverseInfo
        │                           │          │
        └───────────────────────────┴── OsReleaseInfo
                                               ├── engine transaction ── EngineInfo ──┬── rpm_package
                                               │                                     ├── image tooling
                                               │                                     │
                                               └─────────────────────────────────────┴── PackageManagerInfo
                                                                                   ├── image installation
                                                                                   └── BuildrootInfo
                                                                                          └── rpm_package
```

The providers have deliberately narrow roles:

- `PackageSystemInfo` bundles the drivers for one native binary-package ecosystem: snapshot, extract,
  install, package database capture, repository indexing, plan, and build, plus the paths that database
  occupies in an installed root and the file suffix of an installable package. RPM is the only
  implementation today.
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
- `LocalPackageUniverseInfo` describes a universe of locally built packages together with the imported
  runtime Requires/Provides metadata needed to select an install request's closure among them.
- `BuildrootInfo` materializes the shared base root from explicit packages or a release package set.
- `EngineInfo` contains a runnable root filesystem, its resolution architecture, and the sandbox used to
  enter it. Its target label establishes provenance; the engine may serve compatible package managers for
  other releases.

Solver caches are anonymous targets keyed by resolver engine, package system, configured repository,
architecture, and execution platform. Matching engines and package managers therefore consume one shared
cache artifact, while distinct solver contexts remain isolated.

This split is visible in the default catalog. Fedora 44, Rawhide, and CentOS Stream 10 are separate OS
releases. Their package managers solve against their own repositories while sharing the Rawhide engine.
CentOS models BaseOS as required, AppStream as a default repository group, and CRB as an optional group
enabled by the current package manager. `fedora_release()` and `centos_stream_release()` declare these
standard target bundles and package sets while accepting overrides for mirrors, repositories, priorities,
and package policy. Their buildroots resolve the release's `buildroot` package set rather than duplicating
native package names in the buildroot declaration.

An initial image normally fixes one package manager for the lifetime of the logical image and derives its
engine from that manager. Every derived image and terminal output inherits both. Engine-only images are also
supported, but cannot install native packages. A package target names a buildroot because its shared base
root, not OS identity alone, is its relevant input.

### Catalog pinning and refresh

Normal builds do not resolve against live network repositories. The catalog contains one required and one
optional generated form:

- `snapshot/repo/<name>.json` pins filtered `repomd.xml`, the primary/filelists/group streams needed by
  libdnf5, and the complete primary-metadata package inventory keyed by SHA-256 checksum;
- `snapshot/engine/<name>.json` optionally freezes an engine transaction. Remote records contain
  `{source, repo, pkg_checksum, package_id, url, size}`: the checksum verifies the bytes, while `url` and
  `size` record the last known transport after rolling repository metadata stops advertising that package.
  The target's `.repository` or `.engine` suffix is not repeated in the snapshot filename.

An engine with `resolver_engine` and no committed transaction resolves through that predecessor as a normal
cacheable build action. The generated transaction is an input to the existing dynamic package selectors,
so the engine builds in one invocation without mutating the source tree. Its package selection changes only
when its authored policy, resolver engine, or pinned repository inputs change.

`tine/tools/buck run tine//tools:refresh-catalog` refreshes the default `tine//catalog` package in two
phases. Pass another catalog package after `--`, for example
`tine/tools/buck run tine//tools:refresh-catalog -- my_project//catalog`:

0. Advance every repository pinned to an rpmrepo mirror (declared through the release macro's
   `rpmrepo_mirror`/`rpmrepo_snapshot` and carried on the target as `rpmrepo.*` metadata) to the
   newest snapshot its gateway enumerates, by rewriting the declared `rpmrepo_snapshot` in place.
   `verify-catalog` skips this phase and checks the committed pins.
1. Run every remote repository's `[snapshot]` sub-target on the host. `snapshot.py` downloads and verifies
   repodata, drops unused streams, validates package locations, and atomically writes deterministic, pure
   snapshot JSON. It does not carry packages forward from an earlier snapshot.
2. Run the selected engines' `[resolve]` sub-targets against the freshly pinned repository trees and
   atomically replace their optional frozen transactions. The target engine's release, repository
   selection, package list, and architecture define the solve.

The catalog tool asks Buck for the selected package's canonical targets and derives the snapshot directory
from their canonical cell and package. `--engine` limits which engine transactions are resolved; repository
snapshots are always refreshed together.

A committed engine lock retains any package transport needed to build that exact transaction. The repository
package pool combines those retained transports with its current snapshot, so every intermediate refresh
state remains buildable and an interrupted refresh can simply be re-run. A lockless engine always resolves
from the current pinned snapshot and therefore needs no retained transport for packages absent from it.

Transport retention does not turn a rolling mirror into an archive. A URL may eventually disappear; a
clean-cache rebuild then needs a durable archive/content store, while an already fetched artifact can still
come from Buck's content-addressed cache. The lock preserves the identity, expected size, and last route so
that availability can be supplied independently without changing the solve.

`verify-catalog` performs the same generation and fails when committed JSON differs. Repository snapshots
are ordinary Buck source inputs, so changes invalidate only consumers of the changed data.

`rpm_remote_repository()` derives its optional snapshot by stripping `.repository` from the target name and
looking under `snapshot/repo/`. This lets a new repository target analyze before its first refresh;
consuming its empty package pool fails with an explicit instruction to refresh the catalog. An engine that
resolves itself needs a usable committed bootstrap transaction. A new engine instead names a working
`resolver_engine`; the predecessor supplies only the execution environment for `plan.py`, while the new
engine's release, repositories, packages, and architecture define the generated transaction. Refreshing the
catalog is optional for that engine and freezes the generated result at the conventional lock path.

The refresh convention keeps repository and engine declarations plus their generated JSON in the active
catalog's root Buck package, with generated data grouped under `snapshot/{repo,engine}/`. This makes
target-name-derived paths and the package-local optional `snapshot/engine/*.json` retention inputs agree.

### Authoritative repository package pools

Each `rpm_remote_repository()` target owns separate dynamic values for its pinned repodata and package pool.
The pool expands the union of the current snapshot inventory and remote transports retained by committed
engine locks into:

- one digest-checked raw RPM artifact per checksum;
- one decompressed cpio payload representation per RPM.

The raw RPM and derived payload are alternative representations of the same `PackageArtifactInfo` record. The
repository target is their canonical action owner, so engines, buildroots, and images share its downloads
and decompression actions. A package removed from the latest snapshot remains in the pool while a committed
engine lock references its pinned URL and size.

`select_package_artifacts()` reads a resolved transaction, looks up each `(repository, checksum)` in the
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

An engine is a reproducible execution environment built from one base OS release. It supplies rpm, Python,
libdnf5, `createrepo_c`, core utilities, sandbox dependencies, and currently the image-building/VM tools.
The base release identifies where this userspace came from, not the only release it may operate on.

A lockless engine uses `resolver_engine` to produce its build transaction and perform the authoritative RPM
installation. Its target root therefore contains only the requested packages and their dependencies; it
does not need Python, libdnf5, rpm, or other construction tools unless they are part of its intended runtime.
A locked engine can use its own completed root to run the explicit `[resolve]` update command; during a
bootstrap or tooling transition, a predecessor may run that command instead. This edge is deliberately
one-way: it changes where resolution and installation execute, not the repositories, requested packages,
architecture, or root built for the new engine. Engine resolution does not consume package-manager priority
policy; its repositories use the native default priority until bootstrap needs an explicit policy of its own.

Only a root engine without a predecessor bootstraps its own installation tools in two stages:

1. The repository pool supplies pre-decompressed payload cpio artifacts for the effective engine transaction.
   The minimal `extract.py`/`cpio.py` path unpacks them into `chroot1` without running scriptlets or creating
   an rpmdb.
2. The package-system installer runs from `chroot1` and properly installs the raw RPM closure into
   `chroot2`, including scriptlets and the rpmdb. `chroot2` becomes the reusable `EngineInfo` root.

Engine configuration prefers a target-provided systemd factory `nsswitch.conf`, but writes a deterministic
files/DNS fallback for minimal roots. Resolver integration and target configuration therefore do not impose
specific implementation packages on a derived engine.

The bootstrap extractor currently supports the RPM v4/newc form used by the pinned Fedora repository. It
does not implement RPM v6's index-based payload metadata. The second-stage install is authoritative for
package metadata, ownership behavior available through the unprivileged sandbox, and scriptlets.

The host contract is intentionally small; its short list of requirements is documented in
[images.md](images.md).

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
pinned userspace. Development boxes and `image_vm` are the interactive consumers; build actions never use
relaxed mode.

### Native package installation

Native package installation has three phases shared by buildroots and images:

1. **Plan.** Run `PackageSystemInfo.plan` against each configured repository's materialized directory,
   effective priority, and solver cache. Weak dependencies are disabled. Existing lower layers are mounted
   read-only so installed packages can satisfy an incremental request.
2. **Select.** Use the resulting transaction to select raw package files from repository pools, named with
   the package system's declared suffix so its installer finds them. A local repository is materialized by
   an anonymous indexing target using the consuming package manager's engine. The same path handles
   package-build inputs and lets the libdnf5 solve choose between local and upstream packages.
   Extra packages arrive on two mutually exclusive paths: a package build passes its explicit
   `buildroot_deps` outputs, while a package manager with attached `local_packages` computes the request's
   runtime closure at analysis time from imported metadata and offers exactly the locally built packages
   in it. Buildroots reject managers with local packages, because buildroot contents must come from the
   explicit, cycle-checked self-hosting locks.
3. **Install.** Run `PackageSystemInfo.install` over the exact package directory. `install_packages()` owns a
   fresh root or incremental buildroot delta. An image layer instead invokes the same installer against its
   already-mounted root so package and filesystem operations have one output owner.

The package manager assigns default priorities when it configures repositories: local repositories use 50
and remote repositories use 99, with target-specific overrides applied by `package_manager()`. A planner
action carries its repositories as `{id, directory, priority, baseurl}` objects in its spec.
`encode_repositories()` projects them from `ConfiguredPackageRepositoryInfo`; the record's `dependency` is
deliberately stripped because Buck dependencies are analysis-only and not JSON-serializable. `plan.py`
reads each object into its `Repository` named tuple. The directory selects pinned local repodata, while the
base URL records the transport for remote packages selected into a transaction.

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

The OS.git repository's `packages/` tree contains imported source-package metadata and generated BUCK
files. The importer emits data; the Starlark in `package_system/rpm/generated.bzl` validates that data and
creates targets.

For each branch, `rpm_branch()` currently:

- combines common and `x86_64` BuildRequires;
- indexes each imported binary package's name, `Provides`, and file paths;
- maps BuildRequires capabilities to source-package targets;
- computes strongly connected components and drops ordinary intra-cycle edges to upstream packages;
- retains explicitly configured buildroot-only edges and rejects cycles they reintroduce;
- creates one `rpm_package` target per source package;
- publishes the branch's binary-package runtime metadata as a `:_local_packages` target for
  manager-attached local package selection.

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

The `image` rule creates either an initial image from a package manager or engine, or a derived image from
`parent`. `ImageInfo` carries the package manager, its engine, an ordered stack of filesystem deltas,
deferred tmpfiles snippets, and the canonical lazy package-database and SBOM artifacts. A derived image
inherits the construction configuration but declares fresh metadata for its completed stack, so one
composition cannot silently switch package sources or tooling environments between stages. An engine may
be supplied directly for an initial image that never installs native packages.

Each `image` call applies one ordered operation sequence in one action and persists exactly one overlay
upper. It may contain one `install` or `install_package_set` operation at any position; the selected
package-system installer operates on the already-mounted root, while `run`, `copy`, `mkdir`, `symlink`, and
`remove` mutate the same root. A package-set operation resolves its symbolic name through the image's package
manager during analysis, then becomes an ordinary install operation. `copy` introduces a declared Buck
artifact at an absolute image path, preserving its position relative to the other operations. `run` executes
the image's own tools in a chroot by default; `chroot = False` instead executes engine tooling with the image
available at `/buildroot`. Its `env` argument overlays variables on the engine or image environment for that
command. Package installation and copying always run outside the chroot. The `image()` declaration macro
recursively flattens operation lists, allowing reusable helpers to return ordered groups of operations.
Every rule that accepts operations is wrapped in a declaration macro that flattens them the same way, so a
helper returning an ordered group works identically in `image()` and in a composition; the underlying
attribute stays a flat, typed list so Buck can track every embedded source dependency.

`install_langs` narrows the install to the translated files of the named languages, and `install_docs`
drops documentation while keeping licenses. Neither ever enters the tree, so the package database records
them as not installed rather than claiming files that are absent. They belong to one `image` operation
sequence, not the logical image's lifetime: an initrd needs neither, while the root filesystem it boots may
want both. Each package system implements them in its own driver. A sequence that installs nothing ignores
both, so a composition can forward them to a layer whose operations happen to skip installation.

An `image` exposes its newly persisted delta, the full stack and canonical metadata through `ImageInfo`, and
the same metadata through lazy `[pkgdb]` and `[sbom]` subtargets. `ImageSbomInfo` gives typed consumers only
the two SBOM formats. Declaring a logical image declares its metadata with it, because they are intrinsic
metadata facets of every logical image: an `ImageInfo` therefore always carries an SBOM, and a package
database whenever the image has a package manager at all. An artifact referenced by a provider remains
lazy. Selecting either SBOM format runs one shared scan; the default image build runs neither metadata
action. Filesystem materialization remains an explicit terminal operation, so logical image construction
does not depend on an archive or disk format.

Package installation and image tooling remain separate concerns:

- the bootstrap `package_manager` determines what native packages can be resolved;
- that manager's `engine` supplies every layer driver and terminal image tool;
- `local_repository` declares compatible package outputs and infers their package system from
  `LocalPackageInfo`; a consuming package manager materializes deterministic repodata with its own engine;
- a derived package manager adds such repositories to a base manager's configured selection;
- a manager's `local_packages` instead selects locally built packages per install by runtime closure.

A project can therefore expose locally built packages without adding them to its OS release, or a package
manager may instead attach a branch's generated local-packages universe; worked examples of both
declarations are in [images.md](images.md).

Each install operation then computes the runtime closure of its requested packages at analysis time over
the imported Requires/Provides metadata, builds exactly the locally built packages in that closure, and
offers them to the solver ahead of the upstream repositories. Requested capabilities without a local
provider continue to resolve upstream, so partially imported branches simply mix. `ImageInfo` accumulates
its layers' install specs and seeds every later closure with them, keeping lower-layer packages locally
backed in later solves. Unlike a static `local_repository`, only the packages an install actually pulls
in are built; the universe target itself never forces a package build.

Logical images and terminal outputs are separate rule families. Terminal rules merge the stack only when
needed. The catalog of terminal rules (`image_archive`, `image_directory`, `uki`, `repart`, `bootable`,
`image_sysext`, and `image_vm`) and the `rootfs_archive`, `sysext_image`, and `bootable_disk_image`
composition rules are documented in [images.md](images.md). All of them, with the operation helpers and the
conventional partition layouts, are re-exported from `tine//image:defs.bzl`; that facade is the public API,
and the modules behind it are implementation structure. Every rule resolves its drivers through one
`ImageToolsInfo` bundle at `tine//image:tools` instead of a private attribute per driver.

The package database and SBOMs are supply-chain outputs read from the assembled image, never shipped in it.
Declaring any logical image declares both lazy facets, so compositions inherit them rather than repeating a
metadata step. Each package
system captures its database in its native shape. SBOM scanning of the whole tree additionally catches
packages no package manager knows about, such as Go modules bundled into ELF binaries.

Terminal rules leave the package database and other package state intact — except `image_sysext`, which
drops the database from the paths the image's package system declares: a merged extension must not shadow
the host's. Image cleanup is an explicit, configurable layer so output formats do not silently alter image
contents. Every terminal driver receives the same ordered layer stack and deferred tmpfiles snippets. It
applies those snippets with
`systemd-tmpfiles --root` before reading or emitting image content; a missing tool is an error whenever
finalization is needed. The directives
run against a disposable overlay upper and can create paths or restore modes and xattrs. Image-shipped
tmpfiles configuration is not applied implicitly; enabling it will be an explicit output option once its
single-UID/GID behavior is defined. Ownership and named ACL entries are deliberately unsupported: all
archive entries use uid/gid zero.

Tar uses deterministic PAX archives and stores Linux xattrs using `SCHILY.xattr.*` headers. The newc cpio
format has no general xattr representation. Tar/cpio entries are ordered and mtimes are clamped to the fixed
assembly epoch. The cpio reader/writer aligns regular-file payloads and uses `copy_file_range` when possible
so large archives can share extents on reflink-capable filesystems. `compression = "zstd"` compresses the
finished archive in the same action, so the uncompressed form never becomes a Buck artifact; zstd's
multi-threaded output is byte-identical to its single-threaded output, so this stays reproducible. The initrd
uses it, while the kernel-modules cpio that `uki.py` appends stays raw because Fedora already ships each
module compressed; the kernel unpacks the concatenation as independently compressed segments.

`repart` deliberately distinguishes `definitions` from `partitions`. Definitions describe new partitions
to populate directly from `ImageInfo`: repart mounts the delta stack with a disposable overlay upper instead
of first copying a directory artifact. Partition inputs are `RepartInfo` outputs from an earlier split call;
their blocks are copied into the new disk with their resolved type and UUID preserved. Calls emit a disk by
default. `split = True` additionally exposes each newly defined partition with normalized metadata alongside
its block artifact; `disk = False` makes such a call partition-only. No partial disk is passed between
actions. The partition artifacts are portable, so `RepartInfo` carries no engine: repart uses the
destination `ImageInfo.engine`, while standalone conversion and VM rules select an engine explicitly.
`image_directory` is an independent terminal view and is never an input to repart.

Partition layouts are always explicit inputs; neither `repart` nor `bootable_disk_image` chooses one
implicitly. The reusable conventional layouts are listed in [images.md](images.md).

Partition labels may contain `{image_id}` and `{version}` placeholders. `format_partition_labels()`
renders them — `bootable_disk_image()` calls it with its own image identity, raw `repart()` users call it
themselves, and unrendered placeholders fail the build. The usr-verity layouts carry such labels to
produce the `<id>_<version>[_verity[_sig]]` names that systemd-sysupdate A/B slot matching expects.
Rendered labels pass through `partition()` again, so GPT's 36-character label limit is enforced on the
final value.

Verity data, hash, and optional signature partitions are produced together in the split action. The root or
usr hash is an artifact because its value is known only after execution. `RepartInfo.root_hash` carries an
optional `RootHashInfo`, allowing `uki` to consume the hash without another top-level provider. One split
call may produce at most one such hash. This artifact boundary also ensures the final disk contains exactly
the partition bytes whose hash was embedded in the UKI. The hash is also available as the split target's
`roothash` subtarget. Signature partitions require an explicitly declared key and certificate.

`bootable_disk_image()` composes:

```text
base initrd package image (zstd cpio)
              ├──────┐
root filesystem layer ──> identity layer ──> split /usr + verity ──> hash ──> versioned UKIs
                                │                                               │
                                └──────────────────────┬────────────────────────┘
                                                       v
                                              ESP layer
                                              │          │
                                              │          └─> terminal views and supply-chain artifacts
                                              └─> split ESP + system partitions ─> bootable-image result
```

The identity layer stamps `IMAGE_ID` and `IMAGE_VERSION` into the image's os-release. It is a separate
thin layer so that a per-commit version string invalidates only the version-embedding artifacts below it,
never package installation.

With Secure Boot key material, the identity layer also signs the systemd-boot binary in place, as a
`.signed` sibling under `/usr/lib/systemd/boot/efi`. The signed binary must live in the image's own
`/usr`, covered by the verity root hash, not just on the ESP: the booted system's `bootctl update`
(`systemd-boot-update.service`) reinstalls the bootloader from that path after an OS update, and an
unsigned binary there would fail Secure Boot verification on the next reboot. `bootctl` prefers the
`.signed` sibling when populating the ESP. Only the UKI is built and signed outside the tree: its
command line embeds the root hash, so it can only exist after `/usr` is sealed, and it lives solely on
the ESP. The verity partition itself stays unsigned in this scheme: the signed UKI command line pins the
verity root hash, so its trust derives from the Secure Boot signature.

By default, the base initrd is a separate package image with `/init` pointing to systemd and
`/etc/initrd-release` pointing to `/etc/os-release`, installing the release's `initrd` package set, so family
catalog policy supplies concrete native package names. Callers can instead supply any target providing
`ImageInfo`; the rule consumes the resolved provider and skips the default initrd image entirely. It does
not require `InitrdInfo` as an input or infer cpio, SBOM, or pkgdb target names. Instead, it terminalizes the
supplied logical image itself: it creates the zstd cpio consumed by the UKI and republishes the package
database and SBOM that same `ImageInfo` already carries. That cpio omits the database from the paths the
image's package system declares: nothing in an initrd resolves a dependency or verifies a package, and the
kernel unpacks the whole cpio into tmpfs, so shipping it would only cost boot memory. The `[pkgdb]` and
`[sbom]` views read the tree rather than the archive, so they still describe the complete installed set.
It then combines that image and the derived `ImageArchiveInfo` into `InitrdInfo`. The composition returns
this provider directly and publishes the same instance from `[initrd]`, so both interfaces describe exactly
the same initrd.

`uki.py` appends the kernel-modules cpio and runs `ukify`; the UKI is named
`<image_id>_<version>_<arch>.efi` from the image identity. For now an image holds exactly one kernel — the
name (and sysupdate's matching of it) could not distinguish more. If several kernels per image ever become
a requirement, add naming configuration to `uki()` to disambiguate them. The ESP layer copies the UKI
directory into `EFI/Linux` and includes the operations returned by `install_systemd_boot()`. Those create
the ESP path, run the engine's `bootctl` with its paths in the command environment, and remove the random
seed. The final repart action creates and exports the ESP while copying the previously split system
partitions into the same disk. The default system partition is a compressed EROFS `/usr` protected by
dm-verity; the generated `usrhash=` is embedded in every UKI. The same copy operation can place device
trees, bootloader entries, and future standalone artifacts; `esp_files` exposes it, copying caller-declared
artifacts to chosen ESP paths.

Kernel command lines remain lists of arguments through the Starlark API and driver invocation. The UKI
driver appends any generated verity hash and joins the arguments only when writing ukify's command-line file.
Boot profiles become small PE binaries of `.profile` and `.cmdline` sections, built against the image's
addon stub and joined into every UKI; a profile's arguments extend the shared base command line (including
the verity hash), and kernel arguments are last-wins, so profiles can also override it.

A composed raw disk can be re-encoded into distributable formats without rebuilding it: `disk_convert`
uses its explicit engine to drive `qemu-img` for a compact qcow2 and `zstd` for a compressed raw. These are
alternative encodings of the same disk and remain separate, reusable terminal implementations rather than
default outputs. A converted target provides `DiskConversionInfo`, naming the format alongside its artifact,
so one provider covers every encoding instead of one provider type per format.

#### Bootable-image result

Bootability and output format remain independent terminal capabilities, but the artifacts declared by one
`bootable_disk_image()` invocation form one concrete product. The composition publishes one target at
the requested name. Its default output and `RepartInfo.disk` are the raw disk; its other terminal views,
supply-chain metadata, and constituents are lazy subtargets:

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
├── [initrd]                    # default output: zstd cpio
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

Typed providers are the composition API; subtargets are the command-line interface. There is no aggregate
bootable-image provider. The rule returns its completed `ImageInfo`, `RepartInfo`, `InitrdInfo`,
`ImageDirectoryInfo`, and `UkiInfo` independently. `RepartInfo` holds the composed raw disk, its independent
partitions, and optional `RootHashInfo`, rather than exposing separate top-level providers for these facets.
Its qcow2 and compressed-raw encodings are reachable only through their subtargets, each publishing one
`DiskConversionInfo`: a single provider type cannot appear twice in one result, and "the conversion" of a
target that has several is ambiguous anyway. Every other constituent subtarget publishes the same provider
instance as the main target. This lets a
consumer request exactly the capability it needs without fields duplicating another provider's data.
Storing an artifact in a provider does not build it. Optional actions run only when a consumer uses the
corresponding artifact or a user selects its subtarget. They must not appear in the result's
`DefaultInfo.default_outputs` or `DefaultInfo.other_outputs`. SPDX and CycloneDX still come from one scan,
so requesting either format runs the same SBOM action.

`bootable_disk_image` is one rule that owns the complete composition action graph. Provider-oriented action
helpers are shared with the standalone `image`, `repart`, `uki`, and terminal-format rules, so the
composition reuses their implementations without declaring private sibling targets or forwarding through a
result rule. This aggregation remains scoped to the concrete bootable-image product; it does not restore a
generic `image_result` around every logical `ImageInfo`.

`rootfs_archive` and `sysext_image` follow the same ownership model on a smaller graph: one rule declares
the logical image with its lazy package-database/SBOM views, and its terminal archive or DDI. Each target
returns `ImageInfo` alongside its terminal provider, so consumers never depend on generated `.layer`,
`.pkgdb`, or `.sbom` labels. The standalone terminal rules share the same provider-oriented declaration
functions.

The disk, directory, package database, and SBOM derive from the same ESP layer. The initrd is a second
logical image with its own package closure, so the composition creates its cpio from that `ImageInfo` and
republishes the package database and SBOM the image carries. The `[initrd]` subtarget exposes only the
encompassing `InitrdInfo` as its typed contract, plus the image's metadata as nested subtargets. The main
target returns that same `InitrdInfo` directly. Supplying a custom initrd therefore requires only one
regular logical-image target and never a family of conventionally named siblings. VM runners remain separate
targets because execution policy and credentials are behavior, not facets of the image artifact.

The standalone `bootable` rule extracts semantic boot artifacts lazily from a completed logical image
rather than forwarding whichever intermediate target created them; it earns its keep on images whose
kernels arrive through package installation rather than a composition-built UKI. A boot-specific selector
chooses the newest valid UKI by its embedded kernel release and writes a generic artifact manifest. The
image artifact driver only extracts a named path or PE section from that manifest. Without a UKI, the
selector chooses the newest standalone kernel. In either case, bootability requires an initrd matching that
exact release. A UKI remains an optional extraction: requesting it fails if the selected image has only
standalone artifacts. Selecting the `[directory]` subtarget does not assemble the disk, and repart never
materializes the directory.

Repart derives stable UUID seeds from target identity and logical configuration; callers can override them
explicitly. VM runners are declared separately from disk composition. Their execution engine and runtime
policy are passed to `image_vm` rather than baked into the disk provider or image.

The image build tools live in the engine and are not installed into the image merely to build it. Chrooted
`run` operations intentionally use the image's own binaries; non-chrooted runs explicitly use engine tools
against `/buildroot`.

### Reproducibility and caching

Reproducibility is both a release property and a caching requirement. Current mechanisms include:

- repository metadata, package bytes, and source archives pinned by SHA-256;
- generated or committed engine transactions containing repository/package identities;
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

### Commit repository snapshots and selectively freeze engines

Repository snapshots are committed so normal resolution remains independent of live repository state and
reviewable. Engines with a predecessor resolve a cacheable transaction from those pins during their build.
Committing an engine transaction is an explicit freeze operation for bootstrap roots, releases, or other
engines that must remain stable across repository snapshot updates. Unlike an ordinary language lockfile, a
frozen engine transaction also retains the transport for packages needed after a rolling repository
advances.

### Let repositories own upstream packages

Putting downloads in each closure duplicated ownership and left derived operations without a stable home.
The authoritative named pool instead gives every upstream package one action owner per repository. The
current snapshot defines available packages, while optional committed engine locks retain older packages
required by frozen transactions. Closures select artifacts; they do not fetch or transform them.

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

### Pass drivers one JSON spec

A rule describes an action to its driver as a single JSON spec written with `write_json`, invoked as
`<driver> --spec <spec.json>`, rather than as a command line. Buck resolves artifact paths inside the spec,
so declared outputs are named there exactly like inputs and neither has to be flattened into repeated
options or positional triples. Structured configuration (layer operations, partition definitions, boot
profiles, repository selections, subpackage outputs) stays structured, and a driver reads the schema its
rule owns instead of revalidating an argument grammar. A driver that invokes another driver does the same:
the image layer driver writes an install spec for the package installer.

Three exceptions are deliberate. The planner keeps its `solve`/`make-cache` verb on the command line, since
each verb has its own spec schema. Both it and the snapshot driver keep `--out` there too: their `[resolve]`
and `[snapshot]` run targets let a caller name the file to write, and one calling convention per driver
beats splitting the destination by verb. The RPM payload decompressor keeps its two positional paths: it is
declared once per package in a repository pool, where a spec file per package would double that part of the
graph for a driver that has no configuration at all.

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

### Keep static local repositories and closure-selected local packages separate

`local_repository` and a manager's `local_packages` both prefer locally built packages over upstream and
share the per-install `extra` materialization path, but they are deliberately not unified. A
`local_repository` publishes an explicit, hand-listed set and forces those packages to build; it suits a
project exposing a few of its own packages. `LocalPackageUniverseInfo` instead describes a whole imported
branch and builds only the packages an install's runtime closure actually pulls in, so attaching the
universe never forces the branch to build. Collapsing them would either force building an entire branch or
push closure computation into every static repository, so both remain until the planned per-package
source/prebuilt provider model (see Roadmap) subsumes them.

## Operating the current system

Host requirements, the wrapper commands, and representative smoke builds are documented in
[images.md](images.md).

## Current limitations

These are properties of the implementation today, not merely ideas for future optimization:

- RPM is the only package system, and generated package metadata is fixed to `x86_64`.
- The imported self-host dependency graph is inferred from stored BuildRequires/Provides/file metadata; it
  does not run RPM's dynamic BuildRequires protocol.
- Build cycles fall back to upstream RPMs for ordinary intra-SCC edges, so the package set is not a fully
  self-hosted fixed point.
- Image installs select source-built packages through the imported-metadata runtime closure of
  `local_packages`. The walk follows local-to-local edges only, so a local package reachable only through
  an upstream intermediate silently resolves upstream, and there is still no per-package source/prebuilt
  choice under one shared version pin.
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
- Bootable images currently disable SELinux. Verity and Secure Boot/expected-PCR signing accept declared
  development key material, but production signing boundaries are not implemented.
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
- production verity, Secure Boot, and expected-PCR signing through a restricted key boundary;
- OCI, confext, ESP, and other terminal formats as real consumers require them;
- sysext verity signing;
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

- `tine/package/{system,repository,release,manager,solver,buildroot,install}.bzl`
- `tine/package_system/rpm/rules.bzl` and
  `tine/package_system/rpm/{snapshot,plan,install,pkgdb,createrepo,build,extract,decompress}.py`
- `tine/engine/{build,runtime}.bzl`, `tine/engine/sandbox.py`, and `tine/rootfs/rootfs.py`
- `tine/image/{image,compose,defs,sign,vm}.bzl` and `tine/image_format/{archive,boot,disk,sysext,uki}.bzl`
- `tine/tools/catalog.py` and `tine/catalog/BUCK`
- the generated `packages/*/*/BUCK` and `tine/package_system/rpm/generated.bzl`

External projects that informed the design:

- Buck2 for action/dynamic-dependency semantics, sub-targets, content-based paths, and test execution;
- rpm and libdnf5 for build, resolution, transaction, and signature behavior;
- mkosi/mkosi-sandbox for user-namespace isolation, root mounting, UKIs, and repart-based images;
- Barrage for the planned streamed integration-test executor;
- Siguldry for a possible production PKCS#11 signing boundary.
