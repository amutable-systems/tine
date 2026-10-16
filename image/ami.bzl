# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Amazon Machine Images: a bootable disk with what EC2 needs to register it."""

load("//platforms:architecture.bzl", "ARCHITECTURES")

# What `RegisterImage` accepts in an image name besides letters and digits.
_NAME_PUNCTUATION = "()[] ./-'@_"

# EC2's label for the root volume, which ties its BlockDeviceMappings to the AMI's RootDeviceName. This
# is the conventional (but arbitrary) value of most Linux AMIs. Does not appear in guests, that's
# `/dev/nvme<N>n1` on Nitro instances.
_ROOT_DEVICE = "/dev/xvda"

def _ami_name(basename: str) -> str:
    return "".join([c if c.isalnum() or c in _NAME_PUNCTUATION else "-" for c in basename.elems()])

def _gib(size: str) -> int:
    """`size`, a SIZE_PATTERN string read base-1024 as systemd does, in GiB rounded up."""
    exponent = "KMGTPE".find(size[-1]) + 1
    number = float(size[:-1] if exponent else size)

    # Starlark has no math.ceil; flooring the negation rounds up.
    return int(-(-number * (1 << 10 * exponent) // (1 << 30)))

def declare_ami(actions: AnalysisActions, *, arch: str, basename: str, disk: Artifact, importer: RunInfo, virtual_size: str | None) -> list[Provider]:
    """Declare the AMI bundle of a bootable disk, for its `[ami]` subtarget.

    The bundle holds the disk and the `RegisterImage` parameters, which `aws ec2 register-image
    --cli-input-json` takes as they are once the import adds the snapshot. Running the subtarget
    imports the bundle with `importer`.
    """
    register = {
        "Architecture": ARCHITECTURES[arch].ec2,
        "BlockDeviceMappings": [
            {
                "DeviceName": _ROOT_DEVICE,
                "Ebs": {"DeleteOnTermination": True, "VolumeType": "gp3"},
            },
        ],
        # Not "uefi-preferred": instance types without UEFI would boot the disk in legacy BIOS mode.
        "BootMode": "uefi",
        "Description": basename,
        "EnaSupport": True,
        "ImdsSupport": "v2.0",
        "Name": _ami_name(basename),
        "RootDeviceName": _ROOT_DEVICE,
        # Limits the AMI to instance types with a NitroTPM, which leaves out bare metal ones.
        "TpmSupport": "v2.0",
        "VirtualizationType": "hvm",
    }

    # Without it, the volume is as large as the snapshot, which is as large as the disk.
    if virtual_size != None:
        register["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] = _gib(virtual_size)

    bundle = actions.symlinked_dir(
        "ami",
        {
            # Links, not copies: the disk is sparse, as its partitions are mostly empty.
            "disk.raw": disk,
            "register.json": actions.write_json("ami-register.json", register),
        },
    )
    return [DefaultInfo(default_output = bundle), RunInfo(args = cmd_args(importer, bundle))]
