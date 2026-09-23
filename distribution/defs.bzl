# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Declaring a distribution, and pointing a target at one.

A distribution is one value of the `tine//distribution:distribution` constraint plus the incoming
transition that selects it. What a value *means* is not tine's business: a catalog declares one per
release it offers, and a consumer maps values to package managers with an ordinary `select()`, so
this cell never has to know which distributions exist.

The transition sets the constraint only where nothing has set it yet. The target being built is
therefore the one that decides, and every image below it, `parent` chain included, follows: a base
layer is not a distribution, it is whatever the leaf pulling it in is.

A distribution also says which architectures its catalog serves it for. Choosing it marks the
configuration `tine//platforms:architecture-served` only for one of those, and every image requires that
mark beside its distribution, so under any other architecture the image is skipped rather than failing
somewhere under it.
"""

load("@prelude//:rules.bzl", "constraint_value")
load("//platforms:architecture.bzl", "architecture")

def _satisfies(constraints: dict[TargetLabel, ConstraintValueInfo], configuration: ConfigurationInfo) -> bool:
    """Whether a platform's constraints carry everything a `select()` key asks for."""
    for setting, wanted in configuration.constraints.items():
        if setting not in constraints or constraints[setting].label != wanted.label:
            return False
    return True

def _distribution_impl(ctx: AnalysisContext) -> list[Provider]:
    value = ctx.attrs.constraint[ConstraintValueInfo]
    served = ctx.attrs.served[ConstraintValueInfo]
    architectures = [dep[ConfigurationInfo] for dep in ctx.attrs.architectures]

    def choose(constraints: dict[TargetLabel, ConstraintValueInfo]) -> dict[TargetLabel, ConstraintValueInfo]:
        chosen = dict(constraints)
        chosen[value.setting.label] = value
        if [arch for arch in architectures if _satisfies(constraints, arch)]:
            chosen[served.setting.label] = served
        return chosen

    def select_distribution(platform: PlatformInfo) -> PlatformInfo:
        constraints = platform.configuration.constraints
        if value.setting.label in constraints:
            return platform
        return PlatformInfo(
            label = platform.label,
            configuration = ConfigurationInfo(
                constraints = choose(constraints),
                values = platform.configuration.values,
            ),
        )

    # One target answers every question asked of a distribution: it is the key a `select()`
    # branches on, the transition that puts it there, and a platform to build against. The
    # configuration stays the constraint alone, so a `select()` on it matches whatever else the
    # platform carries; the platform adds it to the base one, so choosing a distribution does not
    # drop the cpu and os the base platform brought.
    return [
        DefaultInfo(),
        ConfigurationInfo(constraints = {value.setting.label: value}, values = {}),
        TransitionInfo(impl = select_distribution),
        PlatformInfo(
            label = str(ctx.label.raw_target()),
            configuration = ConfigurationInfo(
                constraints = choose(ctx.attrs.base[PlatformInfo].configuration.constraints),
                values = ctx.attrs.base[PlatformInfo].configuration.values,
            ),
        ),
    ]

_distribution = rule(
    impl = _distribution_impl,
    attrs = {
        "architectures": attrs.list(attrs.dep(providers = [ConfigurationInfo]), doc = "the cpu configurations this is served for"),
        "base": attrs.dep(providers = [PlatformInfo], default = "tine//platforms:default"),
        "constraint": attrs.dep(providers = [ConstraintValueInfo]),
        "served": attrs.dep(providers = [ConstraintValueInfo], default = "tine//platforms:architecture-served"),
    },
    is_configuration_rule = True,
)

def new(name: str, architectures: list[str], visibility: list[str] | None = None) -> None:
    """Declare a distribution and the constraint value `select()` keys on, served for `architectures`."""
    if not name.endswith(".distribution"):
        fail("distribution name must end with '.distribution': {}".format(name))
    if not architectures:
        fail("distribution {} is served for no architecture".format(name))
    constraint_value(
        name = name + ".constraint",
        constraint_setting = "tine//distribution:distribution",
        visibility = visibility,
    )
    _distribution(
        name = name,
        architectures = [architecture.spelling(arch, "config") for arch in architectures],
        constraint = ":" + name + ".constraint",
        visibility = visibility,
    )

_DISTRIBUTIONS = "tine.distributions"

