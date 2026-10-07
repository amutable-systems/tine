<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# Development boxes

A box is a reproducible execution environment built from one base OS release. Build actions run inside one,
and running the target yourself drops you into that same pinned userspace. Declare one in the project-root
`BUCK` file:

```Starlark
load("@tine//box:defs.bzl", "box")

box.new(
    name = "dev.box",
    packages = [
        "bash",
        "git",
        "python3",
    ],
    release = "tine//catalog:fedora.rawhide.release",
)
```

`release` is the base the box is built from; point it at a different catalog entry when the project needs
another base. The environment that resolves and installs the box is the one that release names beside it,
`tine//catalog:fedora.rawhide.box` here, which a `resolver_box` of your own overrides.

Run the target to enter an interactive shell. Buck runs it from the directory you invoked it in, so this
works from anywhere in the project:

```console
$ tine buck run //:dev.box
```

Entering a box sets `TINE_BOX` to the target's name without its `.box` suffix, itself suffixed with `:2`,
`:3`, and so on when boxes nest. For ordinary prompts it also adds the corresponding marker to the standard
`SHELL_PROMPT_PREFIX`. The target itself sets these, so every way of entering it is marked the same.

Starship owns its multiline layout, so tine leaves `SHELL_PROMPT_PREFIX` alone when `STARSHIP_SHELL` is
set. Add a native segment to `~/.config/starship.toml` instead:

```toml
[env_var.TINE_BOX]
format = '[\($env_value\)](bold cyan) '
```

Run a command non-interactively by placing it after `--`:

```console
$ tine buck run //:dev.box -- pytest
```

A project may declare as many boxes as it likes; each is its own target:

```console
$ tine buck run //tools:check.box -- make check
```

The root a box is entered with is the same one build actions get. Its userspace is pinned and
read-only, while the relaxed entry mode exposes the host environment, devices, network, current directory,
and filesystems. Commands run as the invoking user, and the project, including the shared `buck-out`,
remains writable.

## Building on another box

If your project needs a box that just needs to add a few packages on top of an already existing box, you
can inherit its package list instead of copying it. Declare it as `parent`:

```Starlark
box.new(
    name = "dev.box",
    parent = "tine//catalog:fedora.rawhide.box",
    packages = [
        "gcc",
        "git",
    ],
    release = "tine//catalog:fedora.rawhide.release",
)
```

A `select()` in the parent's package list follows the architecture the child box is built or locked for.
Both boxes have to use the same packaging system.

If both boxes come from the same release and repositories, the box is built as a layer over its parent:
it shares the parent's installed root and only adds its own packages on top. Otherwise only the package
list is shared: the box is resolved and installed on its own.

A box with a committed lock keeps it when its parent's packages change, so refresh the catalog after
editing the parent or bumping tine (if you derive from a tine box). A layered box refuses to build until
you do. It also refuses when resolving both package lists together picks other packages than the parent
got, for example `libcurl` where the parent has `libcurl-minimal`. Set `layered = False` to install such a
box on its own.
