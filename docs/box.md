# Development boxes

A box is a reproducible execution environment built from one base OS release. Build actions run inside one,
and running the target yourself drops you into that same pinned userspace. Declare one in the project-root
`BUCK` file:

```Starlark
load("@tine//box:build.bzl", "box")

box(
    name = "dev.box",
    packages = [
        "bash",
        "git",
        "python3",
    ],
    release = "tine//catalog:fedora.rawhide.release",
    resolver_box = "tine//catalog:fedora.rawhide.box",
)
```

`release` is the base the box is built from and `resolver_box` the environment that resolves and installs
it; point them at a different catalog entry when the project needs another base or bootstrap environment.

Run the target to enter an interactive shell. Buck runs it from the directory you invoked it in, so this
works from anywhere in the project:

```console
$ tine buck run //:dev.box
```

Entering a box sets `TINE_BOX` to the target's name without its `.box` suffix, itself suffixed with `:2`,
`:3`, and so on when boxes nest. For ordinary prompts it also adds the corresponding marker to the standard
`SHELL_PROMPT_PREFIX`. The target itself sets these, so every way of entering it is marked the same.

Starship owns its multiline layout, so Tine leaves `SHELL_PROMPT_PREFIX` alone when `STARSHIP_SHELL` is
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

The root a box is entered with is the same artifact build actions get. Its userspace is pinned and
read-only, while the relaxed entry mode exposes the host environment, devices, network, current directory,
and filesystems. Commands run as the invoking user, and the project, including the shared `buck-out`,
remains writable.
