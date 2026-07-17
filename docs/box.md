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

From anywhere in that project, enter an interactive shell:

```console
$ tine box
```

Run a command non-interactively by placing it after `--`:

```console
$ tine box -- pytest
```

The default target is `root//:box`. Use `--target` for another package or box name:

```console
$ tine box --target root//tools:box -- make check
```

The box root is a normal engine artifact. Its userspace is pinned and read-only, while the relaxed entry
mode exposes the host environment, devices, network, current directory, and filesystems. Commands run as
the invoking user, and the project—including the shared `buck-out`—remains writable.
