"""Tests for the Go build driver's checks.

    buck test tine//go:test

The driver hands everything needing a toolchain to go, so what is left to test is how its output is
read and which declarations it refuses. The box these tests run in carries no go.
"""

import unittest
from pathlib import Path

import go_build as build

# What `go list -e -json=ImportPath,Name,Target` prints for a v2 module whose command sits at the
# module root, beside one in cmd/ and a library. go names the first after the second-to-last element
# of the import path, skipping the version, and leaves .Target off anything it would not install.
V2_LISTING = """\
{
	"ImportPath": "example.com/mycmd/v2",
	"Name": "main",
	"Target": "/var/tmp/gobin/mycmd"
}
{
	"ImportPath": "example.com/mycmd/v2/cmd/tool",
	"Name": "main",
	"Target": "/var/tmp/gobin/tool"
}
{
	"ImportPath": "example.com/mycmd/v2/internal/quote",
	"Name": "quote"
}
"""

COLLIDING_LISTING = """\
{
	"ImportPath": "example.com/p/cmd/agent",
	"Name": "main",
	"Target": "/var/tmp/gobin/agent"
}
{
	"ImportPath": "example.com/p/internal/testtools/agent",
	"Name": "main",
	"Target": "/var/tmp/gobin/agent"
}
{
	"ImportPath": "example.com/p/cmd/server",
	"Name": "main",
	"Target": "/var/tmp/gobin/server"
}
"""


class TestMainPackages(unittest.TestCase):
    def test_names_come_from_gos_own_target(self) -> None:
        self.assertEqual(
            build._group_by_name(V2_LISTING),
            {"mycmd": ["example.com/mycmd/v2"], "tool": ["example.com/mycmd/v2/cmd/tool"]},
        )

    def test_ignores_everything_that_is_not_a_command(self) -> None:
        """The stream carries a module's libraries too; only its main packages build a binary."""
        self.assertNotIn("quote", build._group_by_name(V2_LISTING))

    def test_builds_only_the_declared_binaries(self) -> None:
        self.assertEqual(
            build._select(["tool"], build._group_by_name(V2_LISTING), []),
            ["example.com/mycmd/v2/cmd/tool"],
        )

    def test_undeclared_commands_may_collide(self) -> None:
        self.assertEqual(
            build._select(["server"], build._group_by_name(COLLIDING_LISTING), []),
            ["example.com/p/cmd/server"],
        )

    def test_rejects_a_collision_among_declared_binaries(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build._select(["agent"], build._group_by_name(COLLIDING_LISTING), [])
        self.assertEqual(
            str(caught.exception),
            "go-build: several main packages build agent: example.com/p/cmd/agent, "
            "example.com/p/internal/testtools/agent",
        )

    def test_names_the_tags_a_missing_binary_was_looked_for_under(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build._select(["mycmd", "gated"], build._group_by_name(V2_LISTING), ["http", "insecure"])
        self.assertEqual(
            str(caught.exception),
            "go-build: no main package builds gated with tags [http insecure]; "
            "the module's commands are: mycmd, tool",
        )


class TestBuildCommand(unittest.TestCase):
    def test_builds_the_selected_packages(self) -> None:
        self.assertEqual(
            build._build_command(Path("/var/tmp/binaries"), ["example.com/mycmd/v2"], []),
            ["go", "build", "-o", "/var/tmp/binaries/", "example.com/mycmd/v2"],
        )

    def test_linker_flags_become_one_argument(self) -> None:
        """go splits GOFLAGS on spaces, which is why these ride on the command line instead."""
        self.assertEqual(
            build._build_command(Path("/var/tmp/binaries"), ["example.com/p"], ["-s", "-w"]),
            ["go", "build", "-o", "/var/tmp/binaries/", "-ldflags=-s -w", "example.com/p"],
        )
