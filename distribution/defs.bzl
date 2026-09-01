"""Declaring a distribution, and pointing a target at one.

A distribution is one value of the `tine//distribution:distribution` constraint plus the incoming
transition that selects it. What a value *means* is not tine's business: a catalog declares one per
release it offers, and a consumer maps values to package managers with an ordinary `select()`, so
this cell never has to know which distributions exist.

The transition sets the constraint only where nothing has set it yet. The target being built is
therefore the one that decides, and every image below it, `parent` chain included, follows: a base
layer is not a distribution, it is whatever the leaf pulling it in is.
"""

load("@prelude//:rules.bzl", "constraint_value")

def _distribution_impl(ctx: AnalysisContext) -> list[Provider]:
    value = ctx.attrs.constraint[ConstraintValueInfo]

    def select_distribution(platform: PlatformInfo) -> PlatformInfo:
        constraints = platform.configuration.constraints
        if value.setting.label in constraints:
            return platform
        constraints[value.setting.label] = value
        return PlatformInfo(
            label = platform.label,
            configuration = ConfigurationInfo(
                constraints = constraints,
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
                constraints = ctx.attrs.base[PlatformInfo].configuration.constraints | {value.setting.label: value},
                values = ctx.attrs.base[PlatformInfo].configuration.values,
            ),
        ),
    ]

_distribution = rule(
    impl = _distribution_impl,
    attrs = {
        "base": attrs.dep(providers = [PlatformInfo], default = "tine//platforms:default"),
        "constraint": attrs.dep(providers = [ConstraintValueInfo]),
    },
    is_configuration_rule = True,
)

def new(name: str, visibility: list[str] | None = None) -> None:
    """Declare a distribution and the constraint value `select()` keys on."""
    if not name.endswith(".distribution"):
        fail("distribution name must end with '.distribution': {}".format(name))
    constraint_value(
        name = name + ".constraint",
        constraint_setting = "tine//distribution:distribution",
        visibility = visibility,
    )
    _distribution(
        name = name,
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
    being written out once per distribution. The alias itself stays compatible with anything: it is
    what chooses, so requiring a choice of it would be circular.
    """
    _distribution_alias(name = name, actual = actual, incoming_transition = distro, visibility = visibility)

distribution = struct(
    alias = alias,
    aliases = aliases,
    attrs = attributes,
    compatibility = compatibility,
    distributed = distributed,
    for_package = for_package,
    new = new,
    set_for_package = set_for_package,
)
