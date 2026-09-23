<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# tine architecture

This document describes the architecture implemented in this repository, the decisions that shaped it,
and the work that remains. It is a living architecture document, not a chronological implementation plan.

Statements under **Current architecture** describe code that exists. **Roadmap** describes accepted or
possible future work and labels open questions explicitly. When implementation and this document disagree,
the implementation is authoritative and this document should be corrected.

Anything specific to one native package system belongs to that system's own section. The rest of this
document describes machinery that does not know which system it is driving, and names none.

User-facing guides live in [docs/user](../user): [images.md](../user/images.md) covers building and
running images, and [importer.md](../user/importer.md) covers maintaining packages with the importer.

## Purpose and scope

tine uses Buck2 to build native packages and compose operating-system images. The long-term goal is a
monorepo in which a useful core package set is rebuilt from source, scheduled in dependency order, cached
by content, and suitable for remote execution. The current implementation already provides:

- two native package systems, each with pinned repositories and a repository-owned package artifact pool;
- bootstrap box roots containing the pinned userspace used by build actions;
- configured package managers and shared buildroots for several OS releases;
- package builds from imported source metadata, including self-hosted buildroot dependencies;
- layered filesystem images, deterministic archives, bootable GPT disks, and a VM runner.

The system does not yet claim complete source provenance, remote execution, or a full release pipeline.
Images currently mix locally built packages with pinned upstream packages.

## Current architecture

### Repository and cell layout

This repository is the `tine` cell; a consuming project points a `tine` cell at a checkout of it and adds
its own package tree:

```text
//packages/               (consuming project) independently versioned package sources and BUCK files
//examples/image-local-packages/ (consuming project) bootable image from self-built packages
tine//examples/image/     image smoke targets, built for each distribution
tine//examples/box/       pinned interactive development environment
tine//distribution/       the axis an image's distribution is selected on
tine//package/            package-system-neutral providers and installation flow
tine//package_system/     one directory per package system: its repository, resolver, installer,
                          extractor, indexer, and optional builder
tine//box/                box bootstrap and sandbox command construction
tine//rootfs/             bind/overlay mounting and stored-delta translation
tine//image/              layers, boot artifacts, composition macros, and VM runners
tine//image_format/       archive, directory, and raw-disk output rules and drivers
tine//git/                pinned Git repositories with local checkout overrides
tine//cargo/              vendored crate trees and offline Rust source builds
tine//go/                 go.sum-verified module fetches and offline Go source builds
tine//catalog/            default repositories, locks, releases, package managers, and buildroots
tine//tools/              pinned development and catalog-refresh commands, and the boxes they run in
tine//bin/                the command a checkout builds through, which fetches and configures Buck2
```

The default `tine//catalog` package owns its release selection, mirrors, box choice, repository additions,
and policy overrides. Projects can instead declare their own catalog package with tine's reusable family
macros or low-level rules. The project's `//buildroots` package maps importer-generated
`//buildroots/<family>:<release>` names to catalog targets.

Catalog targets use `<family>.<release>[.<component>].<role>` names. A rolling channel occupies the release
segment like any other release. Singular `.repository` targets own remotes, plural `.repositories` targets
define universes, and release, box, package-manager, and buildroot targets use their corresponding
suffixes. Low-level declaration macros require the suffix appropriate to their role. The family catalog
macros instead take a `<family>.<release>` prefix and declare the complete repository, universe, release,
package-manager, and buildroot bundle. A box's identity describes its provenance rather than every
release that may consume it.

The package source tree lives in the OS.git repository (which consumes this `tine` cell) under `packages/`.
It is intentionally not part of the reusable `tine` cell: package policy and imported source data change
independently of build machinery.

Buck2 looks every rule's toolchain up in a cell named `toolchains`. tine declares the only one it needs,
the bootstrap Python interpreter, in this cell's root package and aliases `toolchains` to `tine`;
`tine init` writes that alias into the consuming project's generated `.buckconfig`, so no project
declares a toolchain of its own. The obvious shape, a `toolchains/` cell in this repository, is
impossible: Buck2 forbids a nested cell inside an external cell, so every project consuming tine as one
needed a copy of that directory. An alias resolves through an external cell where a nested cell cannot
exist. A project that does need toolchains beyond the bootstrap one declares the cell itself, dropping
the alias and forwarding what it does not declare:

```Starlark
toolchain_alias(name = "python_bootstrap", actual = "tine//:python_bootstrap", visibility = ["PUBLIC"])
```

### Building with out-of-tree checkouts

A cell root must be project-relative, so Buck cannot point a cell directly at an external directory.
`tine mount` records local overrides in `.buck/tine-mounts.toml`, mapping project-relative targets to
absolute source directories. Targets are cell roots or local-checkout slots declared by `git_fetch()`.
They cannot overlap or cover `.buck/` or `.buckconfig.d/`: nested mounts would depend on application
order, and those directories hold the mount table and private configuration. Invalid declarations stop
the command rather than silently falling back to the checked-in directory.

For example, `tine mount` can make `/work/lib` appear at `vendor/lib` inside a project. tine sets up the
bind mounts in a private mount namespace before starting the Buck client, so uncommitted source edits
are visible without changing Buck's project-relative paths.

Before running Buck, tine selects the configured tine cell's `bin/tine`, whether or not any mounts were
needed. If its path differs from the current wrapper after resolving symlinks, tine re-executes it so
the command, rules, and Buck2 pin come from the same checkout. After creating mounts, tine always
re-executes: a mount may have replaced the wrapper at the same path. A configured cell without
`bin/tine` is an error, not a reason to keep running another checkout's wrapper.

#### Matching clients and daemons

A build needs a daemon with the right mounts, but it should not be stuck with an old copy of every
project setting. The Buck client sends build requests to a background process, the daemon. The daemon
keeps the mounts it started with. Changing a mount declaration does not change that daemon's mounts.

tine gives each set of mounts a label, called the mount digest, using the `[buck2] daemon_buster` setting.
The Buck client reads that setting from its config files. If it starts a daemon, it passes the label to
that daemon in its startup arguments. The daemon keeps that label and reports it to clients; it does not
reread the config to update it. Each later client reads its own config and compares its label with the
daemon's saved label before reusing the daemon.

Previously, tine put that label in a private copy of `.buckconfig`. This also hid later edits to ordinary
settings. For example, if the copy said `build.threads = 4` and the project changed it to `8`, processes
using that copy would still read `4`. Only the mount information needs to be private, not the whole
project config.

Putting the label in a shared file would introduce a different problem. Suppose two commands overlap,
with no daemon running yet:

1. Command A mounts `/work/lib-old` at `vendor/lib`, then pauses before starting its Buck client. Call
   the label for these mounts "old".
2. The mount declaration changes to `/work/lib-new`. Command B prepares those mounts and writes their
   "new" label to the shared config, but has not started its Buck client yet.
3. A's Buck client starts first. It reads "new" from the shared config and passes that label to a new
   daemon. The daemon saves "new", but inherits A's mounts, which still point at `/work/lib-old`.
4. B's Buck client reads "new" from the shared config and compares it with the label reported by the
   daemon. Both say "new", so B's client reuses that daemon and builds against `/work/lib-old` by mistake.

There is no mismatch for either client to detect. Killing mismatched daemons cannot help when the daemon
has already been given the wrong label. The build can succeed while using the wrong checkout.

