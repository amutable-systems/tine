#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Find the Go module a project's sources hold, ahead of the actions that build it.

A project is checked out, not written by us, so it carries no build file pointing at its own root;
the outermost go.mod marks it, unless the go_package's module_root names another. A tree that
arrives as a directory artifact only says where that sits once it has been built, so this runs as an
action and the fetch and the build are declared from what it reports. It also reports the go.mod of
each module that the project replaces with a directory. The fetch reads those go.mod files as well.
For a go_package whose toolchain is a go.releases target, it reports the Go release to build with:
the release that the go.releases target declares for the minor version in the project's .go-version.
"""

import json
import os
import re
from pathlib import Path
from typing import TypedDict

import goversion
import specs
from util import fail, named_files

_MODULE = "go.mod"
_SUM = "go.sum"
_GO_VERSION = ".go-version"
# The error for a .go-version that names no release prints its content.
_GO_VERSION_LIMIT = 256

# goenv and actions/setup-go read a .go-version that contains a release such as 1.25.3. go.dev's
# index spells that release go1.25.3, and some projects write the prefix too.
_RELEASE = re.compile(r"(?:go)?([0-9]+)\.([0-9]+)(?:\.([0-9]+)|(?:rc|beta)[0-9]+)?")

# go starts a comment at `//` only where a token starts, so a path such as `./a//b` contains no
# comment.
_COMMENT = re.compile(r"(?:^|\s)//")


class Spec(TypedDict):
    # The committed snapshot of the go_package's go.releases toolchain, or None when the toolchain is
    # something else.
    snapshot: str | None
    # The directory of the go.mod to build, relative to src. Unset selects the outermost go.mod.
    module_root: str | None
    # The target, named in whatever this refuses.
    name: str
    # Where to write the resolved workspace.
    out: str
    # The project's source directory artifact.
    src: str


class Workspace(TypedDict):
    mod: str
    # The fetch copies `mod` and the go.mod of each module that `mod` replaces with a directory in
    # src.
    modules: list[str]
    root: str
    sum: str | None


def local_replacements(text: str) -> list[str]:
    """Return the directories that a go.mod's replace directives point modules at."""
    directories: list[str] = []
    for line in text.splitlines():
        # In a go.mod, only a replace directive contains `=>`. This holds for a single-line replace
        # directive and for each line of a replace block. go reads the right side of `=>` as a
        # directory when it has no version and starts with `./`, `../` or `/`.
        _, arrow, new = _COMMENT.split(line, maxsplit=1)[0].partition("=>")
        new = new.strip()
        if new[:1] in ('"', "`"):
            if len(new) < 2 or new[-1] != new[0]:
                continue
            new = new[1:-1]
        elif len(new.split()) != 1:
            continue
        if arrow and (new == ".." or new.startswith(("./", "../", "/"))):
            directories.append(new)
    return directories


def resolve_workspace(target: str, source: Path, module_root: str | None = None) -> Workspace:
    """Find the module root, its pins, and the go.mod files the fetch reads, relative to `source`."""
    if not source.is_dir():
        fail(f"go_package {target}: src must be a directory")

    modules = named_files(source, _MODULE)
    if not modules:
        fail(
            f"go_package {target}: src holds no {_MODULE}; by default the checkout is expected in "
            f"the {target}/ directory, pass `src` when it lives elsewhere"
        )

    if module_root is not None:
        # Path("") equals Path("."), so an empty module_root would select the module at the root of
        # src. go_package refuses the empty string at analysis time, and resolve_workspace refuses it
        # as well.
        if not module_root:
            fail(f"go_package {target}: module_root must not be empty")
        root = Path(module_root)
        module = root / _MODULE
        # `modules` contains only relative paths inside src. An absolute module_root, or a
        # module_root with `..`, is therefore never in `modules` and is refused. named_files does not
        # descend into a symlink to a directory. A module_root whose path contains such a symlink is
        # refused for the same reason.
        if module not in modules:
            fail(
                f"go_package {target}: module_root {module_root!r} is not a directory in src that "
                f"contains a {_MODULE}; src contains {[str(path) for path in modules]}"
            )
    else:
        # A second go.mod belongs to a module nested in the project, a tools or testdata helper,
        # which go leaves out of a `./...` build itself. The outermost one is the default.
        module = min(modules, key=lambda path: len(path.parts))
        root = module.parent
        strays = [str(other) for other in modules if other != module and root not in other.parents]
        if strays:
            fail(
                f"go_package {target}: {strays} is not nested in {module}, so src holds no single "
                "project; set `module_root` to select a module"
            )

    # go applies only the replace directives of the module it builds, and reads the go.mod in each
    # directory they name. go reads that go.mod only when the build needs the replaced module, so
    # a directory in src without a go.mod is left for go to report.
    replacements = []
    for directory in local_replacements((source / module).read_text(encoding="utf-8")):
        # go joins the directory to the module root without resolving symlinks. Path.resolve()
        # would resolve them, so os.path.normpath does the join.
        path = Path(os.path.normpath(root / directory))
        # The build runs in the source directory in place. A replacement outside src would make it
        # read files that are not among the action's inputs.
        if path.is_absolute() or path.parts[:1] == ("..",):
            fail(f"go_package {target}: {module} replaces a module with {directory}, which is outside src")
        replacements.append(path / _MODULE)

    # A module that resolves nothing has nothing to pin, and then nothing is fetched either.
    pinned = root / _SUM
    pins = pinned if pinned in named_files(source, _SUM) else None
    staged = {module}
    if pins is not None:
        staged.update(path for path in replacements if path in modules)

    return {
        "mod": str(module),
        "modules": sorted(str(path) for path in staged),
        "root": "" if root == Path() else str(root),
        "sum": None if pins is None else str(pins),
    }


