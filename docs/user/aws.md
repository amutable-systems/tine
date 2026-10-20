<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# Publishing images to AWS EC2

An [Amazon Machine Image](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/AMIs.html) (AMI) is not a
file format but an object in one EC2 region of one account: it refers to an EBS snapshot holding the disk,
and records how instances boot it (architecture, boot mode, TPM, network driver). tine builds everything
that is a function of the image, and uploads it as a separate `buck run` step that uses your AWS login.

## The AMI bundle

Every bootable [image.disk](images.md#imagedisk) has an `[ami]` subtarget: the raw disk and the
parameters to register it with. `buck build` it to get the bundle, `buck run` it to
[import](#importing-an-ami) it.

Nothing about the disk is specific to EC2: every initrd supports
[systemd IMDS](https://www.freedesktop.org/software/systemd/man/devel/systemd-imds.html) for passing
[credentials](https://systemd.io/CREDENTIALS/) and carries the `ena` EC2 network driver. The disk is only
as large as its partitions. The image's `virtual_size` sets the default size of an instance's root volume,
rounded up to GiB, and a launch can override that. On first boot, `systemd-repart` then creates or grows
partitions according to their definitions.

The AMI boots with UEFI only, and has a NitroTPM, which a disk needs if it seals anything to a TPM, such as
a root partition with `Encrypt=tpm2`. That limits it to instance types with NitroTPM support, which leaves
out the bare metal ones. Import with `--no-tpm` for arm64 bare metal; x86 bare metal only boots legacy
BIOS, which a tine disk does not support.

The AMI is named after the disk's published name: `<IMAGE_ID>_<IMAGE_VERSION>_<arch>`. Characters which
EC2 does not accept in names (such as the `^` in versions) get replaced by `-`. The unchanged name is the
AMI's description.

With a `secure_boot_key`, the parameters carry a UEFI variable store that enrolls the key's certificate as
PK, KEK and db. An instance then enforces Secure Boot from its first boot on and trusts only this key;
`examples/image-secureboot` has such a disk. Without a key, an instance boots in setup mode with Secure
Boot disabled.

## Logging in

tine never handles AWS credentials. It uses the Fedora packaged [AWS CLI](https://aws.amazon.com/de/cli/)
in a box, which runs with your home directory and environment:

```sh
tine buck run tine//tools:aws -- login
tine buck run tine//tools:aws -- configure set region eu-central-1
```

Any other `aws` command works the same way, so you do not need the AWS CLI installed on the host.

The import needs these IAM permissions:

- `ebs:StartSnapshot`, `ebs:PutSnapshotBlock`, `ebs:CompleteSnapshot`
- `ec2:GetEbsEncryptionByDefault`, `ec2:DescribeImages`, `ec2:DescribeSnapshots`, `ec2:RegisterImage`,
  `ec2:CreateTags`

## Importing an AMI

Run the disk's `[ami]` subtarget:

```sh
tine buck run '//examples/image:boot-demo.fedora[ami]'
```

This prints the new AMI ID. Options:

- `--name`: register under another name. EC2 requires names to be unique per account and region, and the
  import fails before uploading anything if the name is taken, so pass this to import the same image
  version again.
- `--region`: import into another region than the configured one.
- `--jobs`: the number of blocks uploaded in parallel, 16 by default.
- `--no-tpm`: register the AMI without a NitroTPM, for arm64 bare metal instance types.

Disk partitions often have a lot of empty space, e.g. an ESP sized for future updates. Only the blocks
holding data are uploaded. This only works with unencrypted snapshots, so the import refuses to run while
[EBS encryption by default](https://docs.aws.amazon.com/ebs/latest/userguide/encryption-by-default.html)
is enabled for the region.

If the import fails while uploading, EC2 cancels the incomplete snapshot after an hour. If it fails while
registering, the completed snapshot stays; the import printed its ID, delete it with
`aws ec2 delete-snapshot`.

## Running an instance with credentials

System credentials go into the instance's user data, as a JSON object with a `systemd.credentials` list.

**For testing only**, this one answers the first boot's questions and logs root in on the consoles
without a password, as `image.vm`'s `autologin` does for a local VM:

```json
{"systemd.credentials": [
  {"name": "firstboot.locale", "text": "C.UTF-8"},
  {"name": "firstboot.timezone", "text": "UTC"},
  {"name": "passwd.hashed-password.root", "text": "!*"},
  {"name": "agetty.autologin", "text": "root"},
  {"name": "login.noauth", "text": "yes"}
]}
```

For a root password, put a hash from `mkpasswd` into `passwd.hashed-password.root` and leave out the last
two. An EC2 key pair (`--key-name`) gets specified in the `ssh.authorized_keys.root` credential (for
production instances).

Launch an instance in the default VPC with that user data:

```sh
tine buck run tine//tools:aws -- ec2 run-instances \
    --image-id ami-0123456789abcdef0 \
    --instance-type t3.small \
    --user-data file://user-data.json
```

Add `--block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=20}'` for a volume size different than
the AMI's default.
