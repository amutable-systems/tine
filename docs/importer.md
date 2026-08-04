# Maintaining packages with the importer

The importer maintains a (partial) downstream derivative distribution in a monorepo: it mirrors upstream
distribution dist-gits (currently Fedora or CentOS) and maintains local package deltas. Package sources live
in the OS.git monorepo that vendors this `tine` cell, under `packages/`_distro_`/`_branch_`/`_srcpkg_ (for
example `packages/fedora/f44/glibc`), with pristine imports on the separate `upstream-rpm` branch. The branch
and metadata layout, the design principles, and the rebuild strategy behind all of this are documented in
[packages.md](packages.md).

Run the tool as `tools/buck run tine//tools:importer -- <verb> …`: that executes it in the
`tine//box:dev` environment (host identity, network, and cwd; the pinned rpm/git toolchain from the box
engine), so the host needs no rpm tooling installed. Verbs that only use git also work by executing the
script directly.

## Operations

Operations on the `upstream-rpm` branch to import/fetch upstream dist-git changes into `OS.git`:

 - `import-upstream pkgname distro branch`: new package from `fedora` or `centos`
 - `update-upstreams`: check for any new upstream dist-git commits for all currently imported packages and
   pull them in

Operations on the `main` branch to maintain the downstream packages:

 - `import pkgname [distro branch]`: copies `packages/…`_pkgname_`{/,.json}` from `upstream-rpm` branch
   into main, as a single commit. If there are multiple imports, you have to specify distro and branch to
   disambiguate.
 - `update pkgname`: applies all new upstream commits on top of current state, keeping our local
   modifications.
     * for unmodified local package this always works, file content remains identical between up- and
       downstream
     * for modified local package this may result in conflicts (see `sync` below)
 - `update-all`: Run `update pkgname` for all currently imported packages; i.e. keeps individual
   per-package commits, but will just result in one branch/PR with the whole update batch. That (1) groups
   together updates that were published to Fedora in a single batch, (2) avoids unnecessarily many CI
   runs, and (3) retains bisectability of package updates.
 - `rpm-metadata [--distro … --branch …] pkgname rpm [rpm...]`: Recompute `pkgname.json` from locally
   built rpms. Meant to be run by the build system for a package import/update branch/PR.
 - `sync pkgname`: `update pkgname` for a local delta we no longer want to carry: upstream adopted
   it, or we dropped the requirement. Same replay, except that the package directory is taken from
   upstream wholesale instead of merged into ours, so our changes are gone and nothing can conflict. The
   discard rides along in the upstream commit; if we are already on the latest upstream commit, it
   becomes a commit of its own.
 - `diff pkgname`: show diff between the `upstream-rpm` and `main` versions of `packages/…/pkgname/`
   (ignores metadata differences)
 - `srpm pkgname`: assemble the `.src.rpm`: freeze the `%autorelease`/`%autochangelog` macros, fetch the
   sources from the lookaside cache, and run `rpmbuild -bs`. Can then be locally built with `mock`, and
   later consumed by the production build system.
 - `mockbuild pkgname`: Build `pkgname` using `mock`, in the chroot config that matches its import source
   (e.g. `fedora-rawhide-x86_64`). Developer tool for validating changes sent to Fedora (production builds
   happen with buck).
 - `rebuild pkgname reasonpkg-version-release`: Generate an automated "pkgname: Rebuild against
   reasonpkg-version-release" commit
 - `list`: table with all rpms, local and upstream version/release (a package with no upstream is shown as
   `native`), and modification status
 - `check [start-ref]`: Validate consistency of all commits (optionally, starting from given ref); will
   run in all PRs

## Release and changelog conventions

 - Local modifications don't modify `%changelog`: We document changes in git, this avoids unnecessary
   merge conflicts
 - Local commits (i.e. not from imports) increase `Release:` by 0.1 to avoid colliding with
   Fedora/CentOS/other upstream's namespaces. Note: Hummingbird already does that, so if we modify a
   Hummingbird import, it will have to be bumped by 0.0.1. Consistency checks enforce this.
     * E.g. `glibc-2.43-6.fc44.x86_64` in Fedora → unmodified import builds as
       `glibc-2.43-6.fc44aos.x86_64`, next modifications are `*-2.43-6.1.fc44aos.*`, `-6.2.fc44aos.*`,
       etc.
     * Hummingbird import of `openssl-3.5.6-0.3.hum1.x86_64` gets imported as `*-3.5.6-0.3.hum1aos.*`,
       and next modification is `*-0.3.1.hum1aos.*`
     * The common rule is: "append `.1` for a modification of a previously unmodified package" and
       "increase last component for a modified one"
 - Packages using `%autorelease` need no manual bump; the release value is derived from the imported
   metadata and the local commit count (see the `srpm` operation details in [packages.md](packages.md)).
 - Builds use a downstream-specific `%dist` tag: `aos` (this documentation's placeholder, see
   [packages.md](packages.md)) appended to the upstream dist tag, e.g. `.fc44aos`; the rationale is
   recorded in the design principles in [packages.md](packages.md).

