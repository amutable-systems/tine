# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Declaring a distribution, and pointing a target at one.

A distribution is one value of the `tine//distribution:distribution` constraint, the value of its
`tine//distribution:family` constraint, and the incoming transition that sets the two constraints.
The catalog declares one distribution per release in its root package, so the release name
`fedora.rawhide` names the label `catalog//:fedora.rawhide.distribution`.

The transition sets the constraint only where nothing has set it yet. The target being built is
therefore the one that decides, and every image below it, `parent` chain included, follows: a base
layer is not a distribution, it is whatever the leaf pulling it in is. For an architecture the
distribution does not serve, the transition leaves the constraint unset.
"""

load("@prelude//:rules.bzl", "constraint_value")
load("@prelude//cfg/exec_platform:marker.bzl", "get_exec_platform_marker")
load("//platforms:architecture.bzl", "ARCHITECTURES", "architecture")
load("//platforms:defs.bzl", "check_unmarked")

# The families that tine declares releases for. Each one is a value of `tine//distribution:family`.
FAMILIES = ["arch", "debian", "fedora"]

def _distribution_impl(ctx: AnalysisContext) -> list[Provider]:
    value = ctx.attrs.constraint[ConstraintValueInfo]
    family_value = ctx.attrs.family[ConstraintValueInfo]
    own = {value.setting.label: value, family_value.setting.label: family_value}
    served = [cpu.label for configuration in ctx.attrs.architectures for cpu in configuration[ConfigurationInfo].constraints.values()]

    def served_on(constraints: dict[TargetLabel, ConstraintValueInfo]) -> bool:
        return bool([cpu for cpu in constraints.values() if cpu.label in served])

    def select_distribution(platform: PlatformInfo) -> PlatformInfo:
        constraints = platform.configuration.constraints

        # A distribution has no packages for an architecture it does not serve. With the
        # distribution unset, `compatibility()` makes every target under the transition incompatible.
        # A build of `//...` then skips such a target instead of failing on the missing packages.
        if value.setting.label in constraints or not served_on(constraints):
            return platform
        constraints.update(own)
        return PlatformInfo(
            label = platform.label,
            configuration = ConfigurationInfo(
                constraints = constraints,
                values = platform.configuration.values,
            ),
        )

    # One target answers every question asked of a distribution: it is the key a `select()`
    # branches on, the transition that puts it there, and a platform to build against.
    # The configuration contains only the distribution and family constraints. A `select()` key of
    # the distribution therefore matches whatever else the platform contains. The key also refines the
    # key of its family, so Buck picks the distribution's branch when a `select()` has a key for the
    # distribution and a key for its family.
    # The platform adds the distribution and family constraints to the base platform. Choosing a
    # distribution therefore keeps the cpu and os of the base platform. A base cpu the distribution
    # does not serve gets neither constraint, as under the transition.
    base = ctx.attrs.base[PlatformInfo].configuration
    return [
        DefaultInfo(),
        ConfigurationInfo(constraints = own, values = {}),
        TransitionInfo(impl = select_distribution),
        PlatformInfo(
            label = str(ctx.label.raw_target()),
            configuration = ConfigurationInfo(
                constraints = base.constraints | (own if served_on(base.constraints) else {}),
                values = base.values,
            ),
        ),
    ]

_distribution = rule(
    impl = _distribution_impl,
    attrs = {
        "architectures": attrs.list(attrs.dep(providers = [ConfigurationInfo]), doc = "the cpu configuration of each architecture the distribution serves"),
        "base": attrs.dep(providers = [PlatformInfo], default = "tine//platforms:default"),
        "constraint": attrs.dep(providers = [ConstraintValueInfo]),
        "family": attrs.dep(providers = [ConstraintValueInfo]),
    },
    is_configuration_rule = True,
)

def _box_platform_impl(ctx: AnalysisContext) -> list[Provider]:
    base = ctx.attrs.base[PlatformInfo].configuration
    box = PlatformInfo(
        label = str(ctx.label.raw_target()),
        configuration = ConfigurationInfo(
            constraints = base.constraints | ctx.attrs.distribution[ConfigurationInfo].constraints,
            values = base.values,
        ),
    )
    marker = ctx.attrs._exec_marker[ConstraintValueInfo]

    def configure(platform: PlatformInfo) -> PlatformInfo:
        check_unmarked(platform, marker)
        return box

    return [DefaultInfo(), box, TransitionInfo(impl = configure)]

# A box takes a `_box_platform` target as its incoming transition. The transition replaces the
# caller's configuration, so every caller reaches the same configured box. A `select()` in the
# box's attributes then sees the distribution of the box's release.
# The execution platform registers no exec marker constraint, so the execution configuration is the
# same as the host's. An `exec_dep` therefore reaches the same configured box as an ordinary
# dependency. The host platform assumes one execution platform, for the host's architecture. A
# project can register an execution platform for another architecture. An `exec_dep` resolved for
# that platform still reaches the box built for the host. The actions on that platform then run
# binaries for the wrong architecture, and Buck reports no error.
_box_platform = rule(
    impl = _box_platform_impl,
    attrs = {
        "base": attrs.dep(providers = [PlatformInfo]),
        "distribution": attrs.dep(providers = [ConfigurationInfo]),
        "_exec_marker": attrs.dep(providers = [ConstraintValueInfo], default = get_exec_platform_marker()),
    },
    is_configuration_rule = True,
)

def new(name: str, architectures: list[str], visibility: list[str] | None = None) -> None:
    """Declare a distribution of `architectures` in the family that its name starts with, and the constraint value `select()` keys on.

    A box of the distribution's release is configured for `<name>.host`, the host platform with the
    distribution's constraints. A lock of the box for an architecture is configured for
    `<name>.<architecture>`.
    """
    if not name.endswith(".distribution"):
        fail("distribution name must end with '.distribution': {}".format(name))
    family = name.partition(".")[0]
    if family not in FAMILIES:
        fail("distribution {}: unknown family {!r}; tine knows {}".format(name, family, FAMILIES))
    # `distribution.select()` and `distribution.set_for_package()` turn a release name such as
    # `fedora.43` into the label `catalog//:fedora.43.distribution`. A release name can't refer to a
    # distribution declared anywhere else.
    if get_cell_name() != "catalog" or package_name() != "":
        fail("distribution {}: declare it in the root package of the catalog cell, not in {}//{}".format(name, get_cell_name(), package_name()))
    constraint_value(
        name = name + ".constraint",
        constraint_setting = "tine//distribution:distribution",
        visibility = visibility,
    )
    configurations = [architecture.spelling(arch, "config") for arch in architectures]
    _distribution(
        name = name,
        # The release's box is built for the host and exists only for `architectures`. A host of
        # another architecture has no box to install the release's packages with, so the distribution
        # serves no architecture there.
        architectures = configurations if architecture.host() in architectures else [],
        constraint = ":" + name + ".constraint",
        family = "tine//distribution:" + family,
        visibility = visibility,
    )
    _box_platform(
        name = name + ".host",
        base = "tine//platforms:default",
        distribution = ":" + name,
        visibility = visibility,
    )
    for arch in architectures:
        _box_platform(
            name = "{}.{}".format(name, arch),
            base = "tine//platforms:" + arch,
            distribution = ":" + name,
            visibility = visibility,
        )

def default_release(release: str, visibility: list[str] = ["PUBLIC"]) -> None:
    """Declare a catalog's `default.*` targets as aliases of the targets of `release`, such as `fedora.rawhide`."""

    # box.new() derives the resolver box and the distribution's platforms from the release's name, so
    # every derived target needs an alias.
    for role in ["release", "box", "distribution", "distribution.host"]:
        native.alias(
            name = "default." + role,
            actual = ":{}.{}".format(release, role),
            visibility = visibility,
        )

