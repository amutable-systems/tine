"""Providers for systemd system-extension DDI outputs."""

SysextImageInfo = provider(
    doc = "A systemd system-extension DDI generated from a logical image.",
    fields = {
        "engine": provider_field(Dependency),
        "extension": provider_field(str),
        "image": provider_field(Artifact),
        "source": provider_field(Dependency),
    },
)
