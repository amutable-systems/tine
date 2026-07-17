"""Shared command construction for terminal consumers of logical images."""

load("//engine:runtime.bzl", "EngineInfo", "chroot_run")

def terminal_image_command(image: Provider, exe: Dependency) -> cmd_args:
    """Run a terminal driver against one finalized logical-image stack."""
    cmd = cmd_args(chroot_run(engine = image.engine[EngineInfo], exe = exe))
    for lower in image.layers:
        cmd.add("--lower", lower)
    for snippet in image.tmpfiles:
        cmd.add("--tmpfiles", snippet)
    return cmd