_DISTRIBUTIONS = "tine.distributions"

def _release(release: str, role: str) -> str:
    """Return the label of the target `role` of a catalog release, such as `fedora.rawhide`."""
    family, _, version = release.partition(".")
    if family not in FAMILIES or not version:
        fail("{!r} is not a release; a release is named `<family>.<version>`, and the families are {}".format(release, FAMILIES))
    return "catalog//:{}.{}".format(release, role)

def _describe(name: str, described: dict[str, str]) -> dict[str, str]:
    if "release" not in described:
        fail("distribution {} names no release of the catalog, such as `fedora.rawhide`: {}".format(name, described))
    if "distribution" in described:
        fail("distribution {}: the release names the distribution, so `distribution` cannot be set: {}".format(name, described))
    release = described["release"]
    return {
        "distribution": _release(release, "distribution"),
        "package_manager": _release(release, "package-manager"),
    } | described

def set_for_package(distributions: dict[str, dict[str, str]]) -> None:
    """Declare, for one package and everything under it, which distributions its images serve.

    Each entry names one distribution of the package. Its `release` key names a release of the
    catalog, such as `fedora.rawhide`. Its `package_manager` key defaults to the release's package
    manager. The rest is whatever the package needs to know per distribution, and is read back with
    `distribution.for_package()`, which also returns the `distribution` label of the release.

    Every image rule then defaults its `target_compatible_with` to these, so a build that has chosen
    one of them resolves, `//...` skips the rest, and naming a target directly without choosing
    fails. Saying it once per package is the point: repeating it per target is how one target ends
    up forgetting, becoming a target that is itself fine but depends on something incompatible,
    which buck reports as an error rather than a skip.
    """
    write_package_value(
        _DISTRIBUTIONS,
        {name: _describe(name, described) for name, described in distributions.items()},
        overwrite = True,
    )

