<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# Fetching project sources

tine wraps Buck's `git_fetch()` as `git.fetch()` so local development can use a checkout without changing
the pinned source. The wrapper normally fetches the requested commit. It uses a directory in the current Buck
package instead when that directory is non-empty and its name matches the target after removing a
trailing `.git`.

## Declaring a git fetch

For example, a `packages/hello/BUCK` can declare:

```starlark
load("@tine//cargo:defs.bzl", "cargo")
load("@tine//git:defs.bzl", "git")

git.fetch(
    name = "hello.git",
    repo = "https://github.com/example/hello",
    rev = "1c9e0f4b0f7d2a5e8b3c6d1a4f7b2e5c8d0a3f6b",
)

cargo.package(
    name = "hello",
    binaries = ["hello-cli"],
    box = ":rust.box",
    srcs = [":hello.git"],
)
```

## Building from a local checkout instead

The target above looks for a checkout at `packages/hello/hello/`: the package path followed by the target
name without `.git`. Use `tine mount` to place a working checkout there without copying it into the
project:

```sh
tine mount list                                        # includes packages/hello/hello
tine mount add packages/hello/hello ~/Projects/hello   # use the working checkout
tine mount remove packages/hello/hello                 # use the pinned commit again
```

A mount is also what makes a `cargo.package()` or `go.package()` build incremental: then its build
directory survives between runs, and an edit recompiles just what changed. See
[Rust source builds](../design/architecture.md#rust-source-builds).

The override has a few constraints:

- Only `git_fetch()` targets can be mounted, anything else is rejected.
- The directory must contain at least one file. An empty mount point or uninitialized submodule falls back
  to the remote fetch.
- Every file not excluded by `.gitignore` or `[project] ignore` is a build input. tine merges both, plus
  VCS metadata directories, into the generated ignore list; put build outputs in `.gitignore` and
  anything Git tracks but Buck should not see in `[project] ignore`.
- The local checkout is not checked against `rev`. Remove the mount before a release build to restore the
  pinned source.
- A `BUCK` file in the checkout creates a package boundary that the source glob cannot cross. Projects
  containing Buck packages must use the remote fetch.
