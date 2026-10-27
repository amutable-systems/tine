# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The releases that a package builds its images for when its PACKAGE file declares no distributions.

Each key names the per-distribution alias of an image, such as `:demo.fedora`. Each value is the
`<family>.<release>` prefix of a release that this catalog declares. If the dict contains one release,
images build for that release by their own names and get no aliases.
"""

RELEASES = {
    "arch": "arch.rolling",
    "debian": "debian.testing",
    "fedora": "fedora.rawhide",
}
