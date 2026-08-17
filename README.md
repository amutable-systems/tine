# Tine

Tine builds native packages and composes operating-system images with [Buck2](https://buck2.build/).
Package repositories are pinned by committed snapshots. Every build action runs in an unprivileged
hermetic sandbox (no root, no containers, no network). Filesystem images are stacks of overlay deltas.
The outputs are reproducible and content-cached: deterministic archives, unified kernel images, and
dm-verity protected GPT disks.

## Requirements

The host needs Linux with unprivileged user namespaces, a `/usr/bin/python3` of 3.9 or newer, and
`git`. Also `/dev/kvm` for running VMs, and `zstd` unless that
python is 3.14 or newer, which unpacks the download itself. The first [`tine`](bin/tine) invocation
that runs Buck fetches and verifies the pinned Buck2 binary into
`${XDG_CACHE_HOME:-~/.cache}/tine/buck2` (one directory per pin, safe to delete); everything else is
fetched and cached by Buck itself.

Buck2 refuses to start without `$HOME` (some CI environments don't set it). In that case, the `bin/tine`
command sets `$HOME` to a gitignored `.buck/` in tine's root directory before running anything: that is
then where the cache above lands too. Buck2 keeps its daemon under `$HOME`, so a run without one gets a
daemon of its own; keep `HOME` the same across the commands in a checkout and let CI be what has none.
Kill the daemon if you change the home directory between runs.

## Quick start

The commands below spell the entry point `tine`; it is [`bin/tine`](bin/tine) in a checkout, so run it
from there, or put it on `PATH`. Build a minimal tar image with a few packages and custom files:

```sh
tine buck build //examples/image:demo.fedora
```

Build a bootable GPT disk with a unified kernel image and dm-verity protected `/usr`:

```sh
tine buck build //examples/image:boot-demo.fedora
```

Boot it in an ephemeral VM:

```sh
tine buck run //examples/image:boot-demo-vm.fedora
```

Images are declared in ordinary `BUCK` files ([examples/image/BUCK](examples/image/BUCK) has the complete
demos):

```Starlark
load("@tine//image:defs.bzl", "copy", "rootfs_archive", "run")

rootfs_archive(
    name = "demo",
    package_manager = ":image.package-manager",
    packages = ["bash", "coreutils"],
    ops = [
        copy("marker.txt", "/etc/tine/marker"),
        run(["/usr/bin/bash", "-c", "echo built-by-tine >> /etc/tine/marker"], chroot = True),
    ],
    format = "tar",
)
```

## Consuming the cell

Check this repository out inside the project, as a pinned git clone, worktree, or submodule. Register
that directory as the `tine` cell with `init`, then build:

```sh
git submodule add https://github.com/amutable-systems/tine tine
tine/bin/tine init          # write the project's .buckconfig and .gitignore
tine/bin/tine buck build //packages/...
```

The configuration `init` writes is this repository's own [`.buckconfig`](.buckconfig) with `root = .` added,
the `tine` cell pointing at the checkout, and `root//` in the platform detector. Everything else is copied
verbatim, so what a project has no say in cannot fall out of step with the cell. The entry point is that
checkout's [`bin/tine`](bin/tine). Move to a newer tine with `git submodule update` or the equivalent
change in your pinned `git clone`.

The `toolchains` cell Buck2 looks a toolchain up in is an alias to the tine cell, whose root package
declares the bootstrap interpreter one: Buck2 forbids a nested cell inside an external cell, so a
`toolchains/` directory in this repository would be a copy every consuming project needs of its own. A
project that wants toolchains beyond that one can still declare the cell itself, dropping the alias and
forwarding what it does not declare:

```Starlark
toolchain_alias(name = "python_bootstrap", actual = "tine//:python_bootstrap", visibility = ["PUBLIC"])
```

Buck reads the checkout in place, so a branch or an uncommitted edit is active immediately.

To develop tine against a different checkout, mount it over the checkout registered by the project:

```sh
tine mount add tine ~/Projects/tine    # use this checkout as tine/
tine mount list                        # list active mounts
tine mount remove tine                 # use the project's checkout again
```

The command records the mount in the gitignored `.buckconfig.local`. It runs the build in a private mount
namespace where `tine/` is a bind mount of `~/Projects/tine`, so uncommitted edits are available
immediately. When the tine cell is mounted, `tine/bin/tine` re-executes the mounted copy of itself. The
rules and the command configuring them therefore come from the same checkout.

Mounts cannot overlap or cover `.buck/`. Nested mounts would depend on application order, while `.buck/`
holds Tine's namespace-private Buck configuration, so `tine mount add` rejects both.

`tine mount` does not restart Buck2 immediately. On the next `tine buck`, Buck itself reuses a daemon
started for the same mounts or replaces one started for different mounts, under its own lifecycle lock.
Replacing the daemon does not remove `buck-out`.
Mounting requires unprivileged user namespaces, which some distributions disable.

A consuming OS monorepo ("OS.git" in these docs) additionally holds package sources under
`packages/<distro>/<branch>/<package>`, imported and updated from upstream dist-gits (Fedora, or CentOS
Stream) by the [importer](docs/importer.md). Those build as ordinary Buck targets and feed images, so an
image build rebuilds exactly the affected packages.

## Documentation

User guides:

- [Building images](docs/images.md): host requirements, declaring images and layers, output formats, the
  VM runner
- [Building Rust projects](docs/cargo.md): building a checked-out Rust project offline, with its
  crate graph in the executables and image SBOM
- [Building Go projects](docs/go.md): the same for a checked-out Go project, whose module list go
  itself embeds in the executables
- [Maintaining packages](docs/importer.md): importing and updating packages from upstream distributions,
  local modifications, branch curation
- [Development boxes](docs/box.md): pinned interactive development environments

Design:

- [Architecture](docs/design.md): component model, decision record, current limitations, roadmap
- [Package import machinery](docs/packages.md): branch layout, metadata, consistency checks, rebuild
  strategy
- [Self-hosting approaches](docs/self-host-approaches.md): future design for the BuildRequires cycle

## Development

[`bin/tine`](bin/tine) is the entry point, a command of its own with Buck2 behind one of its verbs:

```sh
tine buck <arguments>    # run the pinned Buck2, which every other command configures
tine mount <verb>        # manage external directories mounted over project paths
tine init [<path>]       # write the configuration a project needs, for the checkout this command is in
tine completion <shell>  # print the completion script for bash, fish or zsh
```

`tine buck` fetches the Buck2 this cell pins, verifies it against the pinned SHA-256, caches it
under `${XDG_CACHE_HOME:-~/.cache}/tine/buck2/<sha256>`, and execs it, forwarding everything after
`buck` untouched. Before handing over, it rewrites its own block in `.buckconfig.local`, the
highest-precedence configuration Buck reads without being told to. The exception is `tine buck
complete`, which a completing shell runs on every keypress: it neither downloads nor rewrites the shared
configuration, and completes nothing until another command has fetched the binary. Everything else in
that file is yours and is left alone, and because the block comes first, a key you set for yourself still
wins over one it writes. While mounts are active, Tine bind-mounts a private copy of the root
`.buckconfig` with a `[buck2] daemon_buster` appended. A project cannot set that key while mounts are
active. The generated local configuration remains live:

- `[tine_mounts] <project path> = <external directory>` for each `tine mount add`. Buck parses the
  section, but no rule consumes it; `tine buck` uses it to set up the mount namespace before Buck starts.
  An invalid declaration stops the command instead of silently using the checked-in directory.
- `[tine] version-base`, `version-count`, `version-height`, `version-commit` and, for a tree carrying
  uncommitted work, `version-dirty`: what an image version is derived from, queried from git. The
  components rather than a version, because how much of the commit hash fits is a question each image's
  own partition labels answer: an image declaring `version = "auto"` (or `"auto:1.4.2"`, to name its own
  base) renders them against its own label budget. A checkout git cannot answer for gets a recorded reason
  instead of components, and no command fails until something asks for a version. See "Image versioning"
  in [images.md](docs/images.md).

That configuration goes into a file rather than onto the command line because `buck2 complete`, which
serves shell completion, accepts no configuration flags at all: a wrapper injecting `-c` would complete
against different configuration than it builds with. Completion comes from the wrapper too, which rewrites
Buck2's own script so that its completions hang off `tine buck` and the target completions route back
through it. It reads that script out of the pinned Buck2, so run it from inside a project, as every
verb but `init` is:

```sh
tine completion fish > ~/.config/fish/completions/tine.fish
tine completion bash > ~/.local/share/bash-completion/completions/tine
tine completion zsh > ~/.local/share/zsh/site-functions/_tine   # any directory on $fpath
```

[`tools/tools.json`](tools/tools.json) is where this cell declares the Buck2 its rules are tested
against, alongside every other pinned tool. `bin/tine` reads it and bootstraps Buck2 itself.
A `[tine] buck2-*` key in the consuming project can override that pin. Be careful though: tine depends
on fixes that exist only in that fork, so running another Buck2 is unsupported.

`tine//tools:bump` moves that pin with every other one, so nothing in this command asks GitHub anything.

The binary it caches is a plain Buck2, but running it directly does not enter a declared mount namespace.
`tine` exports its path as `BUCK2_BINARY` so that a tool Buck runs can nest a command without refreshing
configuration underneath the one that started it.

```sh
tine buck build tine//...                 # the examples, the catalog, and the tooling
tine buck run tine//tools:check           # lint, type-check, unit tests
tine buck run tine//tools:fmt             # auto-format and auto-fix
tine buck run tine//tools:verify-catalog  # assert the committed catalog lock matches
tools/ci.sh                               # the whole CI pipeline: checks, builds, image tests
```

The full command list is in [AGENTS.md](AGENTS.md).