def for_package() -> dict[str, dict[str, str]]:
    """What this package declared, keyed by the name it calls each distribution."""
    return read_package_value(_DISTRIBUTIONS) or {}

def _condition(key: str) -> str:
    if key == "DEFAULT":
        return key
    if key in FAMILIES:
        return "tine//distribution:" + key
    return _release(key, "distribution")

# `distribution.select`; a def of that name would shadow the builtin it wraps.
def by_distribution(values: dict[str, typing.Any]) -> Select:
    """A `select()` keyed by family, such as `fedora`, by catalog release, such as `fedora.43`, or by `DEFAULT`.

    Buck picks a release's key over the key of its family. Buck fails to configure a target that
    matches no key.
    """
    return select({_condition(key): value for key, value in values.items()})

_UNCHOSEN = "tine//distribution:no-distribution-chosen"

def compatibility():
    """Say a target is buildable only where one of this package's distributions was chosen.

    `target_compatible_with` requires every constraint in its list, so "one of these" is a select
    that asks for nothing under each distribution and for the unsatisfiable value under anything
    else. A package that declared no distributions constrains nothing. Rules tine owns get this
    through their macros; a rule it does not own, such as a prelude one over an image, asks here.
    """
    declared = for_package()
    if not declared:
        return []
    targets = [described["distribution"] for described in declared.values()]
    return select({target: [] for target in targets} | {"DEFAULT": [_UNCHOSEN]})

def _chosen(distro: str) -> list[str] | Select:
    """`target_compatible_with` for a target whose incoming transition chooses `distro`.

    The transition leaves the distribution unset for an architecture that `distro` does not serve,
    and the target is then incompatible.
    """
    if for_package():
        return compatibility()
    return select({distro: [], "DEFAULT": [_UNCHOSEN]})