def snapshot_releases(target: str, snapshot: Path) -> dict[str, str]:
    """Return the release of each minor version in a go.releases snapshot, keyed by the minor version."""
    releases: dict[str, str] = {}
    for release in json.loads(snapshot.read_text(encoding="utf-8")):
        parsed = goversion.patch_release(release)
        if parsed is None:
            fail(
                f"go_package {target}: its go.releases snapshot names {release!r}, "
                "not a release such as go1.26.10"
            )
        minor = f"{parsed[0]}.{parsed[1]}"
        if minor in releases:
            fail(
                f"go_package {target}: its go.releases snapshot names {releases[minor]} and {release}, two "
                f"releases of Go {minor}"
            )
        releases[minor] = release
    return releases


def go_version(target: str, source: Path, root: str, releases: dict[str, str]) -> str:
    """Return the release in `releases` of the minor version that the nearest .go-version names.

    The search starts at the module root and goes up to the root of src. etcd builds the module in
    server/ and keeps its .go-version at the root of the checkout.
    """
    module = Path(root)
    for directory in [module, *module.parents]:
        path = source / directory / _GO_VERSION
        # The workspace action runs outside the build sandbox. A .go-version in a fetched checkout
        # that is a symlink to a host file would make the error below print that file.
        if path.is_symlink():
            fail(f"go_package {target}: {directory / _GO_VERSION} is a symlink")
        if not path.is_file():
            continue
        with path.open("rb") as file:
            content = file.read(_GO_VERSION_LIMIT + 1)
        if len(content) > _GO_VERSION_LIMIT:
            fail(f"go_package {target}: {directory / _GO_VERSION} is larger than {_GO_VERSION_LIMIT} bytes")
        text = content.decode("utf-8", errors="replace").strip()
        match = _RELEASE.fullmatch(text)
        if match is None:
            fail(
                f"go_package {target}: {directory / _GO_VERSION} names {text!r}, "
                "not a Go release such as 1.25.3"
            )
        named = "go" + text.removeprefix("go")
        major, minor, patch = match.groups()
        release = releases.get(f"{major}.{minor}")
        if release is None:
            fail(
                f"go_package {target}: {directory / _GO_VERSION} names {named}, and its go.releases "
                f"toolchain contains no Go {major}.{minor} release"
            )
        # Go publishes a patch release only to fix critical problems, such as security problems. A
        # project that names an older patch release builds with the newer one that the go.releases
        # target declares, so that its binaries contain those fixes. A .go-version without a patch
        # version, such as 1.26 or 1.26rc1, names no release newer than a declared patch release.
        # snapshot_releases() accepts only patch releases, such as go1.26.10. The text after the last
        # dot of a release is therefore its patch number.
        if patch is not None and int(patch) > int(release.rpartition(".")[2]):
            fail(
                f"go_package {target}: {directory / _GO_VERSION} names {named}, which is newer than "
                f"{release} that its go.releases toolchain declares; advance the go.releases target "
                "with `tine buck run tine//tools:refresh-catalog -- --go --advance`"
            )
        return release
    fail(
        f"go_package {target}: its toolchain is a go.releases target, and src contains no "
        f"{_GO_VERSION} in {module} or above"
    )


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "go-workspace", argv)
    source = Path(spec["src"])
    workspace = resolve_workspace(spec["name"], source, spec["module_root"])
    snapshot = spec["snapshot"]
    release = None
    if snapshot is not None:
        releases = snapshot_releases(spec["name"], Path(snapshot))
        release = go_version(spec["name"], source, workspace["root"], releases)
    output = {**workspace, "go_version": release}
    Path(spec["out"]).write_text(json.dumps(output, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
