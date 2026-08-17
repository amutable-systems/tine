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

`tine init` writes the configuration a project needs, given the tine cell to build against:

```sh
tine init https://github.com/amutable-systems/tine   # or a checkout: tine init ~/Projects/tine
tine init --local ~/Projects/tine                    # that checkout, and follow it as it moves
```

That writes a `.buckconfig` registering Tine as a
[Buck2 external cell](https://buck2.build/docs/users/advanced/external_cells/) pinned to the origin's
current `HEAD`, a `.gitignore`, and the Buck2 release to fetch. Buck2 has an `init` of its own, which this
shadows: a project here needs the tine cell rather than an empty prelude project. A checkout on this
machine is an origin like any other and is pinned the same way; `tine cell override` is what makes a cell
follow one, and `--local` is that override declared as the project is written (see Development below).

The result is the configuration below, which can equally be written by hand; `init` writes an artifact
and a SHA-256 for each platform tine supports, of which one is shown:

```ini
[cells]
root = .
tine = tine
prelude = prelude
none = none

[cell_aliases]
config = prelude
# The tine cell's own aliases are honoured even as an external cell, and it aliases fbsource to
# satisfy the bundled prelude, so `none` has to resolve here too.
fbsource = none
# The toolchain the tine cell declares. An alias rather than a nested cell, so that a consuming project
# can pull tine in as an external cell.
toolchains = tine

[external_cells]
prelude = bundled
tine = git

[external_cell_tine]
git_origin = <url of this repository>
commit_hash = <sha1 to pin>

[parser]
target_platform_detector_spec = target:root//...->prelude//platforms:default target:tine//...->prelude//platforms:default

# VCS state, dependency and build trees, and the caches tools keep, so that none of them
# invalidates Buck's file watcher.
[project]
ignore = .git, .jj, **/.git, **/.jj, **/.hg, **/.svn, \
    **/buck-out, **/target, **/node_modules, **/__pycache__, **/.venv, **/venv, \
    **/.cache, **/.direnv, **/.gradle, **/.tox, **/.nox, \
    **/.mypy_cache, **/.pytest_cache, **/.ruff_cache, \
    **/.next, **/.parcel-cache, **/.turbo, **/.idea, **/.vscode

[build]
execution_platforms = prelude//platforms:default

# The Buck2 `tine` fetches and verifies before running anything; `tine bump` rewrites these.
[tine]
buck2-repository = daandemeyer/buck2
buck2-release = <release tag>
buck2-Linux-x86_64-artifact = buck2-x86_64-unknown-linux-musl.zst
buck2-Linux-x86_64-sha256 = <sha256 of that artifact>
```

Buck fetches the pinned commit into its own cache; nothing is checked into the consuming repo. The `tine`
path is only the location the cell would occupy, and no `tine/` or `prelude/` or `none/` directory needs to
exist. The `toolchains` cell Buck2 looks a toolchain up in is an alias to the tine cell, whose root package
declares the bootstrap interpreter one: Buck2 forbids a nested cell inside an external cell, so a
`toolchains/` directory in this repository would be a copy every consuming project needs of its own. A
project that wants toolchains beyond that one can still declare the cell itself, dropping the alias and
forwarding what it does not declare:

```Starlark
toolchain_alias(name = "python_bootstrap", actual = "tine//:python_bootstrap", visibility = ["PUBLIC"])
```

The one thing to vendor is [`bin/tine`](bin/tine) itself: it is what fetches Buck2, so it cannot come from
a cell Buck has not fetched yet. It is self-contained for that reason, so vendoring it is a copy and
nothing else, and refreshing it is the same copy from a newer checkout of this repository. Updating the
cell itself is an edit to `commit_hash` above.

`init` refuses to run where a `.buckconfig` already exists, and leaves any other file the project
already has alone. A repository with a `.gitignore` of its own keeps it: what that file would have
said (`/buck-out`, `**/buck-out`, `/.buckconfig.local`, and a `.*.tmp` alongside the last) goes into
`.git/info/exclude` instead, which belongs to the checkout and is committed nowhere. Only what is
missing is added, so running it again adds nothing.

To build against a checkout of this repository instead of the commit `.buckconfig` pins, override the
cell with it:

```sh
tine cell override tine ~/Projects/tine                   # follow that checkout as it is committed to
tine cell override tine ~/Projects/tine --commit v0.3.0   # or stay on one revision of it
tine cell list                                            # what is overridden, and with what
tine cell revert tine                                     # back to what the project pins
```

The declaration lands in the block `tine` owns in `.buckconfig.local`, which is what Buck reads and
is gitignored because it names a path only this machine has. There is no second file to keep in step
with it: what a build reads is what was declared, and `tine cell` is what edits it. A commit named
with `--commit` is recorded as `[tine] cell-<cell>-commit` there, which is what says the
`commit_hash` beside it is not to be resolved again. Overriding the tine cell hands the command over
too: every command that runs in the project re-executes that checkout's `bin/tine`, so the rules being
built and the command configuring them come from one checkout rather than pairing a vendored copy with
a checkout's rules. Every commit in that checkout is picked up by the next command; uncommitted work is
not, since Buck fetches the cell by commit. A declaration that stops resolving, because the checkout
moved or went away, fails every command that runs Buck
until it is pointed elsewhere or reverted: quietly building what `.buckconfig` pins instead would
build something other than what was asked for. `tine cell list` and reverting the checkout that went
away both keep working, so the way out is always open.

Editing the cell in place, with no commit in the loop, is the other way and not a further step: run
`tine cell revert tine` first, since the generated block declares an overridden cell a git cell on
every command and outranks the `.buckconfig` entry you are about to comment out. Then run
`tine buck expand-external-cell tine` and comment out `[external_cells] tine`.

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
tine cell <verb>         # build a cell from a local checkout, or go back to the pinned one
tine bump                # rewrite the pinned Buck2 release to the latest one published
tine init [<origin>]     # write the configuration a project needs, against this repository by default
                         # (--local follows the checkout it names rather than pinning its commit)
tine completion <shell>  # print the completion script for bash, fish or zsh
```

`tine buck` fetches the Buck2 the project pins, verifies it against the pinned SHA-256, caches it
under `${XDG_CACHE_HOME:-~/.cache}/tine/buck2/<sha256>`, and execs it, forwarding everything after
`buck` untouched. Before handing over, it rewrites its own block in `.buckconfig.local`, the
highest-precedence configuration Buck reads without being told to. The exception is `tine buck
complete`, which a completing shell runs on every keypress: it neither downloads nor writes, and
completes nothing until another command has fetched the binary. Everything else in that file is
yours and is left alone, and because the block comes first, a key you set for yourself still wins over
the one it writes:

- `[external_cells]` and `[external_cell_<cell>] git_origin`/`commit_hash` for every cell
  `tine cell override` was pointed at a checkout on this machine, which is the declaration as well
  as what Buck reads: the checkout's `HEAD`, or the revision the override named, which
  `[tine] cell-<cell>-commit` records as one to keep. Buck fetches that commit like any other pin, so
  uncommitted work in that checkout stays invisible until it is committed. A declaration that no longer
  resolves stops the command rather than quietly building what the project pins instead.
- `[tine] version-base`, `version-count`, `version-height`, `version-commit` and, for a dirty tree,
  `version-seconds`: what an image version is derived from, queried from git. The components rather than
  a version, because how much of the commit hash fits is a question each image's own partition labels
  answer: an image declaring `version = "auto"` (or `"auto:1.4.2"`, to name its own base) renders them
  against its own label budget. A checkout git cannot answer for gets a recorded reason instead of
  components, and no command fails until something asks for a version. See "Image versioning" in
  [images.md](docs/images.md).

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

`tine bump` and `tine init` ask GitHub which release to pin, and honour `GH_TOKEN` or `GITHUB_TOKEN`;
without one, GitHub allows 60 API requests an hour per address. `bump` reads and rewrites `.buckconfig`
alone, so a Buck2 you pin for yourself in `.buckconfig.local` still wins for `tine buck` without the
committed pin drifting to meet it.

The binary it caches is a plain Buck2 and can be run directly, with whatever the last `tine` left
behind in `.buckconfig.local`; `tine` exports its path as `BUCK2_BINARY` so that a tool Buck runs can
nest a command without refreshing configuration underneath the one that started it.

```sh
tine buck build tine//...                 # the examples, the catalog, and the tooling
tine buck run tine//tools:check           # lint, type-check, unit tests
tine buck run tine//tools:fmt             # auto-format and auto-fix
tine buck run tine//tools:verify-catalog  # assert the committed catalog lock matches
tools/ci.sh                               # the whole CI pipeline: checks, builds, image tests
```

The full command list is in [AGENTS.md](AGENTS.md).
