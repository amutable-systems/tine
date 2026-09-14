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
load("@tine//image:defs.bzl", "image")

image.rootfs_archive(
    name = "demo",
    package_manager = ":image.package-manager",
    packages = ["bash", "coreutils"],
    ops = [
        image.copy("marker.txt", "/etc/tine/marker"),
        image.run(["/usr/bin/bash", "-c", "echo built-by-tine >> /etc/tine/marker"], chroot = True),
    ],
    format = "tar",
)
```

## Consuming the cell

Check this repository out inside the project, as a pinned git clone, worktree, or submodule. Register
that directory as the `tine` cell with `init`, then build:

```sh
git submodule add https://github.com/amutable-systems/tine tine
tine/bin/tine init          # write tine.toml, .buckconfig, and .gitignore
tine/bin/tine buck build //packages/...
```

`init` records the cell in `tine.toml` and generates the project's `.buckconfig` from the checkout's
defaults. Each ordinary `tine buck` command regenerates that file from the selected checkout, including
a mounted override. Put persistent project settings in `[buckconfig.*]` tables in `tine.toml`; direct
edits to the generated `.buckconfig` are overwritten.

Keep `tine.toml` and the generated `.buckconfig` committed in the consuming project. The latter is a
bootstrap file: Tine needs it to locate the project before it can refresh the defaults. Ignoring it
would leave a fresh clone unable to run `tine buck` without recreating that file first.

Use `tine.local.toml` or `.buckconfig.local` for machine-local overrides, and keep both untracked. Tine
updates only its generated block in `.buckconfig.local`, preserving text outside that block. The
[configuration design](docs/design.md#shared-configuration-and-nested-commands) explains the refresh order
and standalone behavior.

The entry point is [`bin/tine`](bin/tine). For Buck commands and completion, an existing `tine` on `PATH`
automatically hands over to the configured tine cell's `bin/tine`, even without a mount. There is no need
to change `PATH` when selecting another checkout. Move to a newer tine with `git submodule update` or the
equivalent change in your pinned `git clone`.

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
tine mount list                        # list targets and their active sources
tine mount remove tine                 # use the project's checkout again
```

Cell roots and local-checkout slots declared by `git_fetch()` are mount targets. The `add` command rejects
anything else. `tine mount list` shows the active local source, or `default` when a target is not
overridden. `add` creates a missing directory for a declared checkout slot. Builds use the selected
checkout, including uncommitted edits; selecting a different tine checkout also selects its command,
rules, and pinned Buck2.

Mount changes take effect on the next `tine buck`. Switching mounts can interrupt builds already using a
different checkout. Mounting requires unprivileged user namespaces, which some distributions disable. The
[architecture document](docs/design.md#building-with-out-of-tree-checkouts) explains how mounts and daemon
reuse work.

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
- [Fetching project sources](docs/git.md): pinning an external project while allowing a local checkout
  during development
- [Maintaining packages](docs/importer.md): importing and updating packages from upstream distributions,
  local modifications, branch curation
- [Development boxes](docs/box.md): pinned interactive development environments

Design:

- [Architecture](docs/design.md): component model, decision record, current limitations, roadmap
- [Package import machinery](docs/packages.md): branch layout, metadata, consistency checks, rebuild
  strategy
- [Self-hosting approaches](docs/self-host-approaches.md): future design for the BuildRequires cycle
- [Shared build cache](docs/remote-cache.md): set up for a developer, a build runner, and the bucket
- [Shared build cache design](docs/remote-cache-design.md): threat model, bucket layout, signatures and
  keys of tine's own cache

## Development

[`bin/tine`](bin/tine) is the entry point, a command of its own with Buck2 behind one of its verbs:

```sh
tine buck <arguments>    # run the pinned Buck2, which every other command configures
tine mount <verb>        # manage external directories mounted over project paths
tine init [<path>]       # write the configuration a project needs, for the checkout this command is in
tine completion <shell>  # print the completion script for bash, fish or zsh
```

`tine buck` runs the pinned Buck2 and forwards everything after `buck` unchanged. Use it instead of
running Buck2 directly so that builds use your selected checkouts. Image versions can be derived from
the project's Git history; see "Image versioning" in [images.md](docs/images.md).

Install shell completion from inside a project:

```sh
tine completion fish > ~/.config/fish/completions/tine.fish
tine completion bash > ~/.local/share/bash-completion/completions/tine
tine completion zsh > ~/.local/share/zsh/site-functions/_tine   # any directory on $fpath
```

Target completion neither downloads Buck2 nor refreshes shared configuration. Run an ordinary
`tine buck` command first to fetch the binary if target completion has no results.

[`tools/tools.json`](tools/tools.json) is where this cell declares the Buck2 its rules are tested
against, alongside every other pinned tool. `tine//tools:bump` updates those pins. Tine depends on fixes
in its Buck2 fork, so running another Buck2 is unsupported.

```sh
tine buck build tine//...                 # the examples, the catalog, and the tooling
tine buck run tine//tools:check           # lint, type-check, unit tests
tine buck run tine//tools:fmt             # auto-format and auto-fix
tine buck run tine//tools:verify-catalog  # assert the committed catalog lock matches
tools/ci.sh                               # the whole CI pipeline: checks, builds, image tests
```

The full command list is in [AGENTS.md](AGENTS.md).
