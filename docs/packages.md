# rpm package import machinery

## Summary

`tine/tools/importer` is a tool to maintain a (partial) downstream derivative distribution in a monorepo. It mirrors upstream distribution (currently Fedora or CentOS are supported) dist-gits, and maintains local package deltas.

The importer itself is part of `tine`. The actual packages are maintained in a target distribution/product monorepo which vendors in `tine`; this is called `OS.git` in this document. (In this repository, `tine` is checked out inside `OS.git` as `tine/`; `repo_root()` walks up to the `OS.git` root that holds `packages/`.)

This document records the design: branch layout, metadata, operation internals, and rebuild strategy. The user guide is [importer.md](importer.md). It covers running the tool, the verb reference, the local-modification workflow, release conventions, changing import source, and `_properties.json` curation.

## Branch/Directory Layout

 - `OS.git`'s `upstream-rpm` branch contains the pristine imports and any future updates to these. I.e. this is a "dist-git mirror" from which we can then do efficient and local operations.
    * This is a standalone branch, no shared git history with `main`.
    * It contains a `packages/` directory with _distro_`/`_branch_`/`_srcpkg_ subdirectories, for example `packages/fedora/f44/glibc` or `packages/centos/c10s/openssl`.
    * It also contains corresponding `packages/`_distro_`/`_branch_`/`_srcpkg_`.json` metadata files (referred to as `srcpkg.json` throughout this doc) that map `srcpkg`'s binary packages to their relationships (BuildRequires, Requires, Recommends, Provides) and file lists. rpm computes these during package build (in particular, SONAME and pathname dependencies); they cannot be derived from the spec, but we need them for computing Amutable OS'es build system dependencies.
    * Each commit corresponds *exactly* to one upstream dist-git commit, i.e. for one package. It has the upstream commit message plus an `X-Upstream-Commit:` git trailer which refers to the imported upstream commit SHA. This is necessary to translate per-package dist git repos to our monorepo structure. It contains the exact upstream commit plus the corresponding metadata (`srcpkg.json`) changes. An upstream commit that changes nothing for us (like an unbuilt no-op mass rebuild, or a merge with no net change) is skipped entirely: nothing consumes it; see the `%autorelease` notes below.
    * There are *only* mechanical/automated commits on this, no human ones.

 - The "main" branch contains the packages from which we actually build AOS.
   * It mirrors the per-distro-release `packages/` structure from the `upstream-rpm` branch.
   * This allows us to build AOS images from multiple distributions in the future. We value having that capability. We won't actually *do* it at first to avoid multiplying the maintenance overhead, but eventually we may want to build e.g. a CentOS 10 based image.
   * This allows us to ship a "production" configuration which e.g. builds against fedora stable's `glibc` and `kernel`, and a "future" configuration which pulls in all rawhide packages (mostly for CI and learning about breakage as soon as possible).
   * Packages can be in sync, include local modifications, or even get removed (then we should also clean up the `upstream-rpm` branch, but that needs to happen async in e.g. a nightly workflow)
   * Might contain AOS specific packages like `amutablectl` with no upstream dist-git. I.e. `upstream-rpm` branch does *not* have that package. These live in `packages/aos/{latest,lts,...}/`_srcpkg_. (Note: We will try hard to just have one "latest" release stream -- but we could have more than one if we really have to)
   * A `packages/`_distro_`/`_branch_`/` directory may carry a `_properties.json` alongside the per-package `srcpkg.json` files. It holds hand-authored, branch-level build curation, see "Buildroot-only packages" below.

That layout allows efficient diff computation and hence determination whether a package is modified or not, and keeps the pristine `upstream-rpm` mirror cleanly separated from `main`. If a separate branch is considered too unwieldy, we could also have an `upstream-packages/` directory in `main` itself, but that will cause a lot of extra noise on `main`.

