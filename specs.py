"""Load the JSON spec that carries a driver's inputs, outputs, and configuration.

Rules write one spec per action instead of assembling a command line, so a driver's
interface is a schema its rule owns rather than a set of flags, orders, and separators.
"""

import argparse
import json
from pathlib import Path
from typing import Any, Protocol, cast


class Shape[T](Protocol):
    """A driver's TypedDict: the keys it declares, and what it builds.

    A TypedDict class is not a real class, so it cannot be taken as a `type[T]`; being callable
    is one of the few things the typing spec does promise about it.
    """

    __required_keys__: frozenset[str]
    __optional_keys__: frozenset[str]

    # never actually called, just for the type checker
    def __call__(self, *args: Any, **kwargs: Any) -> T: ...  # noqa: ANN401


def add_argument(parser: argparse.ArgumentParser) -> None:
    """Declare the driver half of the spec contract."""
    parser.add_argument("--spec", required=True, help="JSON spec describing this invocation")


def load[T](shape: Shape[T], path: str, *, prog: str) -> T:
    """Read the spec the calling rule wrote for one invocation.

    The rule owns the schema and Starlark has already typed it. Required and optional keys are validated
    so that the rule ←→ driver API can be type checked, and load() callers get a complete type.
    """
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"{prog}: {path} is not a JSON object"
    missing = shape.__required_keys__ - value.keys()
    assert not missing, f"{prog}: {path} is missing key(s) {', '.join(sorted(missing))}"
    unknown = value.keys() - shape.__required_keys__ - shape.__optional_keys__
    assert not unknown, f"{prog}: {path} has unknown key(s) {', '.join(sorted(unknown))}"
    return cast(T, value)


def parse[T](shape: Shape[T], prog: str, argv: list[str] | None = None) -> T:
    """Parse an invocation that consists of a spec alone."""
    parser = argparse.ArgumentParser(prog=prog)
    add_argument(parser)
    return load(shape, parser.parse_args(argv).spec, prog=prog)


def write(path: Path, spec: dict[str, Any]) -> Path:
    """Write a spec for a driver this driver invokes in turn."""
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path
