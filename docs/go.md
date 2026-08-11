# Building Go projects from source

A repository that builds images can also build a Go project it has checked out, without packaging it first.
The project's committed `go.mod` and `go.sum` pin everything: one online action fetches the modules with `go
mod download` inside a box, where go checks them against the `go.sum`. The compile then runs
offline against that fetched cache.

Go embeds the module list in every binary it links (`runtime/debug.BuildInfo`), so the image's SBOM reports
those modules.

`examples/image-go-project` is a complete worked example: a builder box, two projects checked out
beside it, and an image carrying the resulting binaries.

## Declaring a build

```Starlark
load("@tine//go:rules.bzl", "go_package")

go_package(
    name = "hello",
    binaries = ["hello-cli"],
    box = ":go.box",
)
```

- `srcs` defaults to `glob(["<name>/**"])`: the checkout is expected in a directory named after the
  target. Pass `srcs` explicitly when it is called something else. Nothing needs to be added inside the
  checkout, i.e. a pristine project clone works.
- One of the sources must be a `go.mod`, and its directory is the module root the build runs in. An
  action finds it once the sources have been built, and the fetch and the build are declared from what
  it reports, so the sources can be a checkout's files or one directory artifact another target
  produced, such as a `git_fetch()` of the project.
- A checkout carrying more than one `go.mod` declares modules nested in the project, such as a tools or
  testdata helper; go leaves those out of a `./...` build, and so does this. The `go.sum` beside the
  project's own `go.mod` pins what the fetch may download. A module that resolves nothing has no
  `go.sum`; then nothing is fetched and the build runs with `GOPROXY=off`, so anything go would want to
  resolve fails.
- `binaries` names which commands to build; at least one is required. A name is the one `go install`
  would give: `cmd/hello-cli` produces `hello-cli`. A main package at the root of module
  `example.com/mycmd/v2` produces `mycmd`, because go skips the version element. Each name becomes a
  sub-target (`:hello[hello-cli]`), and together they are the target's default outputs. A name matching
  no main package fails the action and lists the ones that do, as does one that two main packages
  would both produce. Only the private name `__tine` is reserved by the rule.
- `box` is the build environment, declared by the consumer because only the consumer knows what its
  projects need.
- `tags` are Go build tags: a `//go:build <tag>` line decides whether a file compiles at all. Nothing
  declares which tags a project defines, and one tag set applies to every module in the build, so this
  list has to come from the project's own build recipe.
- `linker_flags` are passed to the Go linker as `-ldflags`, the same list the Buck prelude's `go_binary`
  takes. Nothing is passed by default. `["-s", "-w"]` drops the symbol table and DWARF, which is what a
  distribution's packaging does to a binary before shipping it and takes roughly a third off a Go binary;
  the SBOM is unaffected, because the module list lives in `.go.buildinfo` and survives. `-X` stamps a
  variable at link time, and `-extldflags` reaches the C linker of a cgo build.
- `cgo_cflags` adds C compiler flags for a cgo build, on top of the `-O2 -g` the driver always passes. A
  project needs one when a dependency's headers do not compile as the box's compiler defaults.
- `cgo` forces cgo on or off. Unset, the box toolchain's own default decides, as it would for a
  `go build` run in the checkout by hand; that default is on. `cgo = False` builds static binaries and
  needs no C compiler, taking Go's own resolver and user lookup instead of the system's. `cgo = True`
  turns a missing C compiler into a build failure, rather than that same silent switch.

The binaries are ordinary artifacts, so an image installs one with a `copy()` operation; see the example.

## What the builder box needs

```Starlark
box(
    name = "go.box",
    packages = ["ca-certificates", "golang", "python3"],
    release = "tine//catalog:fedora.rawhide.release",
    resolver_box = "tine//catalog:fedora.rawhide.box",
)
```

- `golang` is the toolchain, `python3` runs the in-box drivers, and `ca-certificates` gives the module
  fetch its TLS trust for `proxy.golang.org`. A module that proxy does not serve is fetched directly, with
  `git`, which then has to be there too.
- Both actions set `GOTOOLCHAIN=local`: the box's go is the toolchain, never a downloaded one, so a
  `go.mod` demanding a newer go than the box carries fails the build instead.
- A project using cgo needs `gcc` and the `-devel` packages it binds, plus `pkgconf-pkg-config` and `rpm`
  for a `#cgo pkg-config:` directive, because Fedora's `/usr/bin/pkg-config` shells out to `rpm --eval`.
  Its binaries then link dynamically, so the image needs the matching runtime packages. This is not
  limited to projects that write C: with cgo enabled, importing `net` or `os/user` is enough. `cgo =
  False` uses Go's own implementations of those instead, and needs no C compiler.

The box is a build environment. It never becomes part of an image, and the toolchain is not shipped.

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
  `GOPROXY` (`proxy.golang.org`, then `direct`) and checksum database rather than taking the box's,
  which a distribution patches.
- **A local `go build` in the checkout wants `-o`.** A bare one writes its binary into the current
  directory, and everything in `srcs` is an action input, so that binary lands in the next build.
- **A checkout is not the only way in.** The module is found in the built sources rather than at parse
  time, so a project whose tree arrives from a `git_fetch()` needs nothing committed in the consuming
  repository beyond the commit it is pinned at.
- **A committed `vendor/` tree is ignored**, because the build passes `-mod=readonly`, and an explicit
  `-mod` is what turns off go's habit of preferring a vendor directory. Every module comes from the
  `go.sum` either way; excluding the tree from `srcs` only saves copying it.
- **`go.work` workspaces are refused**, because a workspace spans modules where this rule builds one.
