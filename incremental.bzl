"""Maintain a directory for incremental compiler state which buck does not track.

See "Rust source builds" in docs/design.md.
"""

# `.buck` is ignored by the watcher and by git, and nothing clears it between runs.
_INCREMENTAL_DIR = ".buck/incremental"

def incremental_dir(label: Label, leaf: str) -> str:
    """A directory for one configured target's incremental compiler state.

    Keyed like buck's own output paths to avoid collisions.
    """
    if "/" in label.name or label.name in (".", ".."):
        fail("incremental_dir: target name {!r} cannot be a path component".format(label.name))

    parts = [
        _INCREMENTAL_DIR,
        label.cell,
        label.package,
        "__" + label.name + "__",
        label.configured_target().config().hash,
        leaf,
    ]

    # A target in the root package has no package component to contribute.
    return "/".join([part for part in parts if part])
