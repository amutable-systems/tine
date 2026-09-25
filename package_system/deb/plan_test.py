# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for APT EDSP capture and transaction mapping.

buck test tine//package_system/deb:test
"""

import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import edsp
import specs

import plan
import transaction

SHA256 = "0123456789abcdef" * 4
OTHER_SHA256 = "abcdef0123456789" * 4


def packages_record(*, digest: str = SHA256, version: str = "1.0-1", filename: str = "pool/demo.deb") -> str:
    return (
        "Package: demo\n"
        f"Version: {version}\n"
        "Architecture: amd64\n"
        f"Filename: {filename}\n"
        "Size: 42\n"
        f"SHA256: {digest}\n\n"
    )


def repository(
    root: Path, rid: str, priority: int, baseurl: str | None, content: str
) -> transaction.Repository:
    path = root / rid
    path.mkdir()
    (path / "Packages").write_text(content, encoding="utf-8")
    return transaction.Repository(rid, path, priority, baseurl)


class TestReadPackages(unittest.TestCase):
    def test_reads_transport_fields(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            repo = repository(Path(scratch), "main", 99, "https://example.invalid", packages_record())
            records = plan.read_packages(repo)
        record = records[edsp.PackageKey("demo", "1.0-1", "amd64")]
        self.assertEqual(record.filename, "pool/demo.deb")
        self.assertEqual(record.size, 42)
        self.assertEqual(record.sha256, SHA256)

    def test_rejects_conflicting_duplicate_identity(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            repo = repository(
                Path(scratch),
                "main",
                99,
                "https://example.invalid",
                packages_record() + packages_record(digest=OTHER_SHA256),
            )
            with self.assertRaises(SystemExit):
                plan.read_packages(repo)


class TestMain(unittest.TestCase):
    def test_accepts_the_shared_solve_spec_without_prebuilt_caches(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
            root = Path(scratch)
            spec = specs.write(
                root / "solve.spec.json",
                {
                    "arch": "amd64",
                    "cache": [],
                    "install": ["bash"],
                    "lower": ["lower"],
                    "repositories": [],
                },
            )
            out = root / "transaction.json"
            with mock.patch.object(plan, "solve", return_value=[]) as solve:
                plan.main(["solve", "--spec", str(spec), "--out", str(out)])

            solve.assert_called_once_with([], ["bash"], ["lower"], "amd64")
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), [])


class TestResolvedPackages(unittest.TestCase):
    def _edsp(self, root: Path, solution: str, label: str = "preferred") -> tuple[Path, Path]:
        scenario = root / "scenario"
        scenario.write_text(
            "Request: EDSP 0.5\nArchitecture: amd64\n\n"
            "Package: demo\nVersion: 1.0-1\nArchitecture: amd64\nAPT-ID: 17\n"
            f"APT-Release:\n o=Tine,a=tine,n={label},l={label},c=\nAPT-Pin: 1002\n\n",
            encoding="utf-8",
        )
        answer = root / "solution"
        answer.write_text(solution, encoding="utf-8")
        return scenario, answer

    def test_maps_install_to_the_repository_apt_read_it_from(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            preferred = repository(root, "preferred", 50, None, packages_record(filename="0/demo.deb"))
            fallback = repository(root, "fallback", 99, None, packages_record(digest=OTHER_SHA256))
            # The same identity in both, and APT names the one it is not staged first.
            scenario, solution = self._edsp(root, "Install: 17\n\n", label="fallback")
            result = plan.resolved_packages([preferred, fallback], scenario, solution)

        self.assertEqual(
            result,
            [
                {
                    "package_id": "demo_1.0-1_amd64",
                    "repo": "fallback",
                    "pkg_checksum": OTHER_SHA256,
                    "source": "local",
                    "location": "pool/demo.deb",
                }
            ],
        )

    def test_rejects_a_solution_naming_an_unstaged_repository(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            repo = repository(root, "main", 99, None, packages_record())
            scenario, solution = self._edsp(root, "Install: 17\n\n", label="elsewhere")
            with self.assertRaises(SystemExit) as failure:
                plan.resolved_packages([repo], scenario, solution)
        self.assertIn("not one of the pinned repositories", str(failure.exception))

    def test_rejects_an_install_the_front_end_made_and_the_solver_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            repo = repository(root, "preferred", 50, None, packages_record())
            # What apt-get prints for a downgrade, which its solver leaves out of the answer.
            scenario, solution = self._edsp(root, "\n")
            with self.assertRaises(SystemExit) as failure:
                plan.resolved_packages(
                    [repo], scenario, solution, "Inst demo [2.0] (1.0 preferred:tine [amd64])\n"
                )
        self.assertIn("which its solver did not report", str(failure.exception))

    def test_rejects_removals(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            repo = repository(root, "main", 99, "https://example.invalid", packages_record())
            scenario, solution = self._edsp(root, "Remove: 17\nPackage: demo\n\n")
            with self.assertRaises(SystemExit):
                plan.resolved_packages([repo], scenario, solution)

    def test_rejects_unknown_solution_id(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            repo = repository(root, "main", 99, "https://example.invalid", packages_record())
            scenario, solution = self._edsp(root, "Install: 99\n\n")
            with self.assertRaises(SystemExit):
                plan.resolved_packages([repo], scenario, solution)


class TestProxy(unittest.TestCase):
    def test_captures_both_sides_and_forwards_solution(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            solver = root / "solver"
            solver.write_text("#!/bin/sh\nsed 's/^Request:/Error:/'\n", encoding="utf-8")
            solver.chmod(0o755)
            scenario, solution = root / "scenario", root / "solution"
            output = io.BytesIO()
            status = edsp.proxy(io.BytesIO(b"Request: EDSP 0.5\n\n"), output, scenario, solution, solver)

            self.assertEqual(status, 0)
            self.assertEqual(scenario.read_bytes(), b"Request: EDSP 0.5\n\n")
            self.assertEqual(solution.read_bytes(), b"Error: EDSP 0.5\n\n")
            self.assertEqual(output.getvalue(), solution.read_bytes())


class TestIndexEntries(unittest.TestCase):
    def test_a_compressed_index_is_stated_under_both_names(self) -> None:
        plain = b"Package: demo\n\n"
        with tempfile.TemporaryDirectory() as scratch:
            index = Path(scratch) / "Packages.gz"
            index.write_bytes(gzip.compress(plain))
            entries = plan.index_entries(index)

            # APT keys the target on the uncompressed name, so a Release without it provides
            # nothing at all.
            self.assertEqual([name for _, _, name in entries], ["Packages", "Packages.gz"])
            self.assertEqual(entries[0][:2], (hashlib.sha256(plain).hexdigest(), len(plain)))
            self.assertEqual(entries[1][1], index.stat().st_size)

    def test_an_uncompressed_index_is_stated_once(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            index = Path(scratch) / "Packages"
            index.write_bytes(b"Package: demo\n\n")
            self.assertEqual([name for _, _, name in plan.index_entries(index)], ["Packages"])


class TestConfig(unittest.TestCase):
    def test_disowns_the_boxes_own_configuration_before_apt_reads_it(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            written = edsp.config(root).read_text(encoding="utf-8")
            options = edsp.options(
                root, root / "sources.list", root / "preferences", root / "status", "amd64"
            )

            self.assertIn('Dir::Etc::main "/dev/null";', written)
            self.assertIn(f'Dir::Etc::parts "{root / "nothing.d"}";', written)
            self.assertTrue((root / "nothing.d").is_dir())

            # APT applies these while still loading configuration, so a `-o` comes too late.
            self.assertNotIn("Dir::Etc::main", " ".join(options))
            self.assertNotIn("Dir::Etc::parts", " ".join(options))

            # Nothing a solve writes lands outside its own scratch, the planner's log included.
            self.assertIn(f"Dir::Log={root / 'log'}", options)
            self.assertTrue((root / "log").is_dir())


class TestWriteSolver(unittest.TestCase):
    def test_apt_can_exec_it_and_it_re_enters_this_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            driver = root / "driver.py"
            driver.write_text("import sys\nprint(sys.executable)\n", encoding="utf-8")
            solver = root / edsp.SOLVER_NAME
            plan.write_solver(solver, driver)

            # What APT does with Dir::Bin::Solvers: exec the file, with no shell to fall back on.
            self.assertTrue(os.access(solver, os.X_OK))
            result = subprocess.run([solver], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), sys.executable)


class TestStageRepositories(unittest.TestCase):
    def test_labels_sources_and_translates_lower_priorities_to_higher_pins(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            preferred = repository(root, "local.repository", 50, None, packages_record())
            fallback = repository(root, "main.repository", 99, "https://example.invalid", packages_record())
            sources, preferences = plan.stage_repositories([preferred, fallback], root / "apt", "amd64")

            self.assertEqual(len(sources.read_text(encoding="utf-8").splitlines()), 2)
            pins = preferences.read_text(encoding="utf-8")
            self.assertIn("Pin: release l=local.repository\nPin-Priority: 1003", pins)
            self.assertIn("Pin: release l=main.repository\nPin-Priority: 1002", pins)
            release = (root / "apt/repositories/0/Release").read_text(encoding="utf-8")
            self.assertIn("Label: local.repository\n", release)


if __name__ == "__main__":
    unittest.main()