## Making local package modifications

The workflow for changing a package downstream, like adding a patch or tweaking the spec:

 1. Edit the package in `packages/…/pkgname/`: modify the spec, add patch files, etc. There is no tool
    verb for this, it is a plain git change. Don't touch `%changelog` (git documents the change), and bump
    `Release:` per the rules above unless the package uses `%autorelease`.
 2. If the change introduces new `BuildRequires:`, add them to `srcpkg.json`'s `build_requires` map
    manually. This is unavoidable: the buck build system installs the buildroot from the metadata, it does
    not dynamically resolve `BuildRequires:` from the spec.
 3. Build the package locally with the buck build system, or possibly `mockbuild pkgname` (see below).
 4. Recompute the metadata from the built rpms with `rpm-metadata pkgname _build/*.rpm`. This picks up all
    unpredictable metadata changes to relationships, added/removed files, or binary rpm structure.
 5. Commit the package change together with the recomputed `srcpkg.json` in a single commit; `check`
    enforces this and the Release/`%changelog` conventions.

The canonical `srcpkg.json` comes from the buck build (the post-build recompute in the PR, see "Operation
details" in [packages.md](packages.md)). A `mockbuild`-based recompute of the same change likely has
changes in the `/usr/lib/.build-id/…` file lists: the GNU build-id is a content hash of each built binary,
and mock's buildroot resolves the live distro repos instead of buck's pinned snapshot, so the binaries are
not bit-identical between the two.

## Changing import source

This might happen sometimes if we e.g. decide to move a package from Fedora to CentOS, or more
commonly to move from Fedora 44 to 45 or rawhide. As each distro/branch is its own path, this means
importing the package at the new coordinate (`import-upstream` + `import`) and pointing the build
configuration at it; the old coordinate can be dropped once nothing selects it. We may eventually
introduce a proper tool verb for that cleanup, but that can wait until the need actually arises.

## Branch build curation (`_properties.json`)

A `packages/`_distro_`/`_branch_`/` directory may carry a `_properties.json` alongside the per-package
`srcpkg.json` files. It holds hand-authored, branch-level build curation. The importer folds it into the
generated branch `BUCK`'s `rpm_branch(...)` call (`regenerate_buck`); never hand-edit the `@generated`
`BUCK` itself.

### Buildroot-only packages

The `buildroot_only_packages` property lists packages that exist *only* in the distribution's build
ecosystem (koji's buildroot repo) and are *never* shipped in the compose, so the seed cannot provide them
when the BuildRequires cycle-breaking drops an edge (background in [packages.md](packages.md)):

```json
{ "buildroot_only_packages": ["glibc32"] }
```

Add a package when another package fails to build because a `BuildRequires` it needs is all three of:
(a) produced by one of our own packages, (b) never present in the seed/compose, and (c) inside a
BuildRequires cycle, so the lock drops the edge to it. `rpm_branch` validates the list on every load,
failing loudly if either invariant breaks: every entry must have a local provider (else the claim is
simply wrong), and the kept edges must still form a DAG (else that cycle needs a staged bootstrap instead,
not a kept edge).

### Seed-only packages

The opposite curation: the `seed_only_packages` property lists *source* packages whose builds we ship but
never want to build *against*. Their role in other package builds is tool use (a compiler, a signature
verifier, test data), not linkage. A `BuildRequires:` on anything a listed source package provides (`gcc`
covers gcc-c++ and libstdc++-devel, `kernel` covers kernel-devel) never becomes a dependency edge between
our packages; it is always resolved from the seed instead:

```json
{ "seed_only_packages": ["gcc"] }
```

The list is intentionally empty for now. See [packages.md](packages.md) for what listing a package
changes, and [self-host-approaches.md](self-host-approaches.md) for the analyzed candidates and their
trade-offs.

### Per-package rpmbuild options

Some specs offer build-trimming or other configuration through build conditionals (`%bcond`) or macros.
The `rpmbuild_options` property maps a package to the extra rpmbuild CLI options
(`--with`/`--without`/`--define`) its build is invoked with:

```json
{ "rpmbuild_options": { "gcc": ["--with=basic"] } }
```

`rpm-metadata` applies the same options when recomputing the package's srcpkg.json, so the recorded
subpackages and BuildRequires match what the build actually produces. An upstream import re-records the
package as koji built it (all subpackages); the next local build + `rpm-metadata` recompute converges it
back.