def set_for_package(distributions: dict[str, dict[str, str]]) -> None:
    """Declare, for one package and everything under it, which distributions its images serve.

    Each entry names one distribution and describes it. Only the `distribution` key, the label of
    the distribution target, is tine's business; the rest is whatever the package needs to know per
    distribution, such as which package manager to solve with, and is read back with
    `distribution.for_package()`. Declaring them here rather than in a BUCK file is what leaves one
    place to add a distribution.

    Every image rule then defaults its `target_compatible_with` to these, so a build that has chosen
    one of them resolves, `//...` skips the rest, and naming a target directly without choosing
    fails. Saying it once per package is the point: repeating it per target is how one target ends
    up forgetting, becoming a target that is itself fine but depends on something incompatible,
    which buck reports as an error rather than a skip.
    """
    for name, described in distributions.items():
        if "distribution" not in described:
            fail("distribution {} does not name a distribution target: {}".format(name, described))
    write_package_value(_DISTRIBUTIONS, distributions, overwrite = True)

def for_package() -> dict[str, dict[str, str]]:
    """What this package declared, keyed by the name it calls each distribution."""
    return read_package_value(_DISTRIBUTIONS) or {}

# `distribution.select`; a def of that name would shadow the builtin it wraps.
def by_distribution(values: dict[str, typing.Any], default = []):
    """A `select()` over the package's distributions, `default` where one has no entry.

    Keyed by the names the PACKAGE file gives them rather than by distribution labels, so a package
    list or a target that differs per distribution says which by the name the package already uses.
    """
    declared = for_package()
    for name in values:
        if name not in declared:
            fail("unknown distribution {!r}; the PACKAGE file declares {}".format(name, sorted(declared)))
    return select({described["distribution"]: values.get(name, default) for name, described in declared.items()})

_UNCHOSEN = "tine//distribution:no-distribution-chosen"
_SERVED = "tine//platforms:architecture-served"

def compatibility():
    """Say a target is buildable only where one of this package's distributions was chosen, and serves it.

    `target_compatible_with` requires every constraint in its list, so "one of these" is a select
    that asks under each distribution for the mark choosing it leaves where it is served, and for the
    unsatisfiable value under anything else. A package that declared no distributions constrains
    nothing. Rules tine owns get this through their macros; a rule it does not own, such as a prelude
    one over an image, asks here.
    """
    declared = for_package()
    if not declared:
        return []
    targets = [described["distribution"] for described in declared.values()]
    return select({target: [_SERVED] for target in targets} | {"DEFAULT": [_UNCHOSEN]})

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
    compatible_with = compatibility()
    if compatible_with:
        result["target_compatible_with"] = compatible_with
    return result

def aliases(name: str, distro: str | None = None, visibility: list[str] | None = None) -> None:
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
        )

def distributed(
    name: str,
    distro: str | None = None,
    visibility: list[str] | None = None,
) -> dict:
    """Declare a target's per-distribution aliases and return the attributes it takes.

    These are one decision rather than two. A target given the attributes without the aliases is
    compatible only with a choice nothing can make of it: it disappears from `//...` and fails when
    something finally names it. One that nothing names directly, because it is only ever a
    dependency, takes `distribution.attrs()` on its own instead.
    """
    aliases(name, distro, visibility)
    return attributes(distro, visibility)

def _distribution_alias_impl(ctx: AnalysisContext) -> list[Provider]:
    return ctx.attrs.actual.providers

_distribution_alias = rule(
    impl = _distribution_alias_impl,
    attrs = {"actual": attrs.dep()},
    supports_incoming_transition = True,
)

def alias(name: str, actual: str, distro: str, visibility: list[str] | None = None) -> None:
    """The same target, built for another distribution.

    Everything below it is reconfigured, so one declaration serves every distribution rather than
    being written out once per distribution. The alias needs no choice of its own, it is what
    chooses; it does need the choice to be served for the architecture, so that it is skipped where
    its target is, rather than reported as depending on something incompatible.
    """
    _distribution_alias(name = name, actual = actual, incoming_transition = distro, target_compatible_with = [_SERVED], visibility = visibility)

distribution = struct(
    alias = alias,
    aliases = aliases,
    attrs = attributes,
    select = by_distribution,
    compatibility = compatibility,
    distributed = distributed,
    for_package = for_package,
    new = new,
    set_for_package = set_for_package,
)