(Throughout this doc, `packages/…/pkgname/` is shorthand for a package's full `packages/`_distro_`/`_branch_`/`_pkgname_`/` path, which is identical on `upstream-rpm` and `main`.)

## Build configuration

To actually build an image, the selection needs to happen in some "build configuration". E.g. production could define "take packages from fedora/f44 by default, but take amutablectl (our own thing) and systemd (our fork) from aos/latest". A second "future" configuration defines "take everything from rawhide, plus amutablectl from aos/latest". This can take the form of a yaml, or buck rules, or something else, TBD.

## Operations

A single CLI tool (`tine/tools/importer`) performs all operations via CLI verbs; how to run it and the
verb reference are documented in [importer.md](importer.md).

## Design principles

 - Use `git` repository mechanics as much as possible, e. g. for computing diffs or queries for when which package changed
 - Avoid redundant state like duplicating version/release numbers or modification status in separate JSON metadata. This should only be done if performance would otherwise be too slow. `srcpkg.json` is a deliberate exception: on `upstream-rpm` it is *primary* data, not redundant — it can only be (re)computed by a full package build, so we cannot derive it from anything cheaper in the tree. On `main` it requires a package build, so it's expensive enough to record it statically. Its consistency is verified by the post-build recompute check (see below), per the next principle.
 - If we have to introduce any redundant metadata (e.g. the copied `srcpkg.json` on `main`), there must be a check happening on each commit/PR that validates its consistency.
 - Applying a local modification happens naturally: the developer updates the spec file, release, etc. There does not need to be any tool invocation for that, and future upstream updates then get merged with our modificatoins.
 - Local modifications don't modify `%changelog` and bump `Release:` in our own sub-namespace; the concrete conventions developers follow are spelled out in [importer.md](importer.md)
 - Every commit on `main` is a build. We don't "stage" modifications, as that just creates time bombs and makes it difficult to do integration testing. Devel branches can of course deviate from this (in particular, draft PRs which need conflict resolution, see below)
 - We configure our own `%dist` tag in our mock/build config, by appending `aos` to the upstream dist tag, e.g. `.fc44aos`, `.el10aos`, or `.hum1aos`. This keeps NEVRs unique across distros/branches (the same package may be built from several), and makes our rpms look different from Fedora etc. as they build against different library versions/toolchains; security scanners have to know about that, and NEVR in the VEX feed has to be accurate.
 - Rebuilding a package which uses `%autorelease` happens through a commit with subject "pkgname: Rebuild <reason>" (for humans) and an `X-Rebuild: pkgname` trailer (load-bearing). It contains *only* the resulting `srcpkg.json` changes. This avoids touching an arbitrary file in `packages/…/pkgname/`, as we want to avoid unnecessary diff noise.
 - tool code should deliberately be brittle: there should ideally be *no* `except:`. If there is any unforeseen situation in a newly imported or updated package, or a missing file or inconsistent state in our repository, the code crashes with a traceback, and developers need to fix it. Don't try to recover and apply heuristics/warnings.
 - We keep using the lookaside cache principle for sources. Storing them in git directly is out of the question, it will make `git clone` take way too long and become brittle. Depending on the choice of our build system, our lookaside cache might even be a part of e.g. buck2's shared cache in r2.

## Operation details

 * `update-upstreams` (temporarily) switches to the `upstream-rpm` branch, and parses the most recently imported upstream SHA from the `X-Upstream-Commit:` trailer in the most recent commit that changed `packages/…/PKGNAME/`. From there it can determine if there are newer upstream commits, and also do an efficient single-HTTP-query (without cloning, just `git ls-remote`) if upstream has any changes in the first place. It will only import upstream commits up to the most recent one that was actually built upstream, in order to avoid possibly broken staged changes. For Fedora we additionally ask bodhi whether a build was actually "published"; CentOS auto-publishes every koji build, we'll query its koji directly; and Hummingbird builds/publishes every commit.
 * `import-upstream`, `update-upstreams` fetch the binary RPMs, call `rpm -q --requires/--recommends/--provides` and `rpm -ql` (file list) on them, and update `srcpkg.json` accordingly. The `binaries` map is keyed by the build arch that *produces* each rpm: a noarch rpm is recorded under every build arch whose spec evaluation (`rpmspec --target <arch> -q`) produces its name. That correctly handles arch-specific noarch packages like glibc's per-arch `sysroot-<arch>-fcNN-glibc` cross-compilation sysroots, which belong only to their own arch's build. `BuildRequires` detection:
   - Static BuildRequires may be `%ifarch` (or similarly) conditional, so `srcpkg.json` records a per-arch `build_requires` map: the common set under the synthetic `_all` key, plus each build arch's conditional extras, evaluated from the dist-git spec with `rpmspec --target <arch>` in the build's distro context (`%dist`, `%fedora` or `%rhel`+`%centos` — an `.elN` build of ours is CentOS Stream — parsed off the srpm's dist tag).
   - Koji resolves dynamic BuildRequires (`%generate_buildrequires`) and writes them into the generated srpm's `Requires:`; but that got evaluated on whatever arch the srpm task happened to run on (often `s390x`), so the static part of that header can't be trusted per-arch. The dynamic part is recovered as the header minus the best-matching build arch's static set and folded into `_all`; a nonempty remainder without `%generate_buildrequires` in the spec crashes ("brittle by design" principle).
   - A locally built (`mockbuild`) srpm only has the static ones; mock's `buildreqs.nosrc.rpm` (which carries the dynamic ones) is transient and not retrievable post-build, so reproducing it is postponed to the production build system.
 * `import-upstream`, `update-upstreams` fetch the upstream `sources` from their lookaside cache, validate their SHA512 sums (`dist-git-client` already does that by itself, as lookaside cache download happens by SHA512sum), compute their SHA256 sums, and write them to `srcpkg.json` as a "sources" list of {url, sha256sum} objects. Buck's [http_file()](https://buck2.build/docs/prelude/rules/core/http_file/) only accepts SHA256.
 * `import-upstream`, `update-upstreams` will eventually have to upload the sources to our lookaside cache. For now, we don't need that, and keep the upstream distro's lookaside URLs.
 * `import-upstream` defaults to importing the latest commit. This matches the assumption that our srpms will auto-trail their upstreams anyway (necessary for "0 CVEs"). However, in some corner cases (Fedora rawhide being in the middle of a library or Python transition, etc.) we may have to import an older version. Hence this needs an option to specify a SHA. But this is only a temporary workaround, as hours later, the machinery will propose an update.
 * `import-upstream` imports only the target commit, no history. This is true even for `%autorelease`: the version and release live in the imported `srcpkg.json`, ground truth off the koji build's NEVR, so no commit-count context is needed and deeper history stays upstream. (We previously tried to do commit counting, but rpmautospec's number simply cannot be reproduced from imported history: it counts dist-git commits back to the last *evaluated* `Version:` change, past any reasonable import boundary, even past the switch to `%autorelease` — and its `-b`/`-e`/`-p` offsets defeat counting altogether.) The imported commit must have a published build, which is what generates the metadata; an explicit `--sha` without one is refused.
 * `import`/`update`: Since the package's path is identical on `upstream-rpm` and `main`, `git cherry-pick` applies upstream commits directly (preserving their message and `X-Upstream-Commit:` trailer). A literal `git merge` does not work, as the two branches share no common history. If `cherry-pick` ever fails for some corner case, we can fall back to applying the diff and copying the commit message in code.
 * `update` uses the same `X-Upstream-Commit:` git trailer anchor to spot new commits in `upstream-rpm` as `update-upstreams` does for upstream dist-git repos. The trailers (just like the entire commit messages) are preserved on main.
 * `update`: If there are conflicts, keep the markers, and create a commit starting with `CONFLICT:` and a list of conflicted paths. The posted PR will then be draft, and of course fail to build. This will provide enough visibility to human developers which updates need manual work.
 * `import`/`update` keep the 1:1 commit correspondence, i.e. will import new commits from `upstream-rpm` individually, keeping their `X-Upstream-Commit:` link.
 * `import`/`update` handling of `srcpkg.json`:
   - start with copying the `upstream-rpm`'s srcpackage metadata, with the assumption that in the vast majority of cases the relevant bits like `BuildRequires` will be identical in an AmutableOS build.
   - predict the `%dist` tag change (appending `aos` as above) and update dist tags in the relations lists before build. This will help the buck build system to resolve dependencies correctly and should already cover most of the "diff noise". This is a heuristic, but the result will be validated by the next step.
   - After building the packages (in the update PR), a post-build check re-computes the relationships/file lists, amends the corresponding commits, and re-pushes the temporary import branch for the PR.
 * `import`/`update` can further transform the per-srcpackage metadata into AOS build system specific form/rules in the future, once it has been designed/built. These rules will be included into the import commit, so that all corresponding changes are tied together.
 * `srpm`: Download the `sources` into `packages/…/pkgname/`, uncommitted. Like Fedora's koji plugin, compute the `%autorelease` value and write it into the SRPM by prepending a static spec definition (`%global autorelease <pkgrel>[.<minorbump>]%{?dist}` plus an empty `%global autochangelog %{nil}`). With that, the buildroot needs no git access and the SRPM rebuilds reproducibly anywhere. `<pkgrel>` comes straight from the imported `srcpkg.json`. The optional `.<minorbump>` is our AOS modification bump (the `.1`/`.2` *before* the dist tag, per the release rules in [importer.md](importer.md)) and equals the count of local commits since the most recent import: commits without `X-Upstream-Commit:` which either touch `packages/…/pkgname/` or have an `X-Rebuild: pkgname`. A fresh upstream release restarts the bump, so the next modification is `.1` again. This is the only place that counts commits, and it is fully contained in our history. We are not interested in changelogs, hence an empty `%autochangelog`. For production, the buck rules will do the same.
 * `update`/`list`: If the *only* dist-git difference between upstream and ours is in the `Release:` line, then disregard that delta (revert before cherry-pick) and consider the package to be unmodified. We commonly have to do such bumps for rebuilds against newer dependencies.
 * `rebuild`: If the package uses `%autorelease`, generate a commit with `X-Rebuild: pkgname` and no actual changes to the package directory; otherwise, bump `Release:` per the release rules in [importer.md](importer.md). In both cases, update `srcpkg.json` for the expected Release: bump result, similar to what `import`/`update` do.
 * `check`: Walk through all commits (or all since the given ref, `origin/main` in PRs) of the current branch (usually `main` or a temporary developer or package update PR branch). For each commit that touches `packages/`:
   - check that `srcpkg.json` is valid JSON
   - check that `Version:` and `Release:` numbers agree between the architectures in the `binaries` map.
   - if it has `X-Upstream-Commit:`, check that it resolves to a corresponding commit on the `upstream-rpm` branch at the identical `packages/` path
   - if it does *not* have an `X-Upstream-Commit:`, check that the package either uses `%autorelease` or `Release:` was bumped according to the release rules in [importer.md](importer.md). This includes AOS specific packages.
   - if it does *not* have an `X-Upstream-Commit:`, check that it changes `srcpkg.json`; release numbers are part of `Provides:`; guards against accidentally forgetting to update (or `git add`) metadata. Imported commits legitimately lack the metadata change when upstream never built that commit on its own (batched pushes, built once at the end with `%autorelease` counting them all)
   - if it does *not* have an `X-Upstream-Commit:` and *only* changes `srcpkg.json` metadata, check that the package uses `%autorelease` and commit has `X-Rebuild:`. Every other case of local metadata modification has to come from a sourceful modification or no-change rebuild which bumps `Release:`. Two legitimate exemptions:
     * imported commits: upstream's rpmautospec mass rebuilds are empty dist-git commits, and their build's recomputed json is all the mirrored commit contains
     * commits that also change the branch's `_properties.json`: its `rpmbuild_options` are a build input, so the recomputed json is the *result* of a real change
   - if it has `X-Rebuild:`, check that package uses `%autorelease` and the commit only changes `srcpkg.json` and nothing else.
   - if it does *not* have `X-Upstream-Commit:`, check that it does not touch `%changelog`
   - if package uses `%autorelease`, check the recorded release against the metadata chain: an imported commit that changes the recorded version-release must advance the release unless the version changed with it (upstream's counter restarted); a local commit's release must be the last import's release plus `.<count of local commits since>`.

   This does *not* check the integrity of the metadata -- we trust humans won't mess around with this, and it can only be validated through a rebuild of the rpms. There might be more checks in the future.

## Rebuilds

### Distribution status quo

When a `libfoo` shared library changes without a new SONAME, distributions generally don't rebuild reverse dependencies. While they *could* change behaviour/compilation due to changes in include files, it is generally expected that this doesn't happen.

Distributions *do* mass rebuilds of reverse dependencies (often no-change, sometimes with source changes to adjust to API changes) for SONAME changes and static linking, e.g. updated Go modules or Rust crates.

The mass rebuilds generally happen via a source package change/commit that bumps the "Release" number. Packages using `%autorelease` just get no-change commits. This is because rpm (same for deb) repositories are designed with the expectation that the contents of a particular NEVRA .rpm *never changes*. I.e. the NEVRA is a global fixed synonym for the bits in the .rpm/.deb file.

### Our strategy

For our buck based image builds we want/need to be stricter: The "no rebuilds for shared library updates" model does not work with buck's strict "changed input triggers rebuild" model, and for both correctness (include files!) and reproducibility reasons we will just do the full transitive reverse dependency rebuilds. We expect that our images are small enough for that to be viable. We can look into optimizations (such as moving to "header files + symbol table dump" as input instead of the full library rpm) if and when rebuild times become too much of a burden.

Our primary exported build artifact is an image, not a set of rpms. For as long as we can get away with it from a business PoV, we will treat the built RPMs as internal implementation detail and buck cache entries, and will not publish them to an official package mirror. The only reasons why we deal with NEVRAs at all is because we (1) inherit them from Fedora/CentOS imports, and (2) they are the language of SBOMs, security scanners, PURL and VEX feeds. This means that we can *accept* implicit rebuilds within buck, so that a `myapp-1.2-3` NEVRA that depends on `libfoo` will *change its contents* after a libfoo update. In that model, our git repo will only get the actual libfoo change commit, and all rebuilds of reverse dependencies, and of course the image itself, will happen automatically via buck. This includes SONAME changes and static linking cases, as they are included in buck's reverse dependency calculation rules (see below). Exception: when the rebuild incorporates the actual fix (like a bundled Go library with a CVE fix), we need the rebuild/Release bump to mark a CVE fix (in the VEX feed).

This changes if/when we decide to actually *publish* the built rpms. Then we need to play by the "NEVRA is a global fixed synonym for the bits" rule. Then a branch/PR that updates `libfoo` will automatically call `rebuild` for the affected reverse dependencies. This will have to be designed carefully: a naïve implementation that builds all transitive revdeps easily overbuilds and quickly reaches the entire package set for low-level packages like glibc, openssl, or libsystemd. Instead, we can ask buck to compute the rebuilds and which rpms *actually* change their content (for shared library updates we expect they don't, aside from -debuginfo which we filter out), and then commit the No-change rebuilds for only them, so that their Release: bumps for correct publication. We will need to design the integration with buck and automation, but only if and when we actually need it.

### Reverse dependency calculation

This will be implemented in buck, based on `srcpkg.json` metadata the tool produces.

 * Collect all src and binary package names and their `BuildRequires:` and _provides_ = `Provides:` ∪ `Files:` from the `srcpkg.json` metadata of all packages a build configuration selects, into a reverse map. (This treats path names as a special case of `Provides:`, they effectively work the same way). [Per policy](https://docs.fedoraproject.org/en-US/packaging-guidelines/#_file_and_directory_dependencies), dependencies may only refer to `/usr/bin/` and `/etc`, so filter the calculation down to these paths for efficiency.
 * A package _X_ is a direct revdep of `pkgname` iff _X's_ `BuildRequires:` set intersects `pkgname`'s _provides_.

This happens per build configuration on `main`, i.e. with AmutableOS specific metadata. Scoping to the selected package set keeps a rebuild from crossing into a different distro/configuration's packages.

This *overbuilds*: shared libraries which don't change SONAME generally *don't* require their revdeps to be rebuilt. They might change behaviour due to changed C header files and the like, but the C world is generally very disciplined with this. The main reason to do it is (1) be absolutely sure, and (2) keep package builds reproducible. If the overbuilding becomes a burden, we can optimize by checking the `Provides:` diff in the originating `srcpkg.json` and only triggering rebuilds on more specific changes like SONAME bumps. Or e.g. just avoid rebuilds for `glibc`.

## Buildroot-only packages

`BuildRequires:` (BR) cycles are unavoidable at the bottom of the stack (gcc needs glibc, glibc needs gcc). There is a strongly connected cycle of about 70 packages in Fedora's build root. buck rules cannot have cyclic deps, so the intra-cycle edges are broken by ignoring the cycle, and falling back to installing these BuildRequires from the "seed" (upstream distro packages). This is exactly what koji does, by building against its own previous build round.

That fallback assumes the seed can actually provide the dropped edge (i.e. BR). `buildroot_only_packages` is the list of packages for which that assumption is false: i.e. packages that exist *only* in the distribution's build ecosystem (koji's buildroot repo) and are *never* shipped in the compose (i.e. seed).

The only current case is `gcc`, which build-requires `(glibc32 or glibc-devel(x86-32))`. `glibc32` is a `glibc` subpackage that lives only in koji's buildroot, never in the compose; the only compose-side alternative is the 32-bit multilib `glibc-devel.i686`, but as we don't build/track that architecture, our build system cannot use it. (Also, the alternative declaration (and koji) prefer `glibc32` anyway). We *do* build `glibc32`, rawhide's `glibc.spec` emits it, but with the naïve "ignore the cycle" approach from above we cannot use it, and thus `gcc` cannot build.

The `buildroot_only_packages` branch property lists these packages: a listed edge survives the cycle-breaking, so it introduces a gcc → glibc dependency edge and hence gcc builds against *our* glibc build. The reverse glibc → gcc edge stays dropped as per the general rule above. When and how to add an entry, and the `_properties.json` format, are documented in [importer.md](importer.md).

## Seed-only packages

The opposite curation: the `seed_only_packages` branch property (defined and exemplified in [importer.md](importer.md)) resolves everything a listed source package provides from the seed, because its role in other package builds is tool use, not linkage. Since such edges are inside the BuildRequires cycle today, listing a package changes nothing about which rpms end up in a buildroot -- it turns the cycle fallback into policy, and thereby shrinks the cycle itself (fewer intra-cycle edges), which is what makes the future self-hosting approaches cheaper. The analyzed candidates (gcc, gnupg2, kernel, tzdata -- cycle 69 → 38) and their trade-offs are documented in [self-host-approaches.md](self-host-approaches.md).

## aarch64 support

The tool tracks a fixed `BUILD_ARCHES` (currently `x86_64` and `aarch64`) for imports. However, our own build infra will start with x86_64 builds and grow aarch64 later.

 - The `upstream-rpm` branch always has full arch coverage. `import-upstream`/`update-upstreams` fetch all tracked arch rpms from upstream koji.
 - The per-arch `build_requires` and the noarch producing-arch attribution are evaluated from the spec (`rpmspec --target <arch>`) and thus never require an actual build of that arch. This field has full coverage on `main` as well.
 - `rpm-metadata` expects the caller to provide rpms for *all* tracked arches; it wholesale-replaces the `binaries` map with what it is given. Synchronizing the multi-arch builds and collecting the rpms is left to the future build infra (within buck, or coordinating them on the outside with GitHub workflow, TBD). `check` validates that the recorded arches agree on Version/Release.
 - Until an aarch64 builder exists, a `srcpkg.json` update after an x86_64-only build drops or degrades the `aarch64` bucket in the recomputed `srcpkg.json`. This is acceptable because nothing depends on it yet: the buck build system pins x86_64 (`_ARCH` in tine's package_system/rpm/generated.bzl), and its self-hosting buildroot lock is fully determined by the x86_64 bucket.
 - A noarch subpackage from an x86_64-only build may still be recorded under the `aarch64` bucket: the attribution ("an aarch64 build of this spec would produce this rpm") is spec-derived and remains true. Such a bucket is incomplete (missing the arch binaries), but not wrong.
 - When an aarch64 builder comes online, the buckets repopulate naturally on the next build + `rpm-metadata` of each package; no repair step is needed.