Keep the label and the list of mounted project paths in a small private file,
`.buckconfig.d/tine-mounts/config`. Each command sees its own version of this file: A sees "old" and B
sees "new", even though the filename is the same. A private in-memory mount (`tmpfs`) provides that
separation. The Buck client reads this file before deciding whether to reuse a daemon, and passes the
label along if it starts a new one. The ordinary `.buckconfig` and `.buckconfig.local` files stay shared
rather than being frozen with the mounts. They still follow the refresh rules described under
[Shared configuration and nested commands](#shared-configuration-and-nested-commands).

The root-cell config layers are read in order: `.buckconfig.d/`, `.buckconfig`, then `.buckconfig.local`.
tine rejects project-owned `[buck2] daemon_buster` and `[tine] dev` while mounts are declared so that a
higher-precedence setting cannot replace the private label or mounted-path list.

#### Identifying the mounted directory

The label must describe the directory that was actually mounted. For example, after mounting `/work/lib`
at `vendor/lib`, someone could move `/work/lib` to `/work/lib-retired` and put a new checkout at
`/work/lib`. The existing mount still points to the original directory. Looking at `/work/lib` now would
describe the replacement instead. Calculate the label after mounting, using the directory reached
through `vendor/lib`. The digest includes the target path, source path, and mounted directory's inode.
It omits the device number because btrfs can assign a new one each time a subvolume is mounted.

Re-executing inside an existing mount namespace preserves both the namespace and its saved list of
mounted paths. tine does not reread declarations that another command may already have changed. Git
ignores are read through the mounted paths too, so they describe the checkout the build will use.

#### Git metadata in mounted checkouts

Submodules and linked worktrees can use a `.git` file that points to metadata outside the checkout. For
example, `/work/main/lib/.git` might contain `gitdir: ../.git/modules/lib`, referring to
`/work/main/.git/modules/lib`. After mounting that checkout at `vendor/lib`, the same pointer would look
under `vendor/.git/modules/lib` instead.

Before mounting, tine resolves each gitfile with `git rev-parse --absolute-git-dir` and saves the result
under `[tine] gitdirs` in `.buckconfig.d/tine-mounts/config`, as a JSON map from mount targets to absolute
metadata paths. Both relative and absolute gitfile pointers are recorded. Ordinary `.git` directories
remain accessible through the bind mount and need no saved path. Neither directories nor gitfiles are
rewritten.

Ignore queries run from the mounted checkout with `--git-dir=<saved path>` and `--work-tree=.`. The
explicit worktree matters because a submodule's `core.worktree` can still name its original source path.
If that path now holds a replacement checkout, following it would read the wrong `.gitignore`. Giving
Git both paths keeps the query on the mounted tree while retaining the saved metadata's `info/exclude`
rules and tracked-file index, so tracked files are not mistaken for ignored build output.

During configuration refresh, tine reads the map once from the project's private config and passes it
to both the project and tine-cell ignore queries, keyed by mounted checkout paths. With no mounts, it
skips this read. A mounted tine checkout's own private config is unrelated to this invocation and must
not supply this map; malformed Git-directory data there must not break the build.

#### Lock scope

A `flock()` on `.buck/tine-mount.lock` protects edits to the mount table, covering the read, validation,
and atomic replacement. Validation can run a nested `tine buck uquery` to discover new mount targets.
Buck commands take no mount lock: otherwise that query would wait for the mount command that is waiting
for the query.

A build can read either the old or new table, but cannot see a partly written one. Replacing a source
directory does not have to go through `tine mount`, so locking mount-table edits cannot prevent the
source-replacement case above.

The Buck client can still replace a daemon when the labels really do differ, interrupting its connected
clients as usual. Replacement does not remove `buck-out`; the decision to retain one output directory
is explained under [Use bind mounts for out-of-tree content](#use-bind-mounts-for-out-of-tree-content).

#### Shared configuration and nested commands

For a consuming project, an ordinary `tine buck` command regenerates the entire project `.buckconfig`
from the selected tine checkout's `.buckconfig`, applying `[buckconfig.*]` overrides from `tine.toml`
and `tine.local.toml`. Mount setup and wrapper selection happen first, so the same checkout supplies
both the wrapper and its defaults. Direct edits to the generated defaults are discarded; project
settings belong in TOML instead.

Cell selection uses the effective configuration, including `.buckconfig.local`. A local cell override
can select a different wrapper and defaults without being copied into the generated `.buckconfig`.
If TOML does not declare the tine cell, existing cell registrations in `.buckconfig` are retained while
other defaults are regenerated. This also supports consuming projects that declare their cells only
in native Buck configuration.

When the tine cell is the project root itself, it is a standalone checkout. Its `.buckconfig` is source
configuration and is not regenerated; put Buck overrides directly in that file rather than in TOML.

The `.buckconfig.local` update only replaces tine's generated block, which contains project ignores and
Git-derived image version components. Text outside that block is preserved.
Configured ignores are merged with VCS metadata exclusions and Git ignores; a project-owned
`[project] ignore` in `.buckconfig.local` is rejected because it would replace the generated list.
The version components live under `[tine]` as `version-base`, `version-count`, `version-height`,
`version-commit`, and, for uncommitted work, `version-dirty`. Each image renders those components against
its own label budget; see "Image versioning" in [images.md](../user/images.md).

Generated settings go into a file rather than command-line flags because the daemon's file watcher
reads its ignores from configuration files at startup, without command-line overrides. A file also
avoids the 128 KiB limit on a single argument, and `buck2 complete` accepts no configuration flags.
Target completion and `tine completion` select the configured wrapper without refreshing shared
configuration. Target completion uses the selected checkout's cached Buck2 without downloading. tine
rewrites Buck2's completion script so target queries run through `tine buck` too.

The selected tine checkout pins Buck2 in `tools/tools.json`. Projects can override the pin in the
`[buck2]` table of `tine.toml` or `tine.local.toml`, with per-platform fields under
`[buck2.platforms.<platform>]`.

tine exports the pinned binary's path as `BUCK2_BINARY`. A nested Buck command still selects the
configured wrapper, but inherits the current mounts and skips refreshing shared configuration
underneath the build that started it.

### Component model

The package model separates identity and policy from the exact inputs used by an action:

```text
PackageSystemInfo (one package system's drivers)
        │
        ├── PackageRepositoryInfo ──┐
        │                           ├── RepositoryUniverseInfo
        │                           │          │
        └───────────────────────────┴── OsReleaseInfo
                                               ├── box transaction ── BoxInfo ──┬── package build
                                               │                                ├── image tooling
                                               │                                │
                                               └────────────────────────────────┴── PackageManagerInfo
                                                                              ├── image installation
                                                                              └── BuildrootInfo
                                                                                     └── package build
```

The providers have deliberately narrow roles:

- `PackageSystemInfo` bundles the drivers for one native binary-package ecosystem: snapshot, extract,
  install, package database capture, repository indexing, plan, and optionally build, plus the paths
  that database occupies in an installed root, the file suffix a selected package is named with,
  and whether its planner reuses prebuilt repository metadata. The builder is optional, and a system
  whose repository metadata is already what its resolver reads declines the cache instead of leaving
  it on. A package build declared against a manager whose system has no builder is refused by name.
- `PackageRepositoryInfo` represents one repository and binds it to a package system. Its target name is
  the repository ID; remote declarations also expose their pinned directory and base URL. Priority is
  configuration policy, not an intrinsic repository property. A repository is not inherently owned by an
  OS release.
- `LocalPackageRepositoryInfo` identifies a repository assembled from package artifacts in the build graph.
  It carries package directories, not the box that produced them; a consuming package manager generates
  its repository metadata with its own box.
- `RepositoryUniverseInfo` defines one homogeneous solve universe: required repositories, named optional
  groups, and groups enabled by default. Selection preserves declaration order, de-duplicates identical
  targets, and rejects conflicting repository IDs.
- `OsReleaseInfo` associates named native package sets with one repository universe and supplies the base
  of a box. Its target label carries the release identity.
- `PackageManagerInfo` is an immutable solve environment. Each ordered `ConfiguredPackageRepositoryInfo`
  record carries the repository ID, materialized directory, effective priority, base URL, and optional
  declaration dependency. The dependency is analysis-only and is absent for an inline repository made from
  package-build inputs. The manager also carries reusable solver caches, chooses a box, and carries its
  release's package sets. A derived manager inherits this state and can add repositories without repeating
  or rematerializing inherited release policy.
- `LocalPackageUniverseInfo` describes a universe of locally built packages together with the imported
  runtime Requires/Provides metadata needed to select an install request's closure among them.
- `BuildrootInfo` materializes the shared base root from explicit packages or a release package set.
- `BoxInfo` contains a runnable root filesystem and the sandbox used to enter it. Its target label
  establishes provenance; the box may serve compatible package managers for other releases.

Solver caches are anonymous targets keyed by resolver box, package system, configured repository,
architecture, and execution platform. Matching boxes and package managers therefore consume one shared
cache artifact, while distinct solver contexts remain isolated.

This split is visible in the default catalog. Several releases of one family are separate OS releases whose
package managers solve against their own repositories while sharing one box, and a release may model
some of its repositories as required, some as a default group, and some as an optional group a package
manager enables. A family macro declares that whole standard target bundle and its package sets while
accepting overrides for mirrors, repositories, priorities, and package policy. Buildroots resolve the
release's `buildroot` package set rather than duplicating native package names in the buildroot
declaration.

An initial image normally fixes one package manager for the lifetime of the logical image and derives its
box from that manager. Every derived image and terminal output inherits both. Box-only images are also
supported, but cannot install native packages. A package target names a buildroot because its shared base
root, not OS identity alone, is its relevant input.

### Catalog pinning and refresh

Normal builds do not resolve against live network repositories. The catalog contains one required and two
optional generated forms:

- `snapshot/repo/<name>.<architecture>.json` pins one repository's build metadata for one of the
  architectures its mirror serves, and that architecture's complete package inventory,
  keyed by SHA-256 checksum. The metadata is files named by where each lands in the materialized
  repository: some fetched against a stated checksum, some carried verbatim where the pin narrowed what
  the mirror served. What goes in them belongs to the package system; placing them does not. Where the
  mirror itself names metadata by content, the snapshot pins each stream by its own checksum and drops
  the ones the resolver will not read; where it does not, the snapshot pins the bytes the refresh saw,
  which stays buildable only against a mirror that serves immutable snapshots and not against an
  ordinary rolling one;
- `snapshot/box/<name>.json` optionally freezes a box transaction. Remote records contain
  `{source, repo, pkg_checksum, package_id, url, size}`: the checksum verifies the bytes, while `url` and
  `size` record the last known transport after rolling repository metadata stops advertising that package.
  The target's `.repository` or `.box` suffix is not repeated in the snapshot filename;
- `snapshot/key/<FINGERPRINT>.key` holds one signing key a repository declares. The declaration in the
  catalog's BUCK names the key by fingerprint, which is what a reviewer checks against the distribution's
  published one, together with the URL to fetch it from. `refresh-catalog` fetches a missing key file
  once and never updates it: a key is immutable by fingerprint, and whether the file is the declared key
  is checked by the build with the package system's own tools, not by the refresh.

A box with a resolver box and no committed transaction resolves through that predecessor as a normal
cacheable build action. The generated transaction is an input to the existing dynamic package selectors,
so the box builds in one invocation without mutating the source tree. Its package selection changes only
when its authored policy, resolver box, or pinned repository inputs change.

`tine buck run tine//tools:refresh-catalog` refreshes the default `tine//catalog` package in several
phases. Pass another catalog package after `--`, for example
`tine buck run tine//tools:refresh-catalog -- my_project//catalog`:

0. With `--advance`, advance every repository pinned to a mirror that publishes snapshots, by rewriting
   the pin in the declaration. A remote repository rule owns its own pin arguments and carries them as metadata under a
   namespace it owns, so a package system joins this phase by declaring a pin. How the newest snapshot is
   found is the mirror's business: one enumerates its snapshots through a gateway, while another publishes
   a tree per day and exposes no index at all, but does record when it last finished one, which is the
   only thing distinguishing a complete day from a half-written one. Repositories sharing one pin
   advance together, because a release's repositories are only guaranteed to solve together when they come
   from the same snapshot, and a pin never moves backwards. A release macro forwards its own pin arguments
   to the repositories it owns, and overriding a release's mirrors is all or nothing for the same reason.
   Advancing is scoped to the repositories the same run is about to re-snapshot: a base URL from one
   snapshot composing package locations pinned in another builds nothing. `verify-catalog` skips this
   phase and checks the committed pins.
1. Fetch every signing key the selected repositories declare and the catalog does not hold yet, on the
   host. `verify-catalog` reports a missing one.
2. Run every remote repository's `[snapshot.<architecture>]` sub-target on the host, once per
   architecture its mirror serves. The snapshot driver downloads and verifies repository metadata, drops
   unused streams, validates package locations, and writes deterministic, pure snapshot JSON. It does not
   carry packages forward from an earlier snapshot. It only reads metadata, so any host runs it.
3. Run the selected boxes' `[resolve]` sub-targets against the freshly pinned repository trees and
   atomically replace their optional frozen transactions. The target box's release, repository
   selection, package list, and architecture define the solve.

Snapshot and resolve take the result from the driver's stdout: a resolve runs in a sandbox that binds the
project and nothing else, so stdout is the one destination that needs no writable path. The tool then atomically
replaces the committed file, or, for `verify-catalog`, compares in memory and leaves the checkout as it
found it.

The catalog tool asks Buck for the selected package's canonical targets and derives the snapshot directory
from their canonical cell and package. `--box` limits which box transactions are resolved, and scopes
the repositories refreshed to those the selected boxes depend on. The pins stay where they are, so a box
whose package list changed re-resolves in place; `--advance` first moves each selected pin to the newest
snapshot its mirror offers. A refresh writes as it goes, since the advanced pin is what the snapshots are
taken at and a snapshot has to be on disk before the box reading it resolves, so a failed one restores
every file it wrote: the advanced pin beside the old snapshots would build neither catalog.

A committed box lock retains any package transport needed to build that exact transaction. The repository
package pool combines those retained transports with its current snapshot, so every intermediate refresh
state remains buildable and an interrupted refresh can simply be re-run. Where the repository's metadata
rather than the package carries the proof of a package, a lock also records the metadata it was resolved
against, as `metadata` entries, and the repository materializes that generation under `retained/` while
the lock still needs a package the current metadata dropped, so the retained transport fetches nothing a
verifier cannot vouch for. The lock records the pin with it, for a verifier whose clock is its own to judge
the generation as of then, the way the current one is judged as of the repository's; a rolling repository
records nothing, since its metadata URLs do not outlive the mirror's next advance. A verifier reads a
retained generation only for a package the pinned metadata does not vouch for, so a generation that fails
to verify fails those packages and no other. A lockless box always resolves from the current pinned
snapshot and therefore needs no retained transport or metadata for packages absent from it.

Transport retention does not turn a rolling mirror into an archive. A URL may eventually disappear; a
clean-cache rebuild then needs a durable archive/content store, while an already fetched artifact can still
come from Buck's content-addressed cache. The lock preserves the identity, expected size, and last route so
that availability can be supplied independently without changing the solve.

`verify-catalog` performs the same generation and fails when committed JSON differs. Repository snapshots
are ordinary Buck source inputs, so changes invalidate only consumers of the changed data.

The catalog tool asks Buck for the targets carrying each role's label, so a package system joins the
snapshot and resolve phases by labelling its repositories rather than by being named in the tool.
Advancing a pin is not there yet: the tool holds a table mapping each system's repository label to the
pin attribute it rewrites and to the function that asks that mirror for its newest snapshot.

A remote repository declaration derives its optional snapshot by stripping `.repository` from the target
name and looking under `snapshot/repo/`. This lets a new repository target analyze before its first refresh;
consuming its empty package pool fails with an explicit instruction to refresh the catalog. A box that
resolves itself needs a usable committed bootstrap transaction. A new box instead resolves through the box
its release names, or through a `resolver_box` of its own; the predecessor supplies only the execution
environment for the planner, while the new box's release, repositories, packages, and architecture define
the generated transaction. Refreshing the
catalog is optional for that box and freezes the generated result at the conventional lock path.

The refresh convention keeps repository and box declarations plus their generated data in the active
catalog's root Buck package, with generated data grouped under `snapshot/{repo,box,key}/`. This makes
target-name-derived paths and the package-local optional `snapshot/box/*.json` retention inputs agree.

### Authoritative repository package pools

Each remote repository target owns separate dynamic values for its pinned metadata and package pool.
The pool expands the union of the current snapshot inventory and remote transports retained by committed
box locks into one digest-checked package artifact per checksum, and nothing else: a package is
installed, and bootstrapped from, exactly as its repository serves it. A selected package is named with the
one suffix its package system declares, whatever a repository happens to serve it under: an ecosystem that
has changed compressors may still carry a package built before the change, and the byte content, not the
extension, is what a checksum-keyed pool identifies.

The repository target is the canonical action owner, so boxes, buildroots, and images share its downloads.
A package removed from the latest snapshot remains in the pool while a committed box lock references its
pinned URL and size.

`select_package_artifacts()` reads a resolved transaction, looks up each `(repository, checksum)` in the
authoritative pool, and creates a symlinked directory containing the requested representation. It never
creates a second download. Buck materializes only artifacts selected by a consuming transaction, while every
consumer shares their owning actions.

Packages built in this repository use transaction entries with `source = "local"` and a location into an
input package directory. They are projected directly from the producing target rather than copied into a
second pool.

A repository that declares signing keys verifies package signatures after fetching them. The repository
cannot verify them itself, because that takes a box and the box a release names is built from the
release's own repositories; so the package manager or the box rule declares the verification with the box
it has: one keyring per set of declared key files (validated against the committed fingerprints), shared by
the repositories declaring that set, and the selector then verifies what a transaction selects from that
repository in one action, publishing the closure's copies of those packages. One action per closure and
repository, as the overhead of launching the sandbox and `rpmverify` per package is unbearably high. A
derived manager inherits the verifier. The unverified pool is only being used by the repository
configuration code.

Why repository ownership matters:

- one digest and one action graph node define each upstream package;
- box, buildroot, and image closures become cheap selectors;
- repository snapshot skew fails at the lookup boundary instead of silently downloading different bytes.

### Box bootstrap

A box is a reproducible execution environment built from one base OS release. It supplies its package
system's own resolver, installer and indexer, Python, core utilities, sandbox dependencies, and currently
the image-building tools; a VM runner takes its box explicitly, so only a box asked for one carries
that stack. The base release identifies where this userspace came from, not the only release it may
operate on.

A box runs on the build host, so it is an execution dependency: a rule takes it with `attrs.exec_dep`, a
command line with `$(location_exec)`. Buck configures those for the execution
platform (tine has just one) rather than for the consumer's target. A target named on the command line is
configured for the target platform instead, which would be a second build of the same box. To avoid that,
`box.new` declares the box under a hidden name that is compatible with the execution configuration only,
and the public name as an alias reaching it through an execution dependency, so `buck build`, `buck run`
and ordinary dependencies all arrive at the same box.

A lockless box uses its resolver box, by default the one its release names, to produce its build
transaction and perform the authoritative installation. Its target root therefore contains only the
requested packages and their dependencies; it does not need Python, package-manager libraries, or
other construction tools unless they are part of its intended runtime.
A locked box can use its own completed root to run the explicit `[resolve]` update command; during a
bootstrap or tooling transition, a predecessor may run that command instead. This edge is deliberately
one-way: it changes where resolution and installation execute, not the repositories, requested packages,
architecture, or root built for the new box. Box resolution does not consume package-manager priority
policy; its repositories use the native default priority until bootstrap needs an explicit policy of its own.

Only a box declared `root = True`, which has no predecessor, bootstraps its own installation tools in
two stages:

1. The minimal extractor unpacks the same package closure into `stage1` without running scriptlets
   or creating a package database. An extractor reads the packages its repository serves, whatever
   framing they carry, so the pool never has to derive a second form for the bootstrap.
2. The package-system installer runs from `stage1` and properly installs the closure into `stage2`,
   including scriptlets and the package database. `stage2` becomes the reusable `BoxInfo` root.

Before that second stage, `stage1` verifies the same closure against the release's declared signing keys,
the way a predecessor box verifies a successor's packages. That catches an unsigned or tampered package
and a wrong key, but the verifier itself was extracted unchecked, so a root box is not a root of trust
independent of the packages it bootstraps from.

Both catalog boxes are root boxes.

A first lock is the one thing a root box cannot produce for itself, since resolving needs a box to
resolve in. Pointing the new box's `resolver_box` at an existing box that can run the new package
system's planner breaks that cycle for one refresh; making it `root` afterwards leaves the box
self-sufficient. That predecessor has to carry the new system's package manager, which a different
family may well package. This is a one-time exposure per new root box, not a standing dependency.

Box configuration prefers a target-provided systemd factory `nsswitch.conf`, but writes a deterministic
files/DNS fallback for minimal roots. Resolver integration and target configuration therefore do not impose
specific implementation packages on a derived box.

The second-stage install is authoritative for package metadata, ownership behavior available through the
unprivileged sandbox, and scriptlets.

The host contract is intentionally small; its short list of requirements is documented in
[images.md](../user/images.md).

### Execution isolation and target roots

All build actions run through `box.run()` and `box/sandbox.py`. The sandbox binds the box's
userspace read-only over an otherwise isolated namespace, supplies API and temporary filesystems, clears the
host environment, disables network by default, and uses mkosi-sandbox's unprivileged fakeroot behavior
(`--suppress-chown`, `--suppress-sync`, and `--become-root`).

The project is mounted at `/tine/project` and the action runs there, rather than at the path it is checked
out under. A checkout is then free to live anywhere, including under a directory the box populates
itself such as `/var/lib`, and an absolute path that leaks into a build output is the same for every
checkout. A chrooted operation is a second frame and mounts the project again, under the target root's
`/run`, which apivfs covers with a tmpfs so the mount point is not captured into the image.

The sandbox only creates the execution environment. Drivers own their target-root layout through
`rootfs.rootfs()`:

- a fresh install or image layer binds an output directory at `/buildroot`;
- an incremental install or image layer mounts an ordered lower stack plus a persisted upper;
- a package build mounts its buildroot stack with an ephemeral upper and binds action scratch at `/build`;
- pack/disk operations merge a stack with an ephemeral upper so cleanup does not modify stored layers.

This division keeps one namespace boundary while letting each driver express the root it needs. Nesting a
second sandbox inside a box would duplicate isolation, complicate mounts, and make remote execution
harder.

`box.run(relaxed = True)` is reserved for interactive leaves. The box still supplies userspace, but
devices, `/run`, environment, current directory, and network come from the host, and the command remains the
invoking user. The box's `nss-systemd` reads native identities from the host's UserDB services under
`/run`. This avoids importing host NSS modules or shadow databases, which may be incompatible with the
pinned userspace. A box target's own `RunInfo` and `image_vm` are the interactive consumers; build
actions never use relaxed mode.

### Native package installation

Native package installation has three phases shared by buildroots and images:

1. **Plan.** Run `PackageSystemInfo.plan` against each configured repository's materialized directory,
   effective priority, and solver cache. Weak dependencies are disabled. Existing lower layers are mounted
   read-only so installed packages can satisfy an incremental request.
2. **Select.** Use the resulting transaction to select package files from repository pools, verified after
   download if the repository declares signing keys, named with the package system's declared suffix so its
   installer finds them. A local repository is materialized by
   an anonymous indexing target using the consuming package manager's box. The same path handles
   package-build inputs and lets the solve choose between local and upstream packages.
   Extra packages arrive on two mutually exclusive paths: a package build passes its explicit
   `buildroot_deps` outputs, while a package manager with attached `local_packages` computes the request's
   runtime closure at analysis time from imported metadata and offers exactly the locally built packages
   in it. Buildroots reject managers with local packages, because buildroot contents must come from the
   explicit, cycle-checked self-hosting locks.
3. **Install.** Run `PackageSystemInfo.install` over the exact package directory, whose file names carry
   the package system's suffix. `install_packages()` owns a fresh root or incremental buildroot delta. An
   image layer instead invokes the same installer against its already-mounted root so package and
   filesystem operations have one output owner.

What a request looks like, rather than how one package system answers it, belongs to the neutral layer:
`package/transaction.py` owns the transaction schema both planners write and Starlark reads back, and
`package/installer.py` mounts the root an install spec names and captures it afterwards, so a driver is
left with its own transaction and nothing else. `package/repository.bzl` owns the pool merge, whose
rule (the repository's current route wins while it carries the content, a lock's transport is the
fallback once it does not, and a size that disagrees is skew) is stated once for both.

The package manager assigns default priorities when it configures repositories: local repositories use 50
and remote repositories use 99, with target-specific overrides applied by `package.manager()`. A planner
action carries its repositories as `{id, directory, priority, baseurl}` objects in its spec.
`encode_repositories()` projects them from `ConfiguredPackageRepositoryInfo`; the record's `dependency` is
deliberately stripped because Buck dependencies are analysis-only and not JSON-serializable. `plan.py`
reads each object into its `Repository` named tuple. The directory selects the pinned local repository
metadata, while the base URL records the transport for remote packages selected into a transaction.

Package-specific `buildroot_deps` use the same representation. Their ordered package directories are
materialized as an anonymous repository with ID `extra`, local priority, no base URL, and no declaration
dependency. Transaction selection maps its local locations directly back to the producing package outputs.

Fresh buildroot installs are anonymous targets keyed by package manager, sorted install specs, and local
package inputs. Buck therefore shares the base buildroot analysis/action graph across packages that use the
same build profile. Package-specific BuildRequires layers remain inline and are installed over the shared
base.

After installation, the installer parks the package database and scrubs the package-manager and ldconfig
bookkeeping that would otherwise make identical roots differ; what parking takes is each package system's
own business. The action that owns the root then captures names and overlay metadata into Buck-storable
form. A fresh root receives mkosi's `uninitialized` machine-id marker; incremental installs preserve any
existing machine ID.

`LocalPackageInfo` intentionally does not carry the producer's box. `local_repository` checks that its
packages use one native package system and remains a box-independent declaration. The consuming package
manager materializes deterministic repository metadata with its own box; anonymous materializations with
the same box, package system, and ordered package directories share one action.

### The RPM package system

The first implementation of `PackageSystemInfo` covers the RPM family. Everything below is confined to its
drivers under `package_system/rpm/` and to the catalog policy that selects them.

libdnf5 resolves and rpm installs. A repository's build metadata is a filtered `repomd.xml` plus the
primary, filelists, and group streams libdnf5 needs, each named by its own checksum, so a snapshot pins
content rather than bytes and drops the streams nothing will read. Parsing it is expensive enough that the
planner reuses it across solves: the `make-cache` verb prebuilds one cache per configured repository, which
is why this system leaves `solver_cache` at its default. Packages end in `.rpm` and the database lives at
the usr-merged `/usr/lib/sysimage/rpm`. A repository pinned with `rpm.remote_repository()`'s
`rpmrepo_mirror`/`rpmrepo_snapshot` carries that pin as `rpmrepo.*` metadata and advances to the newest
snapshot its gateway enumerates.

`rpmkeys` verifies package signatures. The `keyring` driver imports a repository's declared key files into
rpm 6's filesystem keyring, which names each key by its fingerprint and so checks the declaration without
an OpenPGP parser. The `verify` driver checks each package against it at verify level `signature` (upstream
rpm's default `digest` accepts an unsigned package where Fedora's `all` does not). No key is imported into
an assembled root's rpmdb, and the installer leaves libdnf5's own checks off.

The bootstrap extractor frames the header off a package and decompresses the payload itself, so it reads
exactly what the repository serves. It supports the v4/newc payload form the pinned repositories use and
does not implement RPM v6's index-based payload metadata.

After installation the driver checkpoints and vacuums the SQLite rpmdb, removes its WAL/SHM/lock files, and
scrubs libdnf5 and ldconfig bookkeeping. Documentation and language filtering are rpm's own `nodocs` and
`_install_langs`. A locally built package keeps the `<directory>/<file>` location convention that maps it
back to the input directory it came from.

The default catalog declares Fedora 44 and Rawhide as separate OS releases whose package managers solve
against their own repositories while sharing `fedora.rawhide.box`, both from the one `fedora_release()`
bundle. It offers no CentOS Stream release: nothing publishes immutable CentOS Stream composes, so its
pinned metadata stops resolving the moment the mirror advances, which is not a repository this can pin.

#### Import and build flow

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

#### Limitations

- Generated package metadata is fixed to `x86_64`.
- The imported self-host dependency graph is inferred from stored BuildRequires/Provides/file metadata; it
  does not run RPM's dynamic BuildRequires protocol.
- Build cycles fall back to upstream RPMs for ordinary intra-SCC edges, so the package set is not a fully
  self-hosted fixed point.
- Builds use `--nocheck`; package test policy is not implemented.
- Debuginfo/debugsource outputs are not first-class declared sub-targets.
- The bootstrap extractor supports the pinned v4/newc payload form, not RPM v6 metadata.

Entry points: `package_system/rpm/{rules,catalog,generated}.bzl` and
`package_system/rpm/{snapshot,plan,install,pkgdb,createrepo,build,extract,rpmfile}.py`.

### The alpm package system

The second implementation of `PackageSystemInfo` covers Arch Linux. It declares no rule of its own:
the repository rule, how a pinned repository is materialized, and how one is declared are all neutral,
so what a package system adds is its pin, whatever else its snapshot driver needs to name its metadata,
and the drivers themselves.

libalpm resolves and pacman installs. `alpm.py` binds libalpm through ctypes, the way `kmod.py`
binds libkmod, and `plan.py` drives a transaction against the pinned databases with it; `install.py` runs
`pacman --upgrade` over the exact closure that produced. Both are the same library, so a plan and
its installation cannot disagree about which package provides a capability or which version is
newer, and none of those semantics are reimplemented here.

Binding the library rather than driving its front end is what makes the plan a data structure
instead of text. A resolved package carries its own checksum, size and file name, so a transaction
is written from what resolved it rather than from a second pass over the databases. Removals are
visible, and since a transaction is a set of packages to add and nothing downstream can express
one, an install that would remove something a lower layer carries is refused by name rather than
silently dropped. And a capability more than one package provides arrives as a callback rather
than a prompt with nobody to answer it: the solve records the providers and fails naming them,
because a build cannot be asked and a snapshot advance could otherwise change the choice silently.
Naming the wanted provider among the install specs settles it. The one capability an image here
would otherwise have to choose a provider for is `initramfs`, which `linux` requires: the solve is
told to assume it installed instead, because tine builds the initrd itself and a generator's
install hook would only write one into a tree that discards it.

The soname is the one the box pins, so a pacman major release fails to load with that name in
the message rather than resolving against a different ABI. Applying a transaction stays with the
command: it needs no structured result, only an exit code, and driving a commit through the
library would mean owning its progress, conflict and scriptlet callbacks for nothing.

What the planner owns is the frame: the pinned databases are staged where alpm looks for them, the
layer stack below becomes the root the solve resolves against, and each resolved package is named
by the content checksum its repository published. Nothing is fetched and only the throwaway root
staged below is written to, so a solve stays a pure function of the pins.

A package is a tar under whichever compressor its era used, but the graph names selected packages
`.pkg.tar.zst` and nothing else. Arch has served zstd for years; one package left in `extra` under
the old `.xz` is recognized where a database is read, is never in a closure, and is due to be
rebuilt. Telling a package from its detached signature is a separate question from naming one, and
only the first has to accept an older compressor.

`alpm.py` also reads and writes the formats around that library: what a repository database says
about a package, how to write one, and how to open a package's tar. That is not a duplicate of what
the library does but a consequence of where it runs, since the snapshot and extract drivers are host
tools and the host contract does not include libalpm. Two constraints of the format shape the
drivers. alpm reserves every top-level entry beginning with a dot for its own metadata, so the
bootstrap extractor skips all of them rather than a fixed list. And alpm refuses a `%FILENAME%`
containing a separator, so the `<directory>/<file>` location convention that maps a locally built
package back to its input directory cannot be a path here: `index.py` encodes the directory in the
file name instead, and `plan.py` decodes it.

A repository database carries no checksum of its own and no content-addressed name, so its snapshot
pins the bytes the refresh saw. That is what makes the Arch Linux Archive's dated trees the usable
mirror and an ordinary rolling one unusable: `pacman.remote_repository()`'s
`archive_mirror`/`archive_snapshot` pin, carried as `archlinux.*` metadata, names a day, and since
the archive publishes a tree per day rather than an index to enumerate, the refresh reads the marker
recording when it last finished one.

Installation runs hooks and install scriptlets as they would run on a real system. Documentation and
language filtering map onto `NoExtract`, which is where pacman expresses them. The architecture is
pinned from the spec rather than taken from the build host's uname, and the hook directory is named
explicitly, because alpm rebases its system hook directory onto the target root but not the
administrator's, and the box's must not run against the image. Afterwards the driver replaces the
wall-clock `%INSTALLDATE%` alpm records with the assembly epoch, and drops ldconfig's auxiliary
cache, the sync databases and the transaction log.
It rewrites only the entries that actually change: alpm keeps one file per package, so rewriting an
entry a lower layer already parked would copy that whole database up into this layer's delta.

The database stays at Arch's own `/var/lib/pacman`, since Arch has no usr-merged location for it. The
package-database artifact is read from the assembled layer stack rather than from a terminal output,
so a `/usr`-only disk does not affect it. Each entry's `mtree` is dropped from that capture: it is
what pacman alone reads to verify an installed tree, and it is the largest part of an entry after the
file list.

alpm needs no prebuilt solver cache. A repository database is the metadata pacman reads directly,
not a format to convert in advance, so this package system declares `solver_cache = False` and no
cache target is created for it. There is no package builder either: `arch_release()` defines a
`buildroot` package set, but no `makepkg` rule consumes it, so `PackageSystemInfo.build` is unset.

`arch.rolling.box` is the one that made this second package system prove itself. It is a root box:
bootstrapping it is an alpm extraction followed by a pacman install, and it then resolves its own
lock from its own root. Its first lock could not come from itself, so one refresh pointed
`resolver_box` at the RPM box, which packages pacman; making it `root` afterwards left the box
self-sufficient.

Limitations specific to this system:

- It cannot build packages, so an Arch image can only install upstream ones and no target builds a
  local alpm repository. The local-package naming rule is unit-tested, but nothing exercises that
  path end to end.
- Arch signs each package with an individual packager's key and vouches for those through
  certifications by its main keys, both shipped in `archlinux-keyring`. The declared signing keys are
  the main keys; the keyring driver merges the declared files with the box's copy of that package, drops
  what its revoked list withdraws, and certifies the declared keys locally with marginal ownertrust, so
  that a packager's key is valid once three declared main keys certify it: `pacman-key --populate`'s
  model, with the catalog rather than the package choosing the main keys. A signature is read from the
  pinned database's `%PGPSIG%`, or from the database a frozen box lock retains once the pinned one
  stopped describing a package the lock selected; the keyring judges both as of the repository's
  pin, since it computes validity once when built, so a packager key that expired between the two
  pins is refused until the lock is refreshed. The keyring's gpg clock is stopped at
  the archive day the repository is pinned to, so key expiry is judged as of the snapshot and a build
  of it verifies the same way however much later it runs; an unpinned repository judges as of the build.
- Pinning a database by content means a rolling mirror goes stale the moment it advances; only an
  archive with immutable dated trees is usable as a pinned repository.

Entry points: `package_system/pacman/{rules,catalog}.bzl` and
`package_system/pacman/{alpm,snapshot,plan,install,pkgdb,index,extract,keyring,verify}.py`.

### Rust source builds

A consuming repository can build a Rust project it has checked out instead of packaging it first.
`cargo.package()` takes the project's files as ordinary sources, so a clone needs nothing added to it, and
the single `Cargo.lock` among them identifies the workspace root. An action discovers that lock after the
sources have been built, so the checkout may itself be a fetched directory artifact; a dynamic action
then reads the resolved lock and declares what the build fetches. A project that resolves nothing carries
no lock, because cargo will not create one under `--locked` and there would be nothing in it to pin; its
sole manifest identifies the root, `--locked` is dropped, and the empty vendored source plus the unshared
network are what keep the build from resolving anything. The declaration contract is in
[cargo.md](../user/cargo.md).

A lock entry's `checksum` is the SHA-256 of its crates.io tarball and `static.crates.io` serves that
tarball under a URL derived from name and version, so each registry crate becomes one hash-verified
`download_file`, the same treatment as repository packages, and deriving them needs no network; a lock
older than version 3 records no checksums and is rejected. A git dependency is pinned by the commit in its
lock source and becomes a repository fetch instead, transitive dependencies included, since the lock
always carries the full commit; each fetch
shallow-fetches its commit and fails unless `FETCH_HEAD` is exactly that hash, so the commit itself is
the integrity check. Anything from another registry is rejected with the package named.

A build is then two actions:

1. `cargo_vendor` unpacks the registry crates into `vendor/<name>-<version>/` with the
   `.cargo-checksum.json` cargo expects. Git dependencies never enter this tree.
2. `cargo_build` runs cargo in the consumer's box with the network unshared, against the vendored tree
   and the fetched repositories: each git source is replaced by its repository, served over git's local
   `file://` transport, so cargo takes its own checkout and resolves each crate inside its workspace,
   inheritance and sibling path dependencies included, and it insists on finding the locked commit in the
   replacement. Cargo classifies every git transfer as remote no matter the transport, so a build with
   git dependencies drops `--offline`; the unshared network is what keeps it offline. A cargo
   configuration file inside the checkout would outrank the one the driver writes, redirecting its
   sources, so the build refuses it. The declared binaries come out of `target/release`.

A vendored directory is not cargo's only offline mode (a pre-populated `cargo fetch` cache builds offline
too), but it is the only one buck can assemble from individually hash-verified downloads, and the cache
layout is cargo's private business. `--locked` then makes the build fail rather than resolve differently
from the committed lock the downloads were derived from. `cargo-auditable` embeds the crate graph in each
binary, which syft catalogs as `pkg:cargo` components, so a from-source binary reports its dependencies in
the image SBOM the way an installed package does.

The unit of caching is the project: any change to its sources reruns one action for the whole crate graph.
Splitting that into one action per crate would require the crate dependency graph rather than just the
lock, and is deliberately not attempted.

The rerun is made cheap instead for a `tine mount`ed checkout, for a developer working on that part:
Cargo's build directory then is a declared output that buck is told not to clear before rerunning the
action, so cargo finds the previous one and recompiles only what changed, exactly as it does in a working
copy. Nothing else survives: the source tree is copied afresh from the action's inputs on every run, with
the modification times cargo compares them by. A build that finds no previous directory remains the
reference, which is what CI and any `buck2 clean` produce.

Fetched or committed sources declare no build directory and build in scratch space: there is no edit
cycle to speed up, and the large intermediate build artifacts are not uploaded to a shared cache.

### Go source builds

`go.package()` gives a checked-out Go project the same treatment, sources in and declared binaries out.
The project carries no build file pointing at its own root, so a `go_workspace` action finds the `go.mod`
among the built sources and a dynamic action declares the two steps below from what it reports; as with
`cargo.package()`, that is what lets the sources be a fetched directory artifact rather than a checkout.
Only the two files those steps read are taken back out of the sources, so the fetch still reruns for a
dependency bump alone. But the pinning is delegated rather than translated: A `go.sum` records `h1:`
dirhashes over each module's contents, not the hash of any bytes a proxy serves, so there is nothing a
hash-verified `download_file` could check a download against. Deriving byte hashes would mean a second,
generated lock to keep refreshed. Instead go itself is the verifier, and the build is two actions so the
network stays confined to the first:

1. `go_fetch` is the online action: `go mod download` in the consumer's box with the network shared,
   reading nothing but `go.mod` and `go.sum`, so editing sources never refetches. go checks a download
   against the committed `go.sum` where that pins it, and against the checksum database otherwise.
   Downloading deliberately does not extend `go.sum`, so an unpinned module is fetched here and rejected
   in the step below. The driver does reject a `go.sum` missing the module graph's `go.mod` hashes, which
   is the one incompleteness that downloading repairs silently. The output is go's module cache.
2. `go_build` compiles offline. The `cache/download` half of a module cache is exactly the layout a
   module proxy serves, so the driver points `GOPROXY` at it as a `file://` URL and go re-extracts every
   module from it, verifying against `go.sum` a second time (the fetched artifact is never trusted
   implicitly). `-mod=readonly` makes a lock that no longer agrees with `go.mod` a failure rather than a
   silent re-resolution, and `GOTOOLCHAIN=local` keeps the box's go the only toolchain.

The `packages` mapping selects the main packages to build and names their outputs. A checked-out project
often carries commands an image does not install; building those would cost time and may require
additional build requirements. The driver resolves each selection with `go list` and builds it with
`go build -o`, using the declared output name.

No auditable wrapper exists in this path because go itself embeds the module list in every binary it
links. syft catalogs these as `pkg:golang` components in the image SBOM. The declaration contract is in
[go.md](../user/go.md).

A local `go build` inside the checkout leaves no build tree behind: go's cache lives outside it, so there
is no `target/` equivalent for the glob and the daemon's watcher to exclude. It does drop the binary it
built into the current directory, which the glob then picks up as a source, so a project is better built
with `-o`.

The unit of caching is the project, not the package: one action per package would mean modelling the
package graph and the toolchain here, which is what rules_go exists for, and go's own content-keyed build
cache gets most of that back for none of it. That cache follows the same rule as cargo's build directory
above, and keys on file contents, so a mounted project's rerun recompiles only what actually changed.

The module cache is the one difference: `go_build` consumes it, so it is a declared output whatever the
sources are. For a mount buck keeps it too, so a dependency bump downloads only what is missing; old
module versions accumulate but are inert, since go takes only what `go.sum` names out of the proxy view.
Otherwise buck clears it before the fetch, and what reaches the cache follows `go.mod` and `go.sum` alone.

### Filesystem layer representation

Buck directory artifacts cannot faithfully store overlay whiteout devices, opaque-directory xattrs, or a
backslash in a path component. tine stores filesystem deltas in a regular-file representation:

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

### Selecting a distribution

Which distribution an image is built from is a configuration, and the target carries it.
`tine//distribution` owns one constraint setting; a catalog release declares one value of it beside its
other role targets, as `<family>.<release>.distribution`. That single target answers both questions asked
of a distribution: it is the key a `select()` branches on, and it is the incoming transition that puts the
value in place. Nothing in this cell knows which distributions exist, because a consumer maps values to
package managers itself:

```Starlark
package.manager(
    name = "image.package-manager",
    base = select({
        "//catalog:<family>.<release>.distribution": "//catalog:<family>.<release>.package-manager",
    }),
)
```

Every rule that declares a logical image accepts a `distro`. A target that names none is
buildable under any its package serves, and the rule declares one alias per distribution to say which,
so the names are never a list kept beside the images. `distribution.alias()` does the same for a target
tine does not own. The transition sets the constraint only where nothing has set it, so the
target being built decides and everything under it follows: an image's `parent` chain is not a
distribution of its own, it is whatever the leaf pulling it in is. One declaration therefore serves every
distribution, and only the alias names are per-distribution.

What varies between distributions stays where it belongs. A release names the packages a bootable system
of its own family needs, so an image asks for the `bootable` package set rather than for concrete package
names. Where a genuine difference remains, it is an ordinary select on the same constraint.

Nothing supplies a default. A target declared for no distribution in particular names none of them in
its `select()`, and is `target_compatible_with` a value no platform carries wherever none was chosen,
so a build that has chosen one resolves, `//...` skips the rest, and naming one directly fails as
incompatible rather than quietly picking, reporting the missing choice by name. A default
would decide which distribution actually gets built and leave the others to rot. A package declares
that list once in its `PACKAGE` file and every image rule defaults to it, because the failure mode of
repeating it per target is a target that forgets: being compatible while depending on something
incompatible is an error rather than a skip.

This is deliberately not a buckconfig. A distribution is a property of the image, so it belongs in the
declaration, where it is visible to `buck2 uquery`, can differ between two targets in one build, and does
not reconfigure the world when it changes.

### Image construction

The `image.layer` rule creates either an initial image from a package manager or box, or a derived image from
`parent`. `image.ImageInfo` carries the package manager, its box, an ordered stack of filesystem deltas,
deferred tmpfiles snippets, and the canonical lazy package-database and SBOM artifacts. A derived image
inherits the construction configuration but declares fresh metadata for its completed stack, so one
composition cannot silently switch package sources or tooling environments between stages. A box may
be supplied directly for an initial image that never installs native packages.

Each `image.layer` call applies one ordered operation sequence in one action and persists exactly one overlay
upper. A layer's `packages` and `package_sets` are rule attributes rather than operations: it installs them
as one request before any operation runs, while `run`, `copy`, `mkdir`, `symlink`, and `remove` mutate the
same root afterwards. A package set resolves its symbolic name through the image's package manager during
analysis and joins the concrete names in the same request, deduplicated. Installation is a property of the
layer rather than a position in it because a layer resolves one closure at analysis time over the stack below
it: a second install could only ever be solved blind to what the first one added, so there is nothing an
ordering could mean. Making it an attribute is also what lets operation sequences compose, since two lists
that both need packages no longer collide, and what lets a sequence name a package set and extra packages
together, which a set alone cannot express because its members are known only during analysis. Installing
against the result of an earlier install is a second layer, which is where a closure resolved over that
result becomes available. `copy` introduces a declared Buck artifact at an absolute image path, preserving
its position relative to the other operations. `run` executes the image's own tools in a chroot by default;
`chroot = False` instead executes box tooling with the image available at `/buildroot`. Its `env` argument
overlays variables on the box or image environment for that command. Package installation and copying
always run outside the chroot. The `image.layer()` declaration macro recursively flattens operation lists,
allowing reusable helpers to return ordered groups of operations. Every rule that accepts operations is
wrapped in a declaration macro that flattens them the same way, so a helper returning an ordered group
works identically in `image.layer()` and in a composition; the underlying attribute stays a flat, typed
list so Buck can track every embedded source dependency. An `image.install()` target carries `packages`
beside its operations, so a reusable target declares what it needs and `image.install_from()` folds that
into the installing layer's own request.

`install_langs` narrows the install to the translated files of the named languages, and `install_docs`
drops documentation while keeping licenses. Neither ever enters the tree, so the package database records
them as not installed rather than claiming files that are absent. They belong to one `image.layer` operation
sequence, not the logical image's lifetime: an initrd needs neither, while the root filesystem it boots may
want both. Each package system implements them in its own driver. A sequence that installs nothing ignores
both, so a composition can forward them to a layer whose operations happen to skip installation.

An `image.layer` exposes its newly persisted delta, the full stack and canonical metadata through
`image.ImageInfo`, and the same metadata through lazy `[manifest]`, `[pkgdb]` and `[sbom]` subtargets.
`image.ImageSbomInfo` gives typed consumers only the two SBOM formats. Declaring a logical image declares
its metadata with it, because they are intrinsic metadata facets of every logical image: an
`image.ImageInfo` therefore always carries an SBOM and a UAPI.16 file manifest, and a package database
whenever the image has a package manager at all. The two describe different questions and neither
subsumes the other: an SBOM says what the image was built from, the manifest says what it ships, down to
the digest of every regular file. An artifact referenced by a provider remains lazy. Selecting either
SBOM format runs one shared scan; the default image build runs none of the metadata actions. Filesystem
materialization remains an explicit terminal operation, so logical image construction does not depend on
an archive or disk format.

Package installation and image tooling remain separate concerns:

- the bootstrap `package_manager` determines what native packages can be resolved;
- that manager's `box` supplies every layer driver and terminal image tool;
- `local_repository` declares compatible package outputs and infers their package system from
  `LocalPackageInfo`; a consuming package manager materializes its repository metadata with its own box;
- a derived package manager adds such repositories to a base manager's configured selection;
- a manager's `local_packages` instead selects locally built packages per install by runtime closure.

A project can therefore expose locally built packages without adding them to its OS release, or a package
manager may instead attach a branch's generated local-packages universe; worked examples of both
declarations are in [images.md](../user/images.md).

Each install operation then computes the runtime closure of its requested packages at analysis time over
the imported Requires/Provides metadata, builds exactly the locally built packages in that closure, and
offers them to the solver ahead of the upstream repositories. Requested capabilities without a local
provider continue to resolve upstream, so partially imported branches simply mix. `image.ImageInfo`
accumulates its layers' install specs and seeds every later closure with them, keeping lower-layer packages
locally backed in later solves. Unlike a static `local_repository`, only the packages an install actually
pulls in are built; the universe target itself never forces a package build.

Logical images and terminal outputs are separate rule families. Terminal rules merge the stack only when
needed. The catalog of terminal rules (`image.archive`, `image.directory`, `image.uki`, `image.repart`,
`image.bootable`, `image.sysext`, and `image.vm`) and the `image.rootfs_archive`, `image.sysext_image`, and
`image.bootable_disk` composition rules are documented in [images.md](../user/images.md). All of them,
with the operation helpers and conventional partition layouts, are members of the `image` struct exported
from `tine//image:defs.bzl`; that facade is the public API, and the modules behind it are implementation
structure. Every public facade a consuming project loads from exports one namespace struct the same way:
`box`, `cargo`, `go`, `git`, `package`, `package_system/rpm`, `package_system/deb`,
`package_system/pacman`, `distribution`, and `tests`. Every rule resolves its drivers through one
`ImageToolsInfo` bundle at `tine//image:tools` instead of a private attribute per driver.

The package database and SBOMs are supply-chain outputs read from the assembled image, never shipped in it.
Declaring any logical image declares both lazy facets, so compositions inherit them rather than repeating a
metadata step. Each package system captures its database in its native shape. SBOM scanning of the whole
tree additionally catches
packages no package manager knows about, such as Go modules bundled into ELF binaries.

Terminal rules leave the package database and other package state intact — except `image.sysext`, which
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
uses it, while the kernel-modules cpio that `uki.py` appends stays raw because the distribution already
ships each module compressed; the kernel unpacks the concatenation as independently compressed segments.

`image.repart` deliberately distinguishes `definitions` from `partitions`. Definitions describe new
partitions to populate directly from `image.ImageInfo`: repart mounts the delta stack with a
disposable overlay upper instead of first copying a directory artifact. Partition inputs are
`image.RepartInfo` outputs from an earlier split call; their blocks are copied into the new disk with
their resolved type and UUID preserved.
Calls emit a disk by default. `split = True` additionally exposes each newly defined partition with
normalized metadata alongside its block artifact; `disk = False` makes such a call partition-only. No
partial disk is passed between
actions. The partition artifacts are portable, so `image.RepartInfo` carries no box: repart uses the
destination `image.ImageInfo.box`, while standalone conversion and VM rules select a box explicitly.
`image.directory` is an independent terminal view and is never an input to repart.

Partition layouts are always explicit inputs; neither `image.repart` nor `image.bootable_disk` chooses one
implicitly. The reusable conventional layouts are listed in [images.md](../user/images.md).

Partition labels may contain `{image_id}` and `{version}` placeholders. `image.format_partition_labels()`
renders them — `image.bootable_disk()` calls it with its own image identity, raw `image.repart()` users
call it themselves, and unrendered placeholders fail the build. The usr-verity layouts carry such labels
to produce the `<id>_<version>[_verity[_sig]]` names that systemd-sysupdate A/B slot matching expects.
Rendered labels pass through `image.partition()` again, so GPT's 36-character label limit is enforced on the
final value.

Verity data, hash, and optional signature partitions are produced together in the split action. The root or
usr hash is an artifact because its value is known only after execution. `image.RepartInfo.root_hash` carries
an optional `image.RootHashInfo`, allowing `image.uki` to consume the hash without another top-level
provider. One split call may produce at most one such hash. This artifact boundary also ensures the final
disk contains exactly the partition bytes whose hash was embedded in the UKI. The hash is also available
as the split target's `roothash` subtarget. Signature partitions require an explicitly declared key and
certificate.

`image.bootable_disk()` composes:

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
                                              └─> ESP + system partitions ─> bootable-image result
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

`image.initrd()` declares the conventional initrd as a target of its own: a package image with `/init`
pointing to systemd and `/etc/initrd-release` pointing to `/etc/os-release`, installing the release's
`initrd` package set, so family catalog policy supplies concrete native package names. Its `packages`,
`package_sets`, and `ops` are added to those defaults rather than replacing them, so extending an initrd
never means restating what it already does, and `install_docs` and `install_langs` invert the defaults an OS
image wants, since documentation and translations only cost an initrd boot memory. It generates before it
prunes, so the hardware database it ships is compiled from the sources it then drops. The rule archives the
image into the cpio itself, at the `compression` it declares, and returns both as `image.InitrdInfo`
alongside the image's own providers. That cpio omits the package database from the paths the image's
package system declares: nothing in an initrd resolves a dependency or verifies a package, and the kernel
unpacks the whole cpio into tmpfs, so shipping it would only cost boot memory. The `[manifest]`, `[pkgdb]`
and `[sbom]` views
read the tree rather than the archive, so they still describe the complete installed set.
`image.bootable_disk` consumes `image.InitrdInfo` rather than a bare `image.ImageInfo`, so the archive is
the initrd target's own output and its compression is declared where the initrd is. A composition given
no `initrd` declares `<name>.initrd` for itself through the same macro, inheriting its package manager,
version, and distribution; the default is therefore an ordinary target that can be built and inspected on
its own rather than an anonymous step inside the disk image. The composition publishes the provider it was
given from `[initrd]` and returns the same
instance directly, so both interfaces describe exactly the same initrd.

`uki.py` appends the kernel-modules cpio and runs `ukify`. That cpio carries the modules `initrd_modules`
selects, closed over their dependencies and their firmware with libkmod, which reads the image's own depmod
index and no configuration from the box; `/usr` keeps the full set for the booted system. The pattern
syntax is mkosi's `KernelModules=`, minus the convenience of retrying a leading-slash pattern below
`kernel/`, so a leading slash anchors instead; `re:` regexes and the `host` value have no equivalent, the
latter because reading the build host's loaded modules is not hermetic. `image.DEFAULT_INITRD_MODULES` is
mkosi-initrd's list, so an image moving onto tine from `KernelInitrdModules=default` keeps the module set it
had. One list serves kernels that ship different sets of modules, so a pattern matching nothing is reported
rather than fatal, in the manifest the rule publishes beside the UKI. The UKI is named
`<image_id>_<version>_<arch>.efi` from the image identity. For now an image holds exactly one kernel — the
name (and sysupdate's matching of it) could not distinguish more. If several kernels per image ever become
a requirement, add naming configuration to `image.uki()` to disambiguate them. The ESP layer copies the UKI
directory into `EFI/Linux` and includes the operations returned by `image.install_systemd_boot()`. Those
create the ESP path, run the box's `bootctl` with its paths in the command environment, and remove the
random seed. The final repart action creates the ESP while copying the previously split system partitions
into the same disk. It splits nothing itself: the system partitions arrive already split, and nothing updates
the ESP as a partition. The default system partition is a compressed EROFS `/usr` protected by
dm-verity; the generated `usrhash=` is embedded in every UKI. The same copy operation can place device
trees, bootloader entries, and future standalone artifacts; `esp_files` exposes it, copying caller-declared
artifacts to chosen ESP paths.

Kernel command lines remain lists of arguments through the Starlark API and driver invocation. The UKI
driver appends any generated verity hash and joins the arguments only when writing ukify's command-line file.
Boot profiles become small PE binaries of `.profile` and `.cmdline` sections, built against the image's
addon stub and joined into every UKI; a profile's arguments extend the shared base command line (including
the verity hash), and kernel arguments are last-wins, so profiles can also override it.

A composed raw disk can be re-encoded into distributable formats without rebuilding it: `image.disk_convert`
uses its explicit box to drive `qemu-img` for a compact qcow2 and `zstd` for a compressed raw. These are
alternative encodings of the same disk and remain separate, reusable terminal implementations rather than
default outputs. A converted target provides `image.DiskConversionInfo`, naming the format alongside its
artifact, so one provider covers every encoding instead of one provider type per format.

#### Bootable-image result

Bootability and output format remain independent terminal capabilities, but the artifacts declared by one
`image.bootable_disk()` invocation form one concrete product. The composition publishes one target at
the requested name. Its default output and `image.RepartInfo.disk` are the raw disk; its other terminal
views,
supply-chain metadata, and constituents are lazy subtargets:

```text
//examples/image:boot-demo
├── [uki]
├── [directory]
├── [qcow2]
├── [raw.zst]
├── [manifest]
├── [pkgdb]
├── [sbom]
│   ├── [spdx]
│   └── [cyclonedx]
├── [initrd]                    # default output: zstd cpio
│   ├── [manifest]
│   ├── [pkgdb]
│   └── [sbom]
│       ├── [spdx]
│       └── [cyclonedx]
├── [roothash]
└── [partitions]
    ├── [usr]
    └── [usr-verity]
```

Typed providers are the composition API; subtargets are the command-line interface. There is no aggregate
bootable-image provider. The rule returns its completed `image.ImageInfo`, `image.RepartInfo`,
`image.InitrdInfo`, `image.ImageDirectoryInfo`, and `image.UkiInfo` independently. `image.RepartInfo`
holds the composed raw disk, its independent partitions, and optional `image.RootHashInfo`, rather than
exposing separate top-level providers for these facets.
Its qcow2 and compressed-raw encodings are reachable only through their subtargets, each publishing one
`image.DiskConversionInfo`: a single provider type cannot appear twice in one result, and "the conversion"
of a target that has several is ambiguous anyway. Every other constituent subtarget publishes the same
provider instance as the main target. This lets a consumer request exactly the capability it needs without
fields duplicating another provider's data.
Storing an artifact in a provider does not build it. Optional actions run only when a consumer uses the
corresponding artifact or a user selects its subtarget. They must not appear in the result's
`DefaultInfo.default_outputs` or `DefaultInfo.other_outputs`. SPDX and CycloneDX still come from one scan,
so requesting either format runs the same SBOM action. A metadata view publishes itself the same way a
conversion does: `[sbom]` and `[pkgdb]` carry an `image.PublishedInfo` naming them after the image they
describe, so a release gathers what a scanner reads while an image's own published set stays what an
update transfers.

`image.bootable_disk` is one rule that owns the complete composition action graph. Provider-oriented action
helpers are shared with the standalone `image.layer`, `image.repart`, `image.uki`, and terminal-format
rules, so the composition reuses their implementations without declaring private sibling targets or
forwarding through a result rule. This aggregation remains scoped to the concrete bootable-image product;
it does not restore a generic `image_result` around every logical `image.ImageInfo`.

`image.rootfs_archive` and `image.sysext_image` follow the same ownership model on a smaller graph: one
rule declares the logical image with its lazy package-database/SBOM views, and its terminal archive or DDI.
Each target returns `image.ImageInfo` alongside its terminal provider, so consumers never depend on
generated `.layer`, `.pkgdb`, or `.sbom` labels. The standalone terminal rules share the same
provider-oriented declaration functions.

The disk, directory, package database, and SBOM derive from the same ESP layer. The initrd is a second
logical image with its own package closure, declared and archived by its own target, so the composition
republishes the package database and SBOM that image carries rather than deriving anything. The `[initrd]`
subtarget exposes only the encompassing `image.InitrdInfo` as its typed contract, plus the image's metadata
as nested subtargets. The main target returns that same `image.InitrdInfo` directly. Supplying a custom
initrd therefore requires only one regular target and never a family of conventionally named siblings. VM
runners remain separate targets because execution policy and credentials are behavior, not facets of the
image artifact.

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
explicitly. VM runners are declared separately from disk composition. Their execution box and runtime
policy are passed to `image_vm` rather than baked into the disk provider or image.

The image build tools live in the box and are not installed into the image merely to build it. Chrooted
`run` operations intentionally use the image's own binaries; non-chrooted runs explicitly use box tools
against `/buildroot`.

### Generating derived state

A package installs configuration that describes state rather than carrying it: module directories with no
index, `hwdb.d` with no compiled database, a locale list with no archive. On a running system scriptlets and
boot-time units produce it; an image being assembled is neither, so tine builds it explicitly. Each generator
is an operation of its own rather than a step of one pass, so it is positioned where the image wants it,
configured on its own terms, and adopted one at a time; what it writes persists into the delta that captures
it, so it is built once and cached with the layer rather than repeated by every terminal output. An `image`
gets the content its operations ask for and nothing else, which is the same reason terminal rules leave
package state alone.

Each generator is a no-op for an image that carries none of what it acts on, so none of them needs
per-distribution knowledge: `image.depmod()` is silent without kernels, `image.hwdb()` without `hwdb.d`,
`image.locale_gen()`
without an `/etc/locale.gen` asking for something. That is what lets the compositions, which declare a whole
product rather than one layer, end every image they build with all three at their defaults without knowing
what went into it. A generator named in `ops` replaces the composition's copy instead of adding a second, so
placing or configuring one stays possible. `sysext_image` is the exception that generates nothing: an
extension merges onto a system it does not own, where a database built from the extension's own tree would
shadow that system's while describing only what the extension carries, exactly as its package database
would.

Their tools are the box's, applied to the mounted image through the `--root` interface systemd gives them,
which is the same relationship every other layer driver has to the image and is what lets an image that
installs no systemd still be finalized. Two cannot work that way and use the image's own binaries in a
chroot: `locale-gen` is a distribution's own script over its own sources, and `depmod` resolves its
search-order configuration from absolute paths that `--basedir` does not relocate, so a box-side run
would silently apply the wrong module ordering. Its index format is its own kmod's to define as well, and a
box older than the image would leave out index files the image's modprobe expects. Both report the
missing binary by name rather than skipping.

### Reproducibility and caching

Reproducibility is both a release property and a caching requirement. Current mechanisms include:

- repository metadata, package bytes, and source archives pinned by SHA-256;
- generated or committed box transactions containing repository/package identities;
- a fixed assembly `SOURCE_DATE_EPOCH` for roots that should be shared across consumers;
- per-package source date epochs for build output timestamps and headers;
- a fixed build host and frozen release-numbering macros;
- parked package databases and scrubbed package-manager caches;
- sorted transaction JSON, archive entries, source staging, and output collection;
- target/configuration-derived partition UUID seeding and normalized archive metadata;
- content-based paths for repository-owned package artifacts.

These measures make action-cache reuse meaningful and prepare the graph for remote execution. The repository
does not yet run a systematic build-twice reproducibility audit, and raw filesystem image byte-for-byte
reproducibility still needs dedicated validation.

Sharing those results between machines is a further, per-action decision. Uploading a result trades build
time against download size: assembling an image writes gigabytes in seconds, so fetching one from the
cache takes longer than building it locally and wastes a lot of bandwidth. Worthwhile actions are slow
and have small output: compilers (cargo, go, RPM builds) and package dependency solvers. These enable
`allow_cache_upload`, except where an action keeps its previous outputs: an incremental rerun drops
buck's strong "this exact input produces this exact output" guarantee, so it must not go into the shared
cache.

**An action only hits if every action above it is cached or byte-reproducible.** Adding an expensive
action to the list buys nothing while something upstream of it produces different bytes each run.

`buck log show` reports one `ActionExecution` record per action, carrying the `wall_time_us` and
`output_size` that decide whether it is worth caching, and the output digests that say whether it is
reproducible across two builds.

## Decision record

The following decisions remain the rationale for the current design. Detailed source-code research that led
to them belongs in commit history or focused notes; this section records the durable conclusion.

### Use Buck2 as the graph and cache

Buck2 was chosen because package builds benefit from content-addressed artifacts, lazy action execution,
sub-target providers for a build's binary outputs, and a test protocol that can later host the Barrage
executor. Its lack of an implicit local sandbox also lets tine use the same mkosi-sandbox boundary locally
and on future remote workers. Bazel's broader language-rule ecosystem mattered less than these properties
for a package-heavy repository.

Buck cannot add ordinary target dependencies discovered from an action output. Dynamic actions may select
among declared inputs but cannot turn newly discovered BuildRequires into a new static graph. Therefore
dependency discovery/import must produce committed or analysis-time lock data before normal builds.

### Commit repository snapshots and selectively freeze boxes

Repository snapshots are committed so normal resolution remains independent of live repository state and
reviewable. Boxes with a predecessor resolve a cacheable transaction from those pins during their build.
Committing a box transaction is an explicit freeze operation for bootstrap roots, releases, or other
boxes that must remain stable across repository snapshot updates. Unlike an ordinary language lockfile, a
frozen box transaction also retains the transport for packages needed after a rolling repository
advances.

### Let repositories own upstream packages

Putting downloads in each closure duplicated ownership and left derived operations without a stable home.
The authoritative named pool instead gives every upstream package one action owner per repository. The
current snapshot defines available packages, while optional committed box locks retain older packages
required by frozen transactions. Closures select artifacts; they do not fetch or transform them.

### Separate package system, OS release, package manager, and buildroot

The former distribution object bundled repository membership, box tooling, and buildroot policy. That
made optional repositories awkward, implied that a box had to match every target release it operated
on, and provided no clean place for request-specific local repositories.

The current vocabulary follows the actual responsibilities:

- the package system defines operations;
- the repository universe defines membership and normal enablement policy;
- the OS release defines identity, selects a repository universe, and may provide a box's base;
- the package manager defines one exact solve universe and box; derived managers compose additional
  repositories and priority overrides without changing their inherited release or box;
- the buildroot materializes the shared base packages.

This is also why a release is not called a distribution target: two releases of one family are separate
release identities, while repositories and boxes can be reused across those identities when compatible.

### Keep native package managers homogeneous

Every repository universe and package manager belongs to one native package system. Two native systems must
not participate in one dependency solve, and neither must a third. Adding the second one bore this
out: the neutral rules needed no notion of which system they were driving, and the two never meet in
a solve because a universe, a release, and a manager each belong to exactly one. Supplemental content
systems such as Flatpak may eventually coexist with a native one in an image, but compatibility rules
are deliberately deferred until that is a real requirement. `PackageSystemInfo` is for native binary
package ecosystems, not every possible image content type.

### Separate a box's base release from its target releases

A box is a tools root with a concrete OS userspace, so its base release records where its packages and
identity came from. That does not make it part of a package manager's target OS identity: one box can
still operate on every compatible release. An image normally obtains this explicit box dependency
through its package manager, making reuse visible and content-keyed while avoiding duplicated compatible
tooling roots. Box-only images remain available when no native package resolution is needed.

### Use one sandbox boundary and let drivers mount target roots

The box userspace must be pinned, the host environment must not leak into builds, and package scriptlets
need unprivileged fakeroot semantics. Vendored mkosi-sandbox supplies those properties without host package
tooling, a build-chroot manager, bwrap, or a second nested sandbox. Drivers mount their own target roots
because install, build, image, pack, and disk actions need different layouts.

### Pass drivers one JSON spec

A rule describes an action to its driver as a single JSON spec written with `write_json`, invoked as
`<driver> --spec <spec.json>`, rather than as a command line. Buck resolves artifact paths inside the spec,
so declared outputs are named there exactly like inputs and neither has to be flattened into repeated
options or positional triples. Structured configuration (layer operations, partition definitions, boot
profiles, repository selections, subpackage outputs) stays structured, and a driver reads the schema its
rule owns instead of revalidating an argument grammar. A driver that invokes another driver does the same:
the image layer driver writes an install spec for the package installer.

Two exceptions are deliberate. A planner keeps its verb on the command line, since each verb has its own
spec schema. Both it and the snapshot driver keep `--out` there too: their `[resolve]` and `[snapshot]` run
targets let a caller name the file to write, or `-` for stdout when it has nowhere to write one, and one
calling convention per driver beats splitting the destination by verb.

### Store image layers as deltas

Copying a complete root for every image step scales with total image size rather than change size. Ordered
overlay deltas let child layers and terminal outputs reuse their ancestors. Encoding whiteouts and opaque
directories as regular files keeps those deltas compatible with Buck's artifact/CAS model.

### Prefer exact transactions over package-manager network access

Resolution uses pinned local repository metadata; installation consumes an exact directory of already
selected packages.
This keeps network out of build actions, makes the transaction an inspectable early-cutoff boundary, and
separates “which packages?” from “apply these packages and scriptlets.” Weak dependencies are disabled to
match buildroot policy and avoid unreviewed closure growth.

### Build each source package once and expose subpackages

One build invocation naturally emits every binary subpackage and the source package. Running it once avoids
repeated work and inconsistent sibling outputs. Buck sub-targets give downstream packages addressable binary
outputs without pretending each subpackage is a separate build action.

### Keep static local repositories and closure-selected local packages separate

`local_repository` and a manager's `local_packages` both prefer locally built packages over upstream and
share the per-install `extra` materialization path, but they are deliberately not unified. A
`local_repository` publishes an explicit, hand-listed set and forces those packages to build; it suits a
project exposing a few of its own packages. `LocalPackageUniverseInfo` instead describes a whole imported
branch and builds only the packages an install's runtime closure actually pulls in, so attaching the
universe never forces the branch to build. Collapsing them would either force building an entire branch or
push closure computation into every static repository, so both remain until the planned per-package
source/prebuilt provider model (see Roadmap) subsumes them.

### Use bind mounts for out-of-tree content

The previous implementation declared the checkout as an external git cell, which Buck fetched like any
other pinned repository. As a result, Buck could only see committed changes. Bind mounting the checkout
at the cell root exposes the working copy, preserves file watching, and keeps Buck's declared paths
unchanged. This only works on Linux with unprivileged user namespaces and only when the build runs through
`bin/tine`; a direct `buck2 build` uses the checked-in path. Those constraints are acceptable for a local
development feature.

Using the namespace digest as the Buck2 isolation directory would avoid daemon replacement entirely, but
each mount set would also get a separate `buck-out`. Keeping one isolation directory preserves build
outputs when switching between checkouts, while `daemon_buster` lets Buck compare the digest and replace
the daemon under its native lifecycle lock.

## Operating the current system

Host requirements, the wrapper commands, and representative smoke builds are documented in
[images.md](../user/images.md).

## Current limitations

These are properties of the implementation today, not merely ideas for future optimization. Limitations that
belong to one package system are listed in its own section instead:

- Buck preserves `buck-out` across daemon replacement. This is safe for hermetic actions because source
  changes produce new input digests. An action that reads an undeclared input can still reuse stale output
  after a mount change.
- Two native package systems are implemented, and only one of them can build packages.
- A transaction describes packages to add. An install that would have to remove or replace something a
  lower layer carries is refused rather than expressed.
- Image installs select source-built packages through the imported-metadata runtime closure of
  `local_packages`. The walk follows local-to-local edges only, so a local package reachable only through
  an upstream intermediate silently resolves upstream, and there is still no per-package source/prebuilt
  choice under one shared version pin.
- Rust source builds cover crates.io and git sources; another registry is rejected. A git dependency's
  integrity rests on the commit hash the lock records: SHA-1 for ordinary repositories, which is weaker
  than the SHA-256 pinning everything else here uses.
- `cargo-auditable` is a pinned upstream release binary rather than a source-built one, so the Rust build
  path is not itself part of the source-trust chain.
- Three build steps reach the network instead of downloading content with a recorded byte hash: Rust Git
  dependencies use commit hashes from the lock file, project fetches use the commit passed to
  `git.fetch()`, and Go verifies module downloads against the committed `go.sum`.
- Crate downloads carry no recorded size, so Buck learns it from an HTTP HEAD whenever a download action
  executes. A cold daemon therefore needs the network even when every crate is already cached.
- Repository metadata is trusted on first use: neither Fedora's GA trees nor the pinned mirrors serve a
  signed `repomd.xml`, so a substituted one at refresh time could select other validly signed packages.
  Reviewing the snapshot diff is the check for that.
- A root box verifies its packages with the `rpmkeys` of its own unverified stage1, so a tampered rpm
  package in the seed could defeat the check for that box; every other box and every image verifies with a
  predecessor. Arch packages are not verified at all.
- Archive ownership is intentionally normalized to uid/gid zero. Capabilities, xattrs, and SELinux labels do
  not survive as Buck directory metadata; deferred tmpfiles can restore xattrs at terminal assembly, and tar
  preserves them in PAX headers, but newc cpio cannot represent general xattrs.
- Directory image output cannot represent backslashes in names; archive outputs should be used instead.
- The default `/usr`-only disk has a volatile root, so the `/etc` a build writes is not what such an image
  boots with; first-boot defaults come from credentials instead. Package and authored state outside `/usr`
  is not yet translated into factory defaults or another persistent partition.
- The generators cover the databases under `/usr`. System users, volatile files and directories, and unit
  presets are left to the boot-time units systemd ships for them, so an image whose `/etc` is created at
  first boot gets them then and one that ships a populated `/etc` does not get them at all.
- Bootable images currently disable SELinux.
- Remote execution, Barrage integration, release publishing, and systematic reproducibility audits are not
  wired into CI.

## Roadmap

The roadmap is organized by architectural capability rather than old numbered phases. Ordering within a
section is approximate and should follow the next concrete product need.

### Package graph and self-hosting

1. Replace the current metadata intersection with a generated package lock that records precise direct
   BuildRequires and binary/runtime relationships. Use the package system's dynamic BuildRequires protocol
   for packages that generate requirements while preparing their sources.
2. Introduce the source/prebuilt package-provider model only when an image or buildroot needs to choose
   backing per package. Preserve one coherent version pin so upstream and source-built variants have the
   same dependency graph.
3. Model runtime closures at binary-subpackage granularity and make debuginfo/debugsource outputs explicit
   where consumers or publishing require them.
4. Extend importer/build configuration beyond the fixed `x86_64` slice.
5. Extend bootstrap extraction to a newer package format when a pinned repository requires it.
6. Decide and implement build-time test policy. Because successful build scratch is discarded, checks most
   likely belong in the primary build action with per-package opt-outs for broken or prohibitively
   expensive suites.

The durable self-hosting rule remains: invoked build tools may come from the pinned seed, while libraries
linked into shipped outputs should come from source-built packages once their graph is available. Cycles
must be explicit; silently pretending a cyclic source graph is acyclic is not acceptable.

### Supply-chain authenticity and release output

Upstream authenticity for rpm follows the catalog pinning and package pool sections: a repository declares
its signing keys by fingerprint, refresh fetches the files once, and a consumer with a box verifies each
package against them as it is selected, a box using its predecessor. What remains:

1. A root box has no predecessor and verifies with its own stage1. Closing that needs a verifier that did
   not come out of the served packages: a host-side check of the header signature in `rpmfile.py` would
   do, since Fedora signs with one RSA/SHA-256 key per release.
2. Arch verifies through the box's `archlinux-keyring`, so a packager key the catalog's box predates is
   refused until the box lock is refreshed; a keyring taken from the pinned repository itself would need
   verifying first, by the previous one, which is the same bootstrap pacman has.
3. Repository metadata stays unsigned upstream; the snapshot diff is its review.

A later release pipeline needs repository composition, package-group metadata, source/debuginfo publication
policy, provenance/attestations, and signing. Secure Boot signing should use deterministic RSA PKCS#1 v1.5
without timestamps. Development keys are declared, cacheable inputs; production builds sign with a key held
outside the build, addressed by URI over a PKCS#11 socket, see
[signing-pkcs11.md](../user/signing-pkcs11.md).

### Image hardening and formats

Near-term image gaps are:

- offline SELinux labeling instead of `selinux=0`, as one more generator;
- build-time `systemd-sysusers`, `systemd-tmpfiles` and `systemctl preset-all`, once their single-UID/GID
  and volatile-`/etc` behavior is settled;
- deterministic ext4/FAT byte-level validation and any required normalization;
- OCI, confext, ESP, and other terminal formats as real consumers require them;
- richer ordered operations for setting file metadata directly;
- deciding whether package installation and image tooling eventually need distinct compatible boxes.

The layer model should remain ordered operations captured as deltas. A provides/requires feature solver is
unnecessary unless real composition requirements appear.

### Scale, configuration, and testing

- Configure remote cache/execution only after the local action graph and host contract are stable. Box
  roots and ordinary filesystem artifacts are intended to be CAS inputs; relaxed VM actions remain local.
- Add build-twice reproducibility audits because early cutoff is useful only when rebuilt outputs are
  byte-identical. Track known exceptions explicitly rather than weakening all comparisons.
- Integrate Barrage through Buck2's external test executor so many image/integration tests can share one
  streamed process while still reporting per-test results.
- Replace the pinned `cargo-auditable` binary with a source-built one once that no longer depends on
  itself existing, and map commits to tarballs for whatever forge a dependency turns up on next. Add
  C/C++ equivalents only when in-repository builds need them.
- Define the upstream-update workflow: import upstream changes, rebase local patches, refresh snapshots and
  generated metadata, and verify that version skew has not invalidated source/upstream interchangeability.

## Reference points

Useful implementation entry points:

- `package/{system,repository,release,manager,solver,buildroot,install}.bzl` and
  `package/{href,installer,transaction}.py`
- each package system's `rules.bzl` and drivers under `package_system/`, listed in its own section above
- `box/{build,runtime}.bzl`, `box/sandbox.py`, and `rootfs/rootfs.py`
- `image/{image,compose,defs,sign,vm}.bzl` and `image_format/{archive,boot,disk,sysext,uki}.bzl`
- `cargo/{rules,lock,vendor}.bzl` and `cargo/{vendor,build}.py`
- `go/rules.bzl` and `go/{fetch,build}.py`
- `tools/catalog.py` and `catalog/BUCK`
- the generated `packages/*/*/BUCK` and the importer-facing Starlark that validates it

External projects that informed the design:

- Buck2 for action/dynamic-dependency semantics, sub-targets, content-based paths, and test execution;
- the native package managers tine drives, for build, resolution, transaction, and signature behavior;
- mkosi/mkosi-sandbox for user-namespace isolation, root mounting, UKIs, and repart-based images;
- Barrage for the planned streamed integration-test executor;
- Siguldry for a possible production PKCS#11 signing boundary.
