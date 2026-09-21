"""Tests for the planner against real repodata and a real solve.

Builds throwaway packages and indexes them with the same driver a build uses, thus exercises
libdnf5's actual resolution.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

import createrepo
import plan
import transaction

SPEC = """\
Name: {name}
Version: {version}
Release: 1
Summary: A package to solve for
License: MIT
BuildArch: noarch
{extra}
%description
%install
mkdir -p %{{buildroot}}/usr/share/{name}
touch %{{buildroot}}/usr/share/{name}/marker
%files
/usr/share/{name}
"""


class TestPlan(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls._scratch = tempfile.TemporaryDirectory()
        cls.scratch = Path(cls._scratch.name)
        cls.addClassCleanup(cls._scratch.cleanup)

    def build(self, name: str, version: str, extra: str = "") -> Path:
        spec = self.scratch / f"{name}-{version}.spec"
        spec.write_text(SPEC.format(name=name, version=version, extra=extra))
        subprocess.run(
            [
                "rpmbuild",
                "-bb",
                "--quiet",
                "--define",
                f"_topdir {self.scratch}/rpmbuild",
                "--define",
                "_buildhost tine",
                str(spec),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return self.scratch / f"rpmbuild/RPMS/noarch/{name}-{version}-1.noarch.rpm"

    def repository(self, name: str, *packages: Path) -> transaction.Repository:
        out = Path(tempfile.mkdtemp(dir=self.scratch)) / name
        createrepo.createrepo([(package.name, package) for package in packages], out, "0")
        # No base URL: these are packages this test produced, so the transaction names them by path.
        return transaction.Repository(name, out.absolute(), 99, None)

    def solve(self, install: list[str], *repos: transaction.Repository) -> list[str]:
        """The package ids for solving `install` against `repos`"""
        # separate cache directory, so one case's parsed metadata cannot answer for another's.
        cachedir = Path(tempfile.mkdtemp(dir=self.scratch)) / "cache"
        resolved = plan.plan(list(repos), install, cachedir, None, "x86_64", [])
        return sorted(package["package_id"] for package in resolved)

    def test_resolves_a_spec_to_the_package_it_names(self) -> None:
        repo = self.repository("plain", self.build("gadget", "1"))
        self.assertEqual(self.solve(["gadget"], repo), ["gadget-1-1.noarch"])

    def test_resolves_a_spec_naming_a_capability(self) -> None:
        repo = self.repository("capability", self.build("gadget", "1", "Provides: gizmo = 1"))
        self.assertEqual(self.solve(["gizmo"], repo), ["gadget-1-1.noarch"])

    def test_resolves_a_spec_naming_a_file(self) -> None:
        repo = self.repository("file", self.build("gadget", "1"))
        self.assertEqual(self.solve(["/usr/share/gadget/marker"], repo), ["gadget-1-1.noarch"])

    def test_refuses_a_spec_an_obsoleting_package_displaced(self) -> None:
        """split-out subpackage obsoleting its parent of older version is refused."""

        old = self.repository("old", self.build("widget", "1"))
        new = self.repository("new", self.build("widget-keys", "2", "Obsoletes: widget < 2"))
        with self.assertRaises(SystemExit) as failure:
            self.solve(["widget"], old, new)
        self.assertIn("does not install", str(failure.exception))
        self.assertIn("widget, obsoleted by widget-keys-2-1.noarch", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
