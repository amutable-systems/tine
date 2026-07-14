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

The `catalog` cell is consumer-overridable. Repository URLs, OS releases, engine package lists, package
managers, and buildroots are data owned by the active catalog rather than hard-coded in the reusable rules.
The `buildroots` cell maps importer-generated names such as `buildroots//fedora:rawhide` to catalog targets.

Catalog targets use `<family>.<release>[.<component>].<role>` names. A rolling channel such as Rawhide
occupies the release segment. Singular `.repository` targets own remotes, plural `.repositories` targets
define universes, and release, engine, package-manager, and buildroot targets use their corresponding
suffixes. Declaration macros require the suffix appropriate to their role. An engine's identity describes
its provenance rather than every release that may consume it.

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
- `PackageRepositoryInfo` represents one repository and binds it to a package system. A repository is not
  inherently owned by an OS release.
- `RepositoryUniverseInfo` defines one homogeneous solve universe: required repositories, named optional
  groups, and groups enabled by default. Selection preserves declaration order, de-duplicates identical
  targets, and rejects conflicting repository IDs.
- `OsReleaseInfo` associates OS identity with one repository universe and supplies the base of an engine.
- `PackageManagerInfo` selects the exact repositories used for a solve, applies priority overrides, chooses
  an engine, and owns reusable solver caches.
- `BuildrootInfo` materializes the shared base root installed by a package manager.
- `EngineInfo` contains a runnable root filesystem, its base release, and the sandbox used to enter it. Its
  base establishes provenance; the engine may serve compatible package managers for other releases.

This split is visible in the default catalog. Fedora 44, Rawhide, and CentOS Stream 10 are separate OS
releases. Their package managers solve against their own repositories while sharing the Rawhide engine.
CentOS models BaseOS as required, AppStream as a default repository group, and CRB as an optional group
enabled by the current package manager.

An image bootstrap target fixes one engine for the lifetime of the logical image. Layers name a package
manager only when they install native packages, and that manager must use the same engine. Mutation,
packing, UKI, disk, and VM rules inherit the engine through their image input. A package target names a
buildroot because its shared base root, not OS identity alone, is its relevant input.

### Catalog pinning and refresh

Normal builds do not resolve against live network repositories. The catalog contains two generated forms
of committed lock data:

- `<repository>.json` pins filtered `repomd.xml`, the primary/filelists/group streams needed by libdnf5,
  and the complete primary-metadata package inventory keyed by SHA-256 `pkgid`;
- `<engine>.json` pins the engine transaction as a list of `{source, repo, pkgid, nevra}` records.

`tine/tools/buck run tine//tools:refresh-catalog` refreshes them in two phases:

1. Run every remote repository's `[snapshot]` sub-target on the host. `snapshot.py` downloads and verifies
   repodata, drops unused streams, validates package locations, and writes deterministic JSON.
2. Run every engine's `[resolve]` sub-target inside the current engine. `plan.py` resolves the authored
   top-level engine package list against the freshly pinned repository trees and writes the new transaction.

`verify-catalog` performs the same generation and fails when committed JSON differs. Repository snapshots
are ordinary Buck source inputs, so changes invalidate only consumers of the changed data.

`rpm_remote_repository()` derives its optional snapshot from `<target-name>.json`. This lets a new
repository target analyze before its first refresh; consuming its empty package pool fails with an explicit
instruction to refresh the catalog. A new engine lock is seeded as JSON data and must contain a usable
bootstrap transaction before that engine can resolve itself.

### Authoritative repository package pools

Each remote repository target owns its package artifacts. Its dynamic value expands the committed snapshot
into:

- reconstructed pinned repodata;
- one digest-checked raw RPM artifact per `pkgid`;
- one decompressed cpio payload representation per RPM.

The raw RPM and derived payload are alternative representations of the same `package_artifact` record.
Downloads and decompression are registered once under repository ownership, rather than under every engine,
buildroot, or image closure.

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

1. **Plan.** Run `PackageSystemInfo.plan` against the package manager's exact repositories, priorities, and
   solver cache. Weak dependencies are disabled. Existing lower layers are mounted read-only so installed
   packages can satisfy an incremental request.
2. **Select.** Use the resulting transaction to select raw RPMs from repository pools. If local package
   outputs are present, an anonymous target runs `createrepo`, adds that repository at a higher priority,
   and lets the same libdnf5 solve choose between local and upstream packages.
3. **Install.** Run `PackageSystemInfo.install` over the exact RPM directory. `install_packages()` owns a
   fresh root or incremental buildroot delta. An image layer instead invokes the same installer against its
   already-mounted root so package and filesystem operations have one output owner.

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
5. collects binary RPMs and the source RPM into one output directory;
6. exposes each declared binary subpackage as a Buck sub-target and checks that declared outputs exist.

The build currently uses `--nocheck`. Automatically generated debuginfo/debugsource RPMs are retained in the
directory output but are tolerated rather than exposed as declared sub-targets. Successful build scratch is
discarded; failed scratch remains available for diagnosis.

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

The `image` rule bootstraps an empty logical image and fixes its engine. `ImageInfo` carries that engine, an
ordered stack of filesystem deltas, and deferred tmpfiles snippets. Every derived image and terminal output
inherits the engine, so one composition cannot silently switch tooling environments between stages.

