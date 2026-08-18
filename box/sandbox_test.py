"""Tests for the command line the sandbox launcher composes.

buck test tine//box:test
"""

import os
import tempfile
import unittest
import unittest.mock
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sandbox
from sandbox import _PROJECT


def _tools(root: Path) -> Path:
    """A tools tree holding one of each entry the launcher treats differently."""
    tools = root / "tools"
    (tools / "usr/bin").mkdir(parents=True)
    (tools / "bin").symlink_to("usr/bin")
    (tools / "etc").mkdir()
    (tools / "etc/os-release").touch()
    (tools / "home").mkdir()  # empty, as a distribution ships it
    for name in ("proc", "sys", "dev", "run", "tmp", "boot"):
        (tools / name).mkdir()
    return tools


@contextmanager
def _project() -> Iterator[tuple[Path, Path]]:
    """Enter a temporary project directory, with a tools tree beside it."""
    with tempfile.TemporaryDirectory(prefix="sandbox-test.", dir="/var/tmp") as scratch:
        root = Path(scratch)
        project = root / "project"
        project.mkdir()
        previous = Path.cwd()
        os.chdir(project)
        try:
            # No scratch directory: `buck test` is not a run action, and a test that wants one
            # sets it itself.
            with unittest.mock.patch.dict(os.environ):
                os.environ.pop("BUCK_SCRATCH_PATH", None)
                yield project, _tools(root)
        finally:
            os.chdir(previous)


def _argv(*args: str) -> list[str]:
    return sandbox._argv(sandbox._parse(list(args)))


def _pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    """Every (source, destination) pair one two-argument flag was given."""
    return [(argv[i + 1], argv[i + 2]) for i, arg in enumerate(argv) if arg == flag]


def _value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _values(argv: list[str], flag: str) -> list[str]:
    """Every argument one single-argument flag was given, in order."""
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == flag]


class TestRequest(unittest.TestCase):
    def test_command_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            _argv("--tools", "tools")

    def test_relaxed_rejects_bind_cwd(self) -> None:
        with self.assertRaises(SystemExit):
            _argv("--tools", "tools", "--relaxed", "--bind-cwd", "--", "true")

    def test_box_requires_relaxed(self) -> None:
        with self.assertRaises(SystemExit):
            _argv("--tools", "tools", "--box", "dev", "--", "true")

    def test_ro_bind_needs_a_destination(self) -> None:
        with _project() as (_, tools):
            with self.assertRaises(SystemExit):
                _argv("--tools", str(tools), "--ro-bind", "/run/signer.sock", "--", "true")


class TestHermetic(unittest.TestCase):
    def test_usr_merge_links_are_recreated_rather_than_bound(self) -> None:
        with _project() as (_, tools):
            argv = _argv("--tools", str(tools), "--", "true")

            self.assertIn(("usr/bin", "/bin"), _pairs(argv, "--symlink"))
            self.assertNotIn(str(tools / "bin"), argv)

    def test_tools_directories_are_bound_read_only(self) -> None:
        with _project() as (_, tools):
            argv = _argv("--tools", str(tools), "--", "true")

            bound = dict(_pairs(argv, "--ro-bind"))
            self.assertEqual(bound[str(tools / "usr")], "/usr")
            self.assertEqual(bound[str(tools / "etc")], "/etc")
            self.assertEqual(bound[str(tools / "home")], "/home")

    def test_the_sandbox_supplies_the_api_filesystems_itself(self) -> None:
        with _project() as (_, tools):
            argv = _argv("--tools", str(tools), "--", "true")

            for name in ("proc", "sys", "dev", "run", "tmp", "boot"):
                self.assertNotIn(str(tools / name), argv)
            self.assertIn(("/proc", "/proc"), _pairs(argv, "--bind"))
            self.assertEqual(_value(argv, "--dev"), "/dev")
            self.assertEqual(_values(argv, "--tmpfs"), ["/run", "/tmp", "/var/tmp"])

    def test_project_is_bound_at_a_fixed_path(self) -> None:
        with _project() as (project, tools):
            argv = _argv("--tools", str(tools), "--bind-cwd", "--", "true")

            self.assertIn((str(project), _PROJECT), _pairs(argv, "--bind"))
            self.assertEqual(_value(argv, "--chdir"), _PROJECT)

    def test_tools_directory_on_the_project_path_is_bound_like_any_other(self) -> None:
        with _project() as (project, tools):
            # A checkout under /var/lib or /root shares its first path component with a directory
            # the box populates. The fixed project path is what keeps the two independent.
            occupied = tools / project.parts[1]
            (occupied / "lib").mkdir(parents=True)

            argv = _argv("--tools", str(tools), "--bind-cwd", "--", "true")

            self.assertIn((str(occupied), "/" + occupied.name), _pairs(argv, "--ro-bind"))

    def test_absolute_project_paths_in_the_command_follow_the_mount(self) -> None:
        with _project() as (project, tools):
            argv = _argv(
                "--tools",
                str(tools),
                "--bind-cwd",
                "--",
                f"{project}/buck-out/driver.py",
                f"PYTHONPATH={project}/buck-out/lib",
                str(project),
            )

            self.assertEqual(
                argv[-4:],
                [
                    "--",
                    f"{_PROJECT}/buck-out/driver.py",
                    f"PYTHONPATH={_PROJECT}/buck-out/lib",
                    _PROJECT,
                ],
            )

    def test_paths_outside_the_project_are_left_alone(self) -> None:
        with _project() as (project, tools):
            sibling = f"{project}-other/artifact"

            argv = _argv("--tools", str(tools), "--bind-cwd", "--", "true", "/run/signer.sock", sibling)

            self.assertEqual(argv[-3:], ["true", "/run/signer.sock", sibling])

    def test_scratch_backs_var_tmp_on_disk(self) -> None:
        with _project() as (project, tools):
            with unittest.mock.patch.dict(os.environ, {"BUCK_SCRATCH_PATH": "scratch"}):
                argv = _argv("--tools", str(tools), "--bind-cwd", "--", "true")

            self.assertNotIn("/var/tmp", _values(argv, "--tmpfs"))
            self.assertIn((str(project / "scratch/var-tmp"), "/var/tmp"), _pairs(argv, "--bind"))
            self.assertTrue((project / "scratch/var-tmp").is_dir())

    def test_network_replaces_the_unshared_namespace(self) -> None:
        with _project() as (_, tools):
            argv = _argv("--tools", str(tools), "--network", "--", "true")

            self.assertNotIn("--unshare-net", argv)
            self.assertIn(("/run", "/run"), _pairs(argv, "--ro-bind"))
            self.assertIn(("/etc/resolv.conf", "/etc/resolv.conf"), _pairs(argv, "--ro-bind-nofollow"))

    def test_builds_get_fakeroot_semantics(self) -> None:
        with _project() as (_, tools):
            argv = _argv("--tools", str(tools), "--", "true")

            self.assertEqual(
                argv[-5:], ["--suppress-chown", "--suppress-sync", "--become-root", "--", "true"]
            )


class TestRelaxed(unittest.TestCase):
    def test_userspace_comes_from_tools_and_the_rest_from_the_host(self) -> None:
        with _project() as (project, tools):
            argv = _argv("--tools", str(tools), "--relaxed", "--", "true")

            self.assertIn((str(tools / "usr"), "/usr"), _pairs(argv, "--ro-bind"))
            self.assertEqual(_value(argv, "--chdir"), str(project))
            self.assertNotIn("--unshare-net", argv)
            self.assertNotIn("--become-root", argv)


if __name__ == "__main__":
    unittest.main()
