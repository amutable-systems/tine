# Building Rust projects from source

A repository that builds images can also build a Rust project it has checked out, without packaging it first.
The project's committed `Cargo.lock` pins everything: Buck fetches each registry crate against the SHA-256
the lock records, and each git dependency as a fetch of exactly the locked commit. `cargo` itself
runs offline inside a box, and [`cargo-auditable`](https://crates.io/crates/cargo-auditable) writes the
crate graph into each binary so the image's SBOM reports those crates beside its packages.

`examples/image-rust-project` is a complete worked example: a builder box, two projects checked out
beside it, and an image carrying the resulting binaries.

## Declaring a build

```Starlark
load("@tine//cargo:defs.bzl", "cargo")

cargo.package(
    name = "hello",
    binaries = ["hello-cli"],
    box = ":rust.box",
)
```

- The project's own `Cargo.lock` is picked out of `src`; nothing declares it separately. A dynamic
  action reads it once it has been built and declares the fetches it names from there, so the lock can be
  a source file or an artifact another target produced, and the rule takes the same shape either way.
- `src` defaults to `<name>`: the checkout is expected in a directory named after the target. Pass `src`
  explicitly when it is called something else. Nothing needs to be added inside the checkout, i.e. a
  pristine project clone works.
- Use [`git.fetch()`](git.md) for a project that is not committed to this repository, and pass its work
  tree as `src`.
- The outermost `Cargo.lock` in `src` marks the workspace cargo builds in; a lock below it, such as a
  vendored project's, is never cargo's and is left alone. Two locks side by side name no single
  workspace, so narrow `src` then. A project that resolves nothing has no lock to commit; then its sole
  `Cargo.toml` marks the root instead.
- `binaries` names what to take out of `target/release`, and at least one is required: a crate that
  produces no binary has nothing an image could install. These are cargo binary names, which need not
  match the package. Each becomes a sub-target (`:hello[hello-cli]`), and together they are the target's
  default outputs. A name that the build does not produce fails the action; only the private name
  `__tine` is reserved by the rule.
- `box` is the build environment, declared by the consumer because only the consumer knows what its
  projects link against.

The binaries are ordinary artifacts, so an image installs one with an `image.copy()` operation:

```Starlark
image.rootfs_archive(
    name = "demo",
    package_manager = ":image.package-manager",
    packages = ["glibc"],
    ops = [image.copy(":hello[hello-cli]", "/usr/bin/hello-cli")],
    format = "tar",
)
```

## What the builder box needs

```Starlark
box.new(
    name = "rust.box",
    packages = ["cargo", "gcc", "python3", "rust"],
    release = "tine//catalog:fedora.rawhide.release",
)
```

- `cargo` and `rust` are the toolchain, and `gcc` links. `python3` runs the in-box driver, because
  tine's drivers execute through the box's own interpreter.
- Add whatever the project links against. A crate wrapping a C library needs that library's `-devel`
  package, and on Fedora anything using `pkg-config` also needs `rpm`, because `/usr/bin/pkg-config` is a
  wrapper that shells out to `rpm --eval`.
- The box is a build environment. It never becomes part of an image, and the toolchain is not shipped.

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
  action recompiles only what changed. `buck2 clean` is what forces a build from scratch.
- **A fetched or committed project keeps nothing between runs**. Its build directory never reaches the cache,
  only the final build targets.
- **Only crates.io registry sources work.** Another registry is rejected with the package named, rather
  than guessed at. A `Cargo.lock` older than version 3 is rejected too: it records no per-package
  checksums.
- **A project with dependencies must commit its `Cargo.lock`.** This is good practice for a binary crate
  anyway, for ensuring reproducibility and moving surprise failures from unrelated PRs to
  dependabot/renovate ones. The build passes `--locked`, so a lock that no longer agrees with `Cargo.toml`
  fails the build instead of quietly resolving something else. A manifest that declares no dependency
  table at all needs no lock, because there is nothing to pin and nothing for cargo to resolve; declaring
  one without committing the lock is refused, naming the table it found.
- **The checkout must live in the consuming repository.** A Buck package can only glob its own cell, so a
  project registered as a separate workspace cell would have to carry a build file.
- **A build cannot `rename(2)` a directory of its sources.** It builds on an overlay of them, where that
  fails with `EXDEV`, as it does in a container. `mv` works, since it copies instead.
- **A checkout's own `.cargo/config.toml` is refused.** Cargo would read it ahead of the configuration the
  build writes; keep it out of `src`.
- **A local `cargo build` inside the checkout needs `target/` in `.gitignore`**, which `tine init` writes.
  What Git ignores, Buck ignores; anything else in `src` is an input, so the build tree would be hashed
  and every file cargo writes there would rerun the build.
- **A cold daemon wants the network even when every crate is already cached**, because Buck asks the
  registry for sizes the lock does not record.
- **Two projects sharing a crate download it twice.** Each project owns its downloads, and content-based
  paths dedupe only within a target, so the copies are separate. A shared crate pool, along the lines of
  the package pool a repository owns, would fix it.
- **`cargo-auditable` is a pinned upstream binary**, not built from source, because building it from
  crates.io would need the mechanism it exists to serve. It is trusted like the pinned `buck2`, `python3`
  and `syft`.
