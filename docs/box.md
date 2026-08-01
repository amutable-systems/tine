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

Run the target to enter an interactive shell. Buck runs it from the directory you invoked it in, so this
works from anywhere in the project:

```console
$ tools/buck run //:box
```

Entering a box sets `TINE_BOX` to the target's name, suffixed with `:2`, `:3`, and so on when boxes nest.
For ordinary prompts it also adds the corresponding marker to the standard `SHELL_PROMPT_PREFIX`. The box
target itself sets these, so every way of entering it is marked the same.

Starship owns its multiline layout, so Tine leaves `SHELL_PROMPT_PREFIX` alone when `STARSHIP_SHELL` is
set. Add a native segment to `~/.config/starship.toml` instead:

```toml
[env_var.TINE_BOX]
format = '[\($env_value\)](bold cyan) '
```

Run a command non-interactively by placing it after `--`:

```console
$ tools/buck run //:box -- pytest
```

A project may declare as many boxes as it likes; each is its own target:

```console
$ tools/buck run //tools:box -- make check
```

The box root is a normal engine artifact. Its userspace is pinned and read-only, while the relaxed entry
mode exposes the host environment, devices, network, current directory, and filesystems. Commands run as
the invoking user, and the project—including the shared `buck-out`—remains writable.
