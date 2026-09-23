# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The architectures each catalog release is served for.

A box only ever takes the architectures its release has, so each release's are stated once. A target
elsewhere that names one of the boxes reads its architectures from here: they are the hosts it can
count on that box on, and it is skipped on any other.
"""

# What the rpmrepo mirror serves.
FEDORA_ARCHITECTURES = ["x86_64", "arm64"]

# Arch publishes only x86_64. Arch ARM is a separate distribution with its own mirrors and no archive.
ARCH_ARCHITECTURES = ["x86_64"]
