# Development boxes

Declare a project development environment in the project-root `BUCK` file:

```python
load("@tine//box:rules.bzl", "box")

box(
    name = "box",
    packages = [
        "bash",
        "git",
        "python3",
    ],
)
```

The default release is `tine//catalog:fedora.rawhide.release`, resolved and installed by
`tine//catalog:fedora.rawhide.engine`. Override either attribute when the project needs a different base or
bootstrap environment.

From anywhere in that project, enter an interactive shell. Tine discovers the box target when the project
contains only one:

```console
$ tine box
```

Tine sets `TINE_BOX` to `box`, or `box:2`, `box:3`, and so on for nested boxes. For ordinary prompts it
also adds the corresponding marker to the standard `SHELL_PROMPT_PREFIX`.

Starship owns its multiline layout, so Tine leaves `SHELL_PROMPT_PREFIX` alone when `STARSHIP_SHELL` is
set. Add a native segment to `~/.config/starship.toml` instead:

```toml
[env_var.TINE_BOX]
format = '[\($env_value\)](bold cyan) '
```

Run a command non-interactively by placing it after `--`:

```console
$ tine box -- pytest
```

When a project declares multiple boxes, a root `//:box` remains the default. Otherwise, select one with
`--target`:

```console
$ tine box --target //tools:box -- make check
```

The box root is a normal engine artifact. Its userspace is pinned and read-only, while the relaxed entry
mode exposes the host environment, devices, network, current directory, and filesystems. Commands run as
the invoking user, and the project—including the shared `buck-out`—remains writable.
