<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# Building Rust projects from source

A repository that builds images can also build a Rust project it has checked out, without packaging it first.
The project's committed `Cargo.lock` pins everything: Buck fetches each registry crate against the SHA-256
the lock records, and each git dependency as a fetch of exactly the locked commit. `cargo` itself
runs offline inside a box, and [`cargo-auditable`](https://crates.io/crates/cargo-auditable) writes the
crate graph into each binary so the image's SBOM reports those crates beside its packages.

`examples/image-rust-project` builds two projects checked out beside it and copies their binaries into
an image.

## Declaring a build

```Starlark
load("@tine//cargo:defs.bzl", "cargo")

cargo.package(
    name = "hello",
    binaries = ["hello-cli"],
)
```

- The project's own `Cargo.lock` is picked out of `src`; nothing declares it separately. A dynamic
  action reads it once it has been built and declares the fetches it names from there, so the lock can be
  a source file or an artifact another target produced, and the rule takes the same shape either way.
- `src` defaults to `<name>`: the checkout is expected in a directory named after the target. Pass `src`
  explicitly when it is called something else. Nothing needs to be added inside the checkout, i.e. a
  pristine clone of a project that commits its `Cargo.lock` works.
- Use [`git.fetch()`](git.md) for a project that is not committed to this repository, and pass its work
  tree as `src`.
- The outermost `Cargo.lock` in `src` marks the workspace cargo builds in; a lock below it, such as a
  vendored project's, is never cargo's and is left alone. Two locks side by side name no single
  workspace, so narrow `src` then.
- `binaries` names what to take out of `target/<triple>/release`, and at least one is required: a crate that
  produces no binary has nothing an image could install. These are cargo binary names, which need not match
  the package. Each becomes a sub-target (`:hello[hello-cli]`), and together they are the target's default
  outputs. A name that the build does not produce fails the action. A name that is empty, `.` or `..`,
  contains a slash or a backslash, or is listed twice fails analysis. So does `box`, which names the
  sub-target that opens the build box.
- `box` is the build environment. It defaults to `tine//cargo:build.box`, described below.

The binaries are ordinary artifacts, so an image installs one with an `image.ops.copy()` operation:

```Starlark
image.archive(
    name = "demo",
    package_manager = ":image.package-manager",
    packages = ["glibc"],
    ops = [image.ops.copy(":hello[hello-cli]", "/usr/bin/hello-cli")],
    format = "tar",
)
```

## The build box

`tine//cargo:build.box` contains the distribution's Rust toolchain, `gcc` and `pkgconf`. It installs them
from the release of the distribution that the binaries are built for, so a binary links against the C
library of the image that installs it. Like an image, `cargo.package()` builds once for each distribution
of its package, and `:hello.fedora` names the build for Fedora. See
[Choosing a distribution](images.md#choosing-a-distribution).

The box is a build environment. It never becomes part of an image.

A crate that wraps a C library needs the library's development package. Add it with `buildroot_packages`:

```Starlark
cargo.package(
    name = "hello",
    binaries = ["hello-cli"],
    buildroot_packages = ["openssl-devel"],
)
```

The packages are installed in a layer on top of the box. Every `cargo.package()` that adds the same
packages to the same box shares that layer. The names are the distribution's own. A project that builds
for several families picks them with `distribution.select()`.

The build fails when the box's packages and `buildroot_packages` together resolve to other packages than
the box contains, such as another provider of a library. Then declare a box with all of the packages, and
pass it as `box`.

`buck run :hello[box]` opens a shell in the box that builds `:hello`, with its `buildroot_packages`
installed.

To build in a box of your own, pass it as `box`. Its packages need the toolchain, a linker and `python3`,
which runs tine's driver inside the box. A crate that looks a library up with `pkg-config` also needs
`pkgconf`, because the build runs `pkgconf` in place of `pkg-config`.

## Git dependencies

A crate that lives in git is pinned by the commit alone: every git source in the lock, transitive
dependencies included, becomes a fetch of exactly the locked commit, which the hash itself verifies.
Nothing else has to be pinned or committed, and the manifest needs no particular spelling of the
dependency.

Git dependencies are not vendored: cargo builds each one inside its own fetched repository, so workspace
inheritance and path dependencies between sibling crates behave exactly as in an online build, and cargo
itself verifies that the repository holds the commit the lock names.

## Properties and limits

How the crates are pinned, fetched and vendored is described under "Rust source builds" in
[architecture.md](../design/architecture.md).

- **A project is one cache unit, and reruns are incremental for a `tine mount`.** Any change to its sources
  reruns a single action for the whole project, but cargo's build directory survives between runs, so that
  action recompiles only what changed. `buck clean` is what forces a build from scratch.
- **A fetched or committed project keeps nothing between runs**. Its build directory never reaches the cache,
  only the final build targets.
- **Only crates.io registry sources work.** Another registry is rejected with the package named, rather
  than guessed at. A `Cargo.lock` older than version 3 is rejected too: it records no per-package
  checksums.
- **A project must commit its `Cargo.lock`**, even when it has no dependencies. This is good practice for
  a binary crate anyway, for ensuring reproducibility and moving surprise failures from unrelated PRs to
  dependabot/renovate ones. The build passes `--locked`, so a lock that no longer agrees with `Cargo.toml`
  fails the build instead of quietly resolving something else. The build also reads a `Cargo.lock` that git
  does not track. A local build then passes where a build from a fresh clone fails. For a project from
  `git.fetch()`, fetch a revision that contains the lock.
- **The checkout must live in the consuming repository.** A Buck package can only glob its own cell, so a
  project registered as a separate workspace cell would have to carry a build file.
- **A checkout's own `.cargo/config.toml` is refused**, even one that `.gitignore` excludes. Cargo would read
  it ahead of the configuration the build writes; keep it out of `src`. A `.cargo/config.toml` or a workspace
  `Cargo.toml` elsewhere in the consuming repository does not affect the build.
- **A build script cannot write beside its sources.** The build mounts the sources read-only, so a
  `build.rs` that writes outside `OUT_DIR` fails with a read-only file system error (`EROFS`). Cargo's
  documentation says that a build script should write only into `OUT_DIR`. The vendored crates.io
  dependencies are read-only too, so update or patch a dependency whose build script writes into its
  sources. A git dependency is checked out into a writable directory, so its build script can write
  beside its sources.
- **A local `cargo build` inside the checkout needs `target/` in `.gitignore`**. `tine init` writes it when
  it creates the `.gitignore`. If your project already had a `.gitignore`, add `target/` yourself. Buck
  reads every file that Git does not ignore as a source. While Git does not ignore `target/`,
  `cargo.package()` fails and names the directory. The build skips an ignored `target/` when it looks
  for the project's `Cargo.lock`.
- **A cold daemon wants the network even when every crate is already cached**, because Buck asks the
  registry for sizes the lock does not record.
- **Two projects sharing a crate download it twice.** Each project owns its downloads, and content-based
  paths dedupe only within a target, so the copies are separate. A shared crate pool, along the lines of
  the package pool a repository owns, would fix it.
- **`cargo-auditable` is a pinned upstream binary**, not built from source, because building it from
  crates.io would need the mechanism it exists to serve. It is trusted like the pinned `buck2`, `python3`
  and `syft`.