def attributes(distro: str | None = None, visibility: list[str] | None = None) -> dict:
    """Resolve what a package declares about distributions into the attributes rules take.

    Anything a build can name takes these through `distribution.distributed()`, which declares the aliases that
    make the compatibility satisfiable. This is on its own only for a target reached exclusively as
    a dependency, which is configured by whatever depends on it and never named directly.
    """
    result = {"visibility": visibility}
    if distro != None:
        result["incoming_transition"] = distro

    # A package that declares none leaves compatibility alone, which is every package that has not
    # opted into building for a distribution at all.
    compatible_with = compatibility() if distro == None else _chosen(distro)
    if compatible_with:
        result["target_compatible_with"] = compatible_with
    return result

def aliases(
    name: str,
    distro: str | None = None,
    visibility: list[str] | None = None,
    compatible_with: list[str] | Select = [],
) -> None:
    """Name one target once per distribution its package serves.

    A target that names no distribution of its own is buildable under every one of them, so the
    names that say which are declared beside it rather than listed again somewhere else. A target
    that does name one is already the distribution it is, and gets no aliases.
    """
    if distro != None:
        return
    declared = for_package()
    for suffix in sorted(declared):
        alias(
            name = "{}.{}".format(name, suffix),
            actual = ":" + name,
            distro = declared[suffix]["distribution"],
            visibility = visibility,
            compatible_with = compatible_with,
        )

def distributed(
    name: str,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
) -> dict:
    """Declare a target's per-distribution aliases and return the attributes it takes.

    These are one decision rather than two. A target given the attributes without the aliases is
    compatible only with a choice nothing can make of it: it disappears from `//...` and fails when
    something finally names it. One that nothing names directly, because it is only ever a
    dependency, takes `distribution.attrs()` on its own instead.

    `target_compatible_with` constrains the target and its aliases alike, on top of the
    distributions. Buck reports an alias that is compatible where its target is not as an error, and
    `//...` then fails instead of skipping both.
    """
    compatible_with = [] if target_compatible_with == None else target_compatible_with
    aliases(name, distro, visibility, compatible_with)
    result = attributes(distro, visibility)
    result["target_compatible_with"] = result.get("target_compatible_with", []) + compatible_with
    return result

def serves(name: str, architectures: list[str], in_box: bool = False) -> Select:
    """`target_compatible_with` for a target `name` that exists only for `architectures`, such as a release's repository.

    A target that runs in the release's box, such as its package manager, sets `in_box`. The release
    builds its box only for `architectures`, so such a target is also incompatible on a host whose
    architecture is not in `architectures`.
    """
    unserved = select_incompatible("{} serves {} only".format(name, ", ".join(architectures)))
    if in_box and architecture.host() not in architectures:
        return select({"DEFAULT": unserved})
    return architecture.select({arch: [] if arch in architectures else unserved for arch in ARCHITECTURES})

def _distribution_alias_impl(ctx: AnalysisContext) -> list[Provider]:
    return ctx.attrs.actual.providers

_distribution_alias = rule(
    impl = _distribution_alias_impl,
    attrs = {"actual": attrs.dep()},
    supports_incoming_transition = True,
)

def alias(
    name: str,
    actual: str,
    distro: str,
    visibility: list[str] | None = None,
    compatible_with: list[str] | Select = [],
) -> None:
    """The same target, built for another distribution.

    Everything below it is reconfigured, so one declaration serves every distribution rather than
    being written out once per distribution. The alias is incompatible with an architecture the
    distribution does not serve, and it takes the target's own `compatible_with`.
    """
    _distribution_alias(
        name = name,
        actual = actual,
        incoming_transition = distro,
        target_compatible_with = _chosen(distro) + compatible_with,
        visibility = visibility,
    )

distribution = struct(
    alias = alias,
    aliases = aliases,
    attrs = attributes,
    default_release = default_release,
    select = by_distribution,
    serves = serves,
    compatibility = compatibility,
    distributed = distributed,
    for_package = for_package,
    new = new,
    set_for_package = set_for_package,
)
