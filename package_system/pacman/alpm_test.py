"""Tests for the alpm database format.

buck test tine//package_system/pacman:test
"""

import tempfile
import unittest
from pathlib import Path

import alpm

EPOCH = 1739577600

ENTRY = {
    "FILENAME": ["0-pacman-7.1.0-2-x86_64.pkg.tar.zst"],
    "NAME": ["pacman"],
    "VERSION": ["7.1.0-2"],
    "CSIZE": ["991730"],
    "SHA256SUM": ["2092EC7A0391416E4A1757C455BC071D7B58A124CEDC83F582CB7D37D52F211B"],
    "DEPENDS": ["bash", "coreutils"],
    "PROVIDES": ["libalpm.so=16-64"],
}


class TestParseDesc(unittest.TestCase):
    def test_reads_repeated_and_single_values(self) -> None:
        entry = alpm.parse_desc(
            "%NAME%\npacman\n\n%VERSION%\n7.1.0-2\n\n%DEPENDS%\nbash\ncoreutils\n\n%CSIZE%\n991730\n"
        )
        self.assertEqual(entry["NAME"], ["pacman"])
        self.assertEqual(entry["DEPENDS"], ["bash", "coreutils"])

    def test_ignores_a_line_that_only_looks_like_a_key(self) -> None:
        self.assertEqual(alpm.parse_desc("%NAME%\n%\n%%\n"), {"NAME": ["%", "%%"]})


class TestPackageFromDesc(unittest.TestCase):
    def test_reads_what_a_transaction_names_a_package_by(self) -> None:
        package = alpm.package_from_desc(ENTRY, "core", "core.db")
        self.assertEqual(package.id, "pacman-7.1.0-2")
        self.assertEqual(package.size, 991730)
        # A checksum identifies a pool artifact, which is keyed in lower case.
        self.assertEqual(package.sha256, ENTRY["SHA256SUM"][0].lower())

    def test_rejects_a_repeated_single_value(self) -> None:
        with self.assertRaises(SystemExit):
            alpm.package_from_desc(dict(ENTRY, NAME=["a", "b"]), "core", "core.db")

    def test_rejects_a_non_numeric_size(self) -> None:
        with self.assertRaises(SystemExit):
            alpm.package_from_desc(dict(ENTRY, CSIZE=["huge"]), "core", "core.db")

    def test_requires_a_name(self) -> None:
        with self.assertRaises(SystemExit):
            alpm.package_from_desc({"VERSION": ["1-1"]}, "core", "core.db")


class TestLocalNaming(unittest.TestCase):
    """The indexer publishes a name, the planner recovers a location; they must agree."""

    def test_round_trips(self) -> None:
        for position in (0, 1, 9, 10, 42):
            for name in (
                "acl-2.4.0-1-x86_64.pkg.tar.zst",
                # A package whose own name starts with a digit, and one carrying an epoch.
                "0ad-0.0.26-1-x86_64.pkg.tar.zst",
                "fakeroot-1:1.37.2-2-x86_64.pkg.tar.zst",
                "sigrok-firmware-fx2lafw-0.1.7-1-any.pkg.tar.xz",
            ):
                with self.subTest(position=position, name=name):
                    href = alpm.local_href(position, name)
                    # alpm refuses a separator here; that is the whole reason for the encoding.
                    self.assertNotIn("/", href)
                    self.assertEqual(alpm.local_location(href, "test"), f"{position}/{name}")

    def test_rejects_a_name_it_did_not_publish(self) -> None:
        for href in ("acl-2.4.0-1-x86_64.pkg.tar.zst", "acl", "-acl", ""):
            with self.subTest(href=href), self.assertRaises(SystemExit):
                alpm.local_location(href, "test")


class TestWriteDb(unittest.TestCase):
    """A generated database is an input to pacman, so it must round-trip and not drift."""

    def database(self, directory: Path, name: str = "local.db") -> Path:
        out = directory / name
        alpm.write_db([("pacman-7.1.0-2", ENTRY), ("acl-2.4.0-1", dict(ENTRY, NAME=["acl"]))], out, EPOCH)
        return out

    def test_round_trips_through_read_db(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            packages = alpm.read_db(self.database(Path(scratch)), "local")
            self.assertEqual([package.name for package in packages], ["acl", "pacman"])
            self.assertEqual(packages[1].id, "pacman-7.1.0-2")
            self.assertEqual(packages[1].filename, ENTRY["FILENAME"][0])
            self.assertEqual(packages[1].size, 991730)

    def test_is_byte_identical_across_writes_and_names(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            first = self.database(directory, "first.db").read_bytes()
            second = self.database(directory, "second.db").read_bytes()
            # Equal despite differing output names: gzip records one, and write_db suppresses it.
            self.assertEqual(first, second)

    def test_drops_an_empty_value_list(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / "local.db"
            alpm.write_db([("acl-2.4.0-1", dict(ENTRY, GROUPS=[]))], out, EPOCH)
            self.assertNotIn("%GROUPS%", out.read_bytes().decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