`image_layer` applies one ordered operation sequence in one action and persists exactly one overlay upper.
It may contain one `install` operation at any position; the selected package-system installer operates on
the already-mounted root, while `run`, `mkdir`, `symlink`, and `remove` mutate the same root. `run` executes
the image's own tools in a chroot by default; `chroot = False` instead executes engine tooling with the image
available at `/buildroot`. Package installation always runs outside the chroot. Installing packages requires
a package manager backed by the image's engine; analysis rejects a mismatched manager.

Package installation and image tooling remain separate concerns:

- `package_manager` determines what native packages can be resolved;
- the bootstrap `engine` supplies every layer driver and terminal image tool;
- `extra_packages` allows a direct layer rule to prefer local RPM output directories.

Logical images and terminal outputs are separate rule families. Terminal rules merge the stack only when
needed:

- `image_archive` writes deterministic tar or uncompressed newc cpio archives;
- `image_directory` materializes a Buck directory artifact;
- `uki` builds a standalone unified kernel image from a logical image and one or more initrds;
- `boot_layer` installs standalone boot artifacts and optionally systemd-boot as another persisted delta;
- `image_disk` uses offline `systemd-repart` to create a GPT image with an ESP and discoverable root
  partition by default;
- `image_vm` runs the raw image ephemerally with the engine's `systemd-vmspawn`, QEMU, and OVMF stack.

`rootfs_archive()` is the convenience composition for building a single layer from operations and emitting
an archive. `image_archive` remains the terminal rule for archiving an existing logical image.

Terminal rules leave the rpmdb and other package state intact. Image cleanup is an explicit, configurable
layer so output formats do not silently alter image contents. Terminal rules apply deferred tmpfiles lines
with `systemd-tmpfiles --root`; a missing tool is an error whenever finalization is needed. These lines can
create paths and restore modes or xattrs, but ownership is deliberately unsupported: all archive entries use
uid/gid zero, matching the single-user namespace used for assembly.

Tar uses deterministic PAX archives and stores Linux xattrs using `SCHILY.xattr.*` headers. The newc cpio
format has no general xattr representation. Tar/cpio entries are ordered and mtimes are clamped to the fixed
assembly epoch. The cpio reader/writer aligns regular-file payloads and uses `copy_file_range` when possible
so large archives can share extents on reflink-capable filesystems.

`bootable_disk_image()` composes:

```text
base initrd package image (cpio)
              ├──────┐
root filesystem layer ──> standalone UKI ──> boot-artifact/systemd-boot layer ──> raw GPT disk
```

The base initrd is a separate package image with `/init` pointing to systemd and an
`/etc/initrd-release` marker. `uki.py` discovers or selects the installed kernel, appends a kernel-modules
cpio, and runs `ukify`. The generic boot layer can place UKIs, device trees, bootloader entries, and future
boot artifacts before installing the selected bootloader. The default disk derives a stable UUID seed from
its target identity and partition definitions; callers can override it explicitly. VM runners are declared
separately from disk composition. Runtime policy is passed to `image_vm` rather than baked into the image.
Its `autologin` option provisions a locked root password and runtime `login.noauth`; arbitrary non-secret
system credentials configure settings such as first-boot locale and timezone. The smoke image uses
`console=hvc0 rw selinux=0`; SELinux is disabled because the build does not yet produce filesystem labels.

The image build tools live in the engine and are not installed into the image merely to build it. Chrooted
`run` operations intentionally use the image's own binaries; non-chrooted runs explicitly use engine tools
against `/buildroot`.

### Reproducibility and caching

Reproducibility is both a release property and a caching requirement. Current mechanisms include:

- repository metadata, package bytes, and source archives pinned by SHA-256;
- committed engine transactions containing repository/package identities;
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
analogous to a language dependency lockfile, only it also pins repository metadata.

### Let repositories own upstream packages

Putting downloads in each closure duplicated ownership and left derived operations without a stable home.
The authoritative pool instead makes the repository snapshot the single definition of every upstream
package. Closures select artifacts; they do not fetch or transform them.

### Separate package system, OS release, package manager, and buildroot

The former distribution object bundled repository membership, engine tooling, and buildroot policy. That
made optional repositories awkward, implied that an engine had to match every target release it operated
on, and provided no clean place for request-specific local repositories.

The current vocabulary follows the actual responsibilities:

- the package system defines operations;
- the repository universe defines membership and normal enablement policy;
- the OS release defines identity, selects a repository universe, and may provide an engine's base;
- the package manager defines one exact solve universe and engine;
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
can still operate on Fedora 44 and CentOS Stream. Keeping the engine dependency explicit at an image's
bootstrap makes reuse visible and content-keyed, avoids duplicating compatible tooling roots, and lets
images choose richer tools without shipping those tools.

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
tine/tools/buck build root//examples/image:boot-demo
```

The first validates package import, package-manager selection, buildroot assembly, and RPM collection.
Packages with `buildroot_deps` additionally exercise local-package preference. The image targets validate
package installation and commands sharing one delta, incremental layering, archive packing, standalone UKI
creation, boot-layer assembly, and disk composition. Running `boot-demo-vm` validates the interactive VM
runner; ephemeral mode preserves the Buck disk artifact.

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
- Bootable images currently disable SELinux and do not use dm-verity or Secure Boot signing.
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
- dm-verity and measured/Secure Boot integration;
- OCI, sysext/confext, ESP, and other terminal formats as real consumers require them;
- a richer but still ordered operation vocabulary for copying general artifacts and setting metadata;
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
