# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The CPU architectures tine builds for, and how each consumer spells them.

The canonical names are systemd's `ConditionArchitecture=` spellings, but with `_` instead of `-` (same
as in mkosi). Everything with its own naming schema reads it from this table:

- `config`: the Buck2 prelude's constraint value, which `select()` keys on
- `efi`: ukify's `--efi-arch`, which also names the boot stubs and systemd-boot
- `systemd`: the `%a` specifier, which names published artifacts
- `rpm`, `pacman`: the package system's own name, in its metadata and its mirrors' layout
"""

ARCHITECTURES = {
    "arm64": struct(
        config = "config//cpu:arm64",
        efi = "aa64",
        pacman = "aarch64",
        rpm = "aarch64",
        systemd = "arm64",
    ),
    "x86_64": struct(
        config = "config//cpu:x86_64",
        efi = "x64",
        pacman = "x86_64",
        rpm = "x86_64",
        systemd = "x86-64",
    ),
}

def _check(names: list[str]) -> None:
    unknown = sorted([name for name in names if name not in ARCHITECTURES])
    if unknown:
        fail("architecture: unknown {}; tine builds for {}".format(unknown, sorted(ARCHITECTURES)))

def _select(values: dict[str, typing.Any]) -> Select:
    """select() the value declared for the configured architecture."""
    _check(values.keys())
    return select({ARCHITECTURES[name].config: value for name, value in values.items()})

def _spelling(name: str, schema: str) -> str:
    """How `schema`, a field of the table, spells the architecture `name`."""
    _check([name])
    spelled = getattr(ARCHITECTURES[name], schema)
    if spelled == None:
        fail("architecture: {} has no {} name".format(name, schema))
    return spelled

# What supplies this, and why requiring it skips a target; see platforms/BUCK
_SERVED = "tine//platforms:architecture-served"

def _compatibility(names: list[str]) -> Select:
    """`target_compatible_with` value for a target that exists on `names` only.

    `//...` skips it under any other architecture, and naming it directly reports why. Every target
    depending on it has to declare the same value, as Buck reports a compatible target depending on an
    incompatible one as an error, not a skip.
    """
    _check(names)
    return select({ARCHITECTURES[name].config: [] for name in names} | {"DEFAULT": [_SERVED]})

def _configured() -> Select:
    """A rule's attribute default for the architecture being built for.

    Under the target configuration (plain `dep`) this is the host's, unless `--target-platforms` picks
    another. Under the execution configuration (`exec_dep`), it is always the host's.
    """
    return _select({name: name for name in ARCHITECTURES})

architecture = struct(
    compatibility = _compatibility,
    configured = _configured,
    select = _select,
    spelling = _spelling,
)
