"""What a build publishes, under the names it publishes them as.

A published name is a contract with whatever consumes the build: systemd-sysupdate matches its
transfer sources against them, so the name carries the image identity, the version, and, for a
partition, the type and UUID repart gave it. Each rule names what it produces, and a rule that
gathers artifacts from several targets reads those names here rather than composing any of its own.

A partition is the exception: its name exists only once repart has run, so it travels as the typed
result of the run rather than as a name, and whatever materializes it reads the name back out of
the metadata written beside it.
"""

load("//image_format:disk.bzl", "PartitionInfo")

PublishedInfo = provider(
    doc = "The artifacts one target contributes to a release, keyed by published name.",
    fields = {
        "artifacts": provider_field(dict[str, Artifact]),
        "partitions": provider_field(list[PartitionInfo], default = []),
    },
)
