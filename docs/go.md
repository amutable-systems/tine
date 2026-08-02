# Building Go projects from source

A repository that builds images can also build a Go project it has checked out, without packaging it first.
The project's committed `go.mod` and `go.sum` pin everything: one online action fetches the modules with `go
mod download` inside an engine, where go checks them against the `go.sum`. The compile then runs
offline against that fetched cache.

Go embeds the module list in every binary it links (`runtime/debug.BuildInfo`), so the image's SBOM reports
those modules.

`examples/image-go-project` is a complete worked example: a builder engine, two projects checked out
beside it, and an image carrying the resulting binaries.

## Declaring a build

```python
load("@tine//go:rules.bzl", "go_package")

go_package(
    name = "hello",
    binaries = ["hello-cli"],
    engine = ":go.engine",
)
```

- `srcs` defaults to `glob(["<name>/**"])`: the checkout is expected in a directory named after the
  target. Pass `srcs` explicitly when it is called something else. Nothing needs to be added inside the
  checkout, i.e. a pristine project clone works.
- One of the sources must be a `go.mod`, and its directory is the module root the build runs in. A
  checkout carrying more of them declares modules nested in the project, such as a tools or testdata
  helper; go leaves those out of a `./...` build, and so does this. The `go.sum` beside the project's own
  `go.mod` pins what the fetch may download. A module that resolves nothing has no `go.sum`; then
  nothing is fetched and the build runs with `GOPROXY=off`, so anything go would want to resolve fails.
- `binaries` names which commands to build; at least one is required. A name is the one `go install`
  would give: `cmd/hello-cli` produces `hello-cli`. A main package at the root of module
  `example.com/mycmd/v2` produces `mycmd`, because go skips the version element. Each name becomes a
  sub-target (`:hello[hello-cli]`), and together they are the target's default outputs. A name matching
  no main package fails the action and lists the ones that do, as does one that two main packages
  would both produce. `gocache`, `module-cache` and `src` are
  reserved.
- `engine` is the build environment, declared by the consumer because only the consumer knows what its
  projects need.
- `tags` are Go build tags: a `//go:build <tag>` line decides whether a file compiles at all. Nothing
  declares which tags a project defines, and one tag set applies to every module in the build, so this
  list has to come from the project's own build recipe.
- `cgo_cflags` adds C compiler flags for a cgo build, on top of the `-O2 -g` the driver always passes. A
  project needs one when a dependency's headers do not compile as the engine's compiler defaults.
- `cgo` forces cgo on or off. Unset, the engine toolchain's own default decides, as it would for a
  `go build` run in the checkout by hand; that default is on. `cgo = False` builds static binaries and
  needs no C compiler, taking Go's own resolver and user lookup instead of the system's. `cgo = True`
  turns a missing C compiler into a build failure, rather than that same silent switch.

The binaries are ordinary artifacts, so an image installs one with a `copy()` operation; see the example.

## What the builder engine needs

```python
engine(
    name = "go.engine",
    packages = ["ca-certificates", "golang", "python3"],
    release = "tine//catalog:fedora.rawhide.release",
    resolver_engine = "tine//catalog:fedora.rawhide.engine",
)
```

- `golang` is the toolchain, `python3` runs the in-engine drivers, and `ca-certificates` gives the module
  fetch its TLS trust for `proxy.golang.org`. A module that proxy does not serve is fetched directly, with
  `git`, which then has to be there too.
- Both actions set `GOTOOLCHAIN=local`: the engine's go is the toolchain, never a downloaded one, so a
  `go.mod` demanding a newer go than the engine carries fails the build instead.
- A project using cgo needs `gcc` and the `-devel` packages it binds, plus `pkgconf-pkg-config` and `rpm`
  for a `#cgo pkg-config:` directive, because Fedora's `/usr/bin/pkg-config` shells out to `rpm --eval`.
  Its binaries then link dynamically, so the image needs the matching runtime packages. This is not
  limited to projects that write C: with cgo enabled, importing `net` or `os/user` is enough. `cgo =
  False` uses Go's own implementations of those instead, and needs no C compiler.

The engine is a build environment. It never becomes part of an image, and the toolchain is not shipped.

## Properties and limits

How the modules are pinned, fetched and verified is described under "Go source builds" in
[design.md](design.md).

- **A project is one cache unit, and reruns are incremental.** Any change to its sources reruns the
  build action for the whole project, but go's build cache survives between runs, so it recompiles only
  what changed. Only changes to `go.mod`/`go.sum` rerun the fetch, whose module cache survives too, so a
  dependency bump downloads only what is missing. `buck2 clean` is what forces a build from scratch.
- **`go.mod` and `go.sum` must agree.** The build runs `-mod=readonly`, so a stale `go.sum` fails the
  build instead of quietly resolving something else; `go mod tidy` and commit the result.
- **The fetch is a network action**, one of the few build steps that reach out at all. It declares its own
  `GOPROXY` (`proxy.golang.org`, then `direct`) and checksum database rather than taking the engine's,
  which a distribution patches.
- **A local `go build` in the checkout wants `-o`.** A bare one writes its binary into the current
  directory, and everything in `srcs` is an action input, so that binary lands in the next build.
- **The checkout must live in the consuming repository.** A Buck package can only glob its own cell, so a
  project registered as a separate workspace cell would have to carry a build file.
- **A committed `vendor/` tree is ignored**, because the build passes `-mod=readonly`, and an explicit
  `-mod` is what turns off go's habit of preferring a vendor directory. Every module comes from the
  `go.sum` either way; excluding the tree from `srcs` only saves copying it.
- **`go.work` workspaces are refused**, because a workspace spans modules where this rule builds one.
