"""Tests for the Python request the sandbox launcher composes.

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
from isolation import Bind, Devices, Sandbox, Symlink, Tmpfs
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


def _launch(*args: str) -> sandbox.Launch:
    return sandbox._launch(sandbox._parse(list(args)))


def _binds(launch: sandbox.Launch) -> list[Bind]:
    return [filesystem for filesystem in launch.sandbox.filesystems if isinstance(filesystem, Bind)]


def _symlinks(launch: sandbox.Launch) -> list[Symlink]:
    return [filesystem for filesystem in launch.sandbox.filesystems if isinstance(filesystem, Symlink)]


def _tmpfs(launch: sandbox.Launch) -> list[Tmpfs]:
    return [filesystem for filesystem in launch.sandbox.filesystems if isinstance(filesystem, Tmpfs)]


class TestRequest(unittest.TestCase):
    def test_command_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            _launch("--tools", "tools")

    def test_relaxed_rejects_bind_cwd(self) -> None:
        with self.assertRaises(SystemExit):
            _launch("--tools", "tools", "--relaxed", "--bind-cwd", "--", "true")

    def test_box_requires_relaxed(self) -> None:
        with self.assertRaises(SystemExit):
            _launch("--tools", "tools", "--box", "dev", "--", "true")

    def test_ro_bind_needs_a_destination(self) -> None:
        with _project() as (_, tools):
            with self.assertRaises(SystemExit):
                _launch("--tools", str(tools), "--ro-bind", "/run/signer.sock", "--", "true")


class TestHermetic(unittest.TestCase):
    def test_usr_merge_links_are_recreated_rather_than_bound(self) -> None:
        with _project() as (_, tools):
            launch = _launch("--tools", str(tools), "--", "true")

            self.assertIn(Symlink(Path("usr/bin"), Path("/bin")), _symlinks(launch))
            self.assertNotIn(tools / "bin", [bind.source for bind in _binds(launch)])

    def test_tools_directories_are_bound_read_only(self) -> None:
        with _project() as (_, tools):
            launch = _launch("--tools", str(tools), "--", "true")

            bound = {bind.source: bind.target for bind in _binds(launch) if bind.readonly}
            self.assertEqual(bound[tools / "usr"], Path("/usr"))
            self.assertEqual(bound[tools / "etc"], Path("/etc"))
            self.assertEqual(bound[tools / "home"], Path("/home"))

    def test_the_sandbox_supplies_the_api_filesystems_itself(self) -> None:
        with _project() as (_, tools):
            launch = _launch("--tools", str(tools), "--", "true")

            for name in ("proc", "sys", "dev", "run", "tmp", "boot"):
                self.assertNotIn(tools / name, [bind.source for bind in _binds(launch)])
            self.assertIn(Bind(Path("/proc"), Path("/proc")), _binds(launch))
            self.assertIn(Devices(Path("/dev")), launch.sandbox.filesystems)
            self.assertEqual(
                [tmpfs.target for tmpfs in _tmpfs(launch)], list(map(Path, ("/run", "/tmp", "/var/tmp")))
            )

    def test_project_is_bound_at_a_fixed_path(self) -> None:
        with _project() as (project, tools):
            launch = _launch("--tools", str(tools), "--bind-cwd", "--", "true")

            self.assertIn(Bind(project, Path(_PROJECT)), _binds(launch))
            self.assertEqual(launch.sandbox.chdir, Path(_PROJECT))

    def test_tools_directory_on_the_project_path_is_bound_like_any_other(self) -> None:
        with _project() as (project, tools):
            # A checkout under /var/lib or /root shares its first path component with a directory
            # the box populates. The fixed project path is what keeps the two independent.
            occupied = tools / project.parts[1]
            (occupied / "lib").mkdir(parents=True)

            launch = _launch("--tools", str(tools), "--bind-cwd", "--", "true")

            self.assertIn(Bind(occupied, Path("/") / occupied.name, readonly=True), _binds(launch))

    def test_absolute_project_paths_in_the_command_follow_the_mount(self) -> None:
        with _project() as (project, tools):
            launch = _launch(
                "--tools",
                str(tools),
                "--bind-cwd",
                "--",
                f"{project}/buck-out/driver.py",
                f"PYTHONPATH={project}/buck-out/lib",
                str(project),
            )

            self.assertEqual(
                launch.command,
                (
                    f"{_PROJECT}/buck-out/driver.py",
                    f"PYTHONPATH={_PROJECT}/buck-out/lib",
                    _PROJECT,
                ),
            )

    def test_paths_outside_the_project_are_left_alone(self) -> None:
        with _project() as (project, tools):
            sibling = f"{project}-other/artifact"

            launch = _launch(
                "--tools",
                str(tools),
                "--bind-cwd",
                "--",
                "true",
                "/run/signer.sock",
                sibling,
            )

            self.assertEqual(launch.command, ("true", "/run/signer.sock", sibling))

    def test_scratch_backs_var_tmp_on_disk(self) -> None:
        with _project() as (project, tools):
            with unittest.mock.patch.dict(os.environ, {"BUCK_SCRATCH_PATH": "scratch"}):
                launch = _launch("--tools", str(tools), "--bind-cwd", "--", "true")

            self.assertNotIn(Path("/var/tmp"), [tmpfs.target for tmpfs in _tmpfs(launch)])
            self.assertIn(Bind(project / "scratch/var-tmp", Path("/var/tmp")), _binds(launch))
            self.assertTrue((project / "scratch/var-tmp").is_dir())

    def test_network_replaces_the_unshared_namespace(self) -> None:
        with _project() as (_, tools):
            launch = _launch("--tools", str(tools), "--network", "--", "true")

            self.assertFalse(launch.sandbox.isolate_network)
            self.assertIn(Bind(Path("/run"), Path("/run"), readonly=True), _binds(launch))
            self.assertIn(
                Bind(Path("/etc/resolv.conf"), Path("/etc/resolv.conf"), readonly=True, nofollow=True),
                _binds(launch),
            )

    def test_builds_get_fakeroot_semantics(self) -> None:
        with _project() as (_, tools):
            launch = _launch("--tools", str(tools), "--", "true")

            self.assertTrue(launch.sandbox.become_root)
            self.assertTrue(launch.sandbox.suppress_chown)
            self.assertTrue(launch.sandbox.suppress_sync)


class TestRelaxed(unittest.TestCase):
    def test_userspace_comes_from_tools_and_the_rest_from_the_host(self) -> None:
        with _project() as (project, tools):
            launch = _launch("--tools", str(tools), "--relaxed", "--", "true")

            self.assertIn(Bind(tools / "usr", Path("/usr"), readonly=True), _binds(launch))
            self.assertEqual(launch.sandbox.chdir, project)
            self.assertFalse(launch.sandbox.isolate_network)
            self.assertFalse(launch.sandbox.become_root)

    def test_a_development_box_may_choose_its_shell_after_entry(self) -> None:
        with _project() as (_, tools):
            launch = _launch("--tools", str(tools), "--relaxed", "--box", "dev")

        self.assertEqual(launch.command, ())


class TestInteractiveShell(unittest.TestCase):
    def test_shell_from_the_environment_wins(self) -> None:
        with unittest.mock.patch.object(
            sandbox.shutil,
            "which",
            return_value="/box/bin/zsh",
        ) as which:
            shell = sandbox._interactive_shell({"PATH": "/box/bin", "SHELL": "zsh"})

        self.assertEqual(shell, "/box/bin/zsh")
        which.assert_called_once_with("zsh", path="/box/bin")

    def test_bash_is_the_fallback(self) -> None:
        environment = {
            "PATH": "/box/bin",
            "SHELL": "/bin/fish",
            "STARSHIP_SHELL": "fish",
            "TINE_BOX": "systemd",
        }
        with unittest.mock.patch.object(
            sandbox.shutil,
            "which",
            side_effect=[None, "/box/bin/bash"],
        ) as which:
            shell = sandbox._interactive_shell(environment)

        self.assertEqual(shell, "/box/bin/bash")
        self.assertEqual(environment["SHELL"], "/box/bin/bash")
        self.assertEqual(environment["SHELL_PROMPT_PREFIX"], "(systemd)")
        self.assertNotIn("STARSHIP_SHELL", environment)
        self.assertEqual(
            which.call_args_list,
            [
                unittest.mock.call("/bin/fish", path="/box/bin"),
                unittest.mock.call("bash", path="/box/bin"),
            ],
        )

    def test_no_installed_shell_is_an_error(self) -> None:
        with (
            unittest.mock.patch.object(sandbox.shutil, "which", return_value=None),
            self.assertRaisesRegex(SystemExit, "no shell installed in box"),
        ):
            sandbox._interactive_shell({"PATH": "/box/bin"})

    def test_shell_is_chosen_after_entering_the_box(self) -> None:
        launch = sandbox.Launch(sandbox=Sandbox(filesystems=()), command=(), environment={})
        entered = False

        def enter(_sandbox: Sandbox) -> None:
            nonlocal entered
            entered = True

        def shell(_environment: dict[str, str]) -> str:
            self.assertTrue(entered)
            return "/bin/bash"

        with (
            unittest.mock.patch.object(sandbox, "_parse"),
            unittest.mock.patch.object(sandbox, "_launch", return_value=launch),
            unittest.mock.patch.object(sandbox, "enter", side_effect=enter),
            unittest.mock.patch.object(sandbox, "_interactive_shell", side_effect=shell),
            unittest.mock.patch.object(os, "execvpe") as execute,
            self.assertRaises(SystemExit) as raised,
        ):
            sandbox.main([])

        self.assertEqual(raised.exception.code, 127)
        execute.assert_called_once_with("/bin/bash", ("/bin/bash",), {})


if __name__ == "__main__":
    unittest.main()
