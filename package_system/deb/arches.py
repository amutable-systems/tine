# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""How Debian lays a root out for an architecture."""

# The compatibility directories an architecture's own ABI needs alongside the merged ones, taken
# from debootstrap's list. On amd64 `/lib64` is where every binary's ELF interpreter is looked up,
# so this is not cosmetic.
_MERGED_USR = {
    "amd64": ("lib32", "lib64", "libx32"),
    "i386": ("lib64", "libx32"),
    "loong64": ("lib32", "lib64"),
    "ppc64el": ("lib64",),
    "s390x": ("lib32",),
}


def merged_usr(arch: str) -> tuple[str, ...]:
    """The top-level directories that must be symlinks into `/usr` before a root is unpacked."""
    return ("bin", "sbin", "lib", *_MERGED_USR.get(arch, ()))
