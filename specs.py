"""Load the JSON spec that carries a driver's inputs, outputs, and configuration.

Rules write one spec per action instead of assembling a command line, so a driver's
interface is a schema its rule owns rather than a set of flags, orders, and separators.
"""

import argparse
import json
from pathlib import Path
from typing import Any, cast


def add_argument(parser: argparse.ArgumentParser) -> None:
    """Declare the driver half of the spec contract."""
    parser.add_argument("--spec", required=True, help="JSON spec describing this invocation")


def load[T](path: str, *, prog: str) -> T:
    """Read the spec the calling rule wrote for one invocation.

    The rule owns the schema and Starlark has already typed it, so the driver takes the
    shape it declares instead of revalidating every field.
    """
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"{prog}: {path} is not a JSON object")
    return cast(T, value)


def parse[T](prog: str, argv: list[str] | None = None) -> T:
    """Parse an invocation that consists of a spec alone."""
    parser = argparse.ArgumentParser(prog=prog)
    add_argument(parser)
    return load(parser.parse_args(argv).spec, prog=prog)


def write(path: Path, spec: dict[str, Any]) -> Path:
    """Write a spec for a driver this driver invokes in turn."""
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path
