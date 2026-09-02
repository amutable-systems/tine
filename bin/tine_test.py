"""Tests for the wrapper's git query, buckconfig reading and argument handling.

    buck test tine//bin:test

Each test builds a real throwaway repository or checkout; the wrapper's git calls run for real.
"""

import collections.abc
import contextlib
import io
import json
import os
import re
import runpy
import signal
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import cast, override

import tine

TOOL_PATH = Path(__file__).parent / "tine"


class TestCommand(unittest.TestCase):
    def test_loads_the_module_in_isolated_mode(self) -> None:
        proc = subprocess.run([str(TOOL_PATH), "--help"], check=True, text=True, stdout=subprocess.PIPE)
        self.assertEqual(proc.stdout, tine.USAGE)

    def test_an_old_python_is_rejected_before_the_module_is_loaded(self) -> None:
        with (
            unittest.mock.patch.object(sys, "version_info", (3, 11)),
            unittest.mock.patch.object(sys, "argv", [str(TOOL_PATH)]),
            self.assertRaisesRegex(SystemExit, "Python 3.12 or newer is required"),
        ):
            runpy.run_path(str(TOOL_PATH), run_name="__main__")


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


def declare(root: Path, mounts: dict[str, str]) -> None:
    """Write the machine-local declarations `tine mount` would have left."""
    (root / tine.LOCAL_CONFIG).write_text(tine.render_mounts(mounts))


def scratch(case: unittest.TestCase, prefix: str = "tine-test.") -> Path:
    tmp = tempfile.TemporaryDirectory(prefix=prefix)
    case.addCleanup(tmp.cleanup)
    return Path(tmp.name)


def pins(cell: Path, spec: object | None = None) -> Path:
    """The `tools.json` a cell declares its Buck2 in, holding a usable entry unless given one."""
    path = cell / tine.PINS
    path.parent.mkdir(parents=True, exist_ok=True)
    default = {
        "buck2": {
            "repository": "example/buck2",
            "release": "2026-01-01",
            "platforms": {tine._platform(): {"artifact": "buck2.zst", "sha256": "a" * 64}},
        }
    }
    path.write_text(json.dumps(default if spec is None else spec))
    return path


def isolate_git(case: unittest.TestCase) -> None:
    """Keep the developer's own git configuration out of it; `tag.gpgSign` alone breaks the suite."""
    patched = unittest.mock.patch.dict(
        os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    )
    patched.start()
    case.addCleanup(patched.stop)


class RepositoryTestCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        isolate_git(self)
        self.repo = scratch(self)
        git("init", "--quiet", "--initial-branch=main", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        git("config", "user.email", "test@example.com", cwd=self.repo)

    def commit(self, message: str = "commit") -> str:
        git("commit", "--allow-empty", "--quiet", "--message", message, cwd=self.repo)
        return git("rev-parse", "HEAD", cwd=self.repo)

    def dirty(self) -> None:
        (self.repo / "uncommitted").write_text("wip")


class TestComponents(RepositoryTestCase):
    def test_release(self) -> None:
        commit = self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.assertEqual(
            tine.components(self.repo),
            {"base": "1.2.3", "count": 0, "height": 1, "commit": commit},
        )

    def test_snapshot_counts_commits(self) -> None:
        # The whole commit, never an abbreviation: how much of it fits is the renderer's question.
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.commit("second")
        commit = self.commit("third")
        self.assertEqual(
            tine.components(self.repo),
            {"base": "1.2.3", "count": 2, "height": 3, "commit": commit},
        )

    def test_no_tag(self) -> None:
        commit = self.commit()
        self.assertEqual(
            tine.components(self.repo),
            {"base": "0.0.0", "count": 1, "height": 1, "commit": commit},
        )

    def test_v_prefixed_word_is_not_a_version_tag(self) -> None:
        # `v*` would match this and derive the version `endor-drop-2024` from it.
        commit = self.commit()
        git("tag", "vendor-drop-2024", cwd=self.repo)
        self.assertEqual(
            tine.components(self.repo),
            {"base": "0.0.0", "count": 1, "height": 1, "commit": commit},
        )

    def test_latest_tag_wins(self) -> None:
        self.commit()
        git("tag", "v1.0.0", cwd=self.repo)
        commit = self.commit("second")
        git("tag", "v2.0.0", cwd=self.repo)
        self.assertEqual(
            tine.components(self.repo),
            {"base": "2.0.0", "count": 0, "height": 2, "commit": commit},
        )

    def test_tag_sharing_a_branch_name(self) -> None:
        # An unqualified revision would be ambiguous, which git resolves with a warning.
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        git("branch", "v1.2.3", cwd=self.repo)
        commit = self.commit("second")
        self.assertEqual(
            tine.components(self.repo),
            {"base": "1.2.3", "count": 1, "height": 2, "commit": commit},
        )

    def test_uncommitted_work_sets_the_dirty_bit(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        commit = self.commit("second")
        self.dirty()
        self.assertEqual(
            tine.components(self.repo),
            {"base": "1.2.3", "count": 1, "height": 2, "commit": commit, "dirty": 1},
        )

    def test_the_bit_does_not_move_as_the_tree_keeps_changing(self) -> None:
        # It says that a checkout holds more than its commit, and nothing about how much: the
        # version it renders has to stand still while the work on top of that commit goes on.
        commit = self.commit()
        self.dirty()
        first = tine.components(self.repo)
        (self.repo / "uncommitted").write_text("more")
        (self.repo / "another").write_text("wip")
        git("add", "another", cwd=self.repo)
        self.assertEqual(tine.components(self.repo), first)
        self.assertEqual(
            first,
            {"base": "0.0.0", "count": 1, "height": 1, "commit": commit, "dirty": 1},
        )

    def test_untracked_files_are_dirty_whatever_git_is_configured_to_show(self) -> None:
        commit = self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        git("config", "status.showUntrackedFiles", "no", cwd=self.repo)
        self.dirty()
        self.assertEqual(
            tine.components(self.repo),
            {"base": "1.2.3", "count": 0, "height": 1, "commit": commit, "dirty": 1},
        )

    def test_height_ignores_tags(self) -> None:
        # A version whose base a tag never named counts in the height, so a new tag must not move it.
        self.commit()
        self.commit("second")
        before = tine.components(self.repo)
        git("tag", "v9.9.9", cwd=self.repo)
        after = tine.components(self.repo)
        assert isinstance(before, dict) and isinstance(after, dict)
        self.assertEqual((after["height"], after["count"]), (before["height"], 0))

    def test_ignores_the_repository_git_was_pointed_at(self) -> None:
        # `git -C` would leave GIT_DIR alone, so a run from a hook would describe the hook's repo.
        other = scratch(self, "tine-test-other.")
        git("init", "--quiet", cwd=other)
        commit = self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        with unittest.mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            self.assertEqual(
                tine.components(self.repo),
                {"base": "1.2.3", "count": 0, "height": 1, "commit": commit},
            )


class TestNoComponents(RepositoryTestCase):
    """A checkout that cannot answer says why, rather than taking the command down with it."""

    def test_not_a_repository(self) -> None:
        self.assertEqual(tine.components(scratch(self)), "not a git checkout")

    def test_repository_without_commits(self) -> None:
        self.assertIn("git rev-parse HEAD", str(tine.components(self.repo)))

    def test_shallow_checkout(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.commit("second")
        clone = scratch(self, "tine-test-clone.") / "clone"
        git("clone", "--quiet", "--depth", "1", f"file://{self.repo}", str(clone), cwd=self.repo)
        self.assertEqual(tine.components(clone), "shallow git checkout")

    def test_tag_that_is_not_a_version(self) -> None:
        self.commit()
        git("tag", "v1.2.3+dirty", cwd=self.repo)
        self.assertEqual(tine.components(self.repo), "tag 'v1.2.3+dirty' is not a valid version")

    def test_reason_is_recorded_in_the_generated_config(self) -> None:
        self.assertIn("# no version components: not a git checkout", tine.generate(scratch(self)))


class TestGenerate(RepositoryTestCase):
    def test_writes_every_component_it_has(self) -> None:
        commit = self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.assertEqual(
            tine.generate(self.repo),
            [
                tine.BLOCK_BEGIN,
                tine.BLOCK_NOTE,
                "",
                "[tine]",
                "version-base = 1.2.3",
                "version-count = 0",
                "version-height = 1",
                f"version-commit = {commit}",
                tine.BLOCK_END,
            ],
        )

    def test_the_dirty_bit_appears_only_for_an_uncommitted_tree(self) -> None:
        self.commit()
        self.assertNotIn("version-dirty = 1", tine.generate(self.repo))
        self.dirty()
        self.assertIn("version-dirty = 1", tine.generate(self.repo))

    def test_every_line_it_writes_is_buckconfig(self) -> None:
        # A checkout without commits fails `rev-parse` with three lines of git advice; all but the
        # first would land in the file as configuration, and Buck would refuse to parse it.
        (self.repo / ".buckconfig").write_text("")
        tine.refresh(self.repo)
        for line in (self.repo / tine.LOCAL).read_text().splitlines():
            self.assertTrue(not line or line.startswith(("#", "[", "<")) or "=" in line)
        self.assertEqual(tine.project_config(self.repo), {"": {}})


class TestWriteIfChanged(unittest.TestCase):
    def test_leaves_an_unchanged_file_alone(self) -> None:
        path = scratch(self) / tine.LOCAL
        tine.write_if_changed(path, "one\n")
        before = path.stat().st_mtime_ns
        tine.write_if_changed(path, "one\n")
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_replaces_a_changed_file(self) -> None:
        path = scratch(self) / tine.LOCAL
        tine.write_if_changed(path, "one\n")
        tine.write_if_changed(path, "two\n")
        self.assertEqual(path.read_text(), "two\n")

    def test_a_file_reached_through_a_symlink_is_written_through(self) -> None:
        # Buck reads that layout, and renaming over the link would leave the real file stale.
        root = scratch(self)
        shared = root / "shared.bcfg"
        shared.write_text("one\n")
        link = root / tine.LOCAL
        link.symlink_to(shared)
        tine.write_if_changed(link, "two\n")
        self.assertTrue(link.is_symlink())
        self.assertEqual(shared.read_text(), "two\n")

    def test_reports_a_path_it_cannot_write(self) -> None:
        # Root, which the box runs as, writes through a read-only directory; an absent one stops it.
        with self.assertRaisesRegex(SystemExit, "tine: cannot write"):
            tine.write_if_changed(scratch(self) / "absent" / tine.LOCAL, "one\n")


class TestMerge(unittest.TestCase):
    """The block is this command's; the rest of the file stays the developer's."""

    def local(self, content: str | None = None) -> Path:
        path = scratch(self) / tine.LOCAL
        if content is not None:
            path.write_text(content)
        return path

    def test_a_file_that_does_not_exist_yet(self) -> None:
        self.assertEqual(tine.merge(self.local(), ["one", "two"]), "one\ntwo\n")

    def test_content_that_was_there_first_is_kept_after_the_block(self) -> None:
        path = self.local("[buck2]\nmaterializations = all\n")
        self.assertEqual(
            tine.merge(path, [tine.BLOCK_BEGIN, tine.BLOCK_END]),
            f"{tine.BLOCK_BEGIN}\n{tine.BLOCK_END}\n\n[buck2]\nmaterializations = all\n",
        )

    def test_an_earlier_block_is_replaced_and_not_repeated(self) -> None:
        path = self.local(
            f"{tine.BLOCK_BEGIN}\n[tine]\nversion-base = 1.0.0\n{tine.BLOCK_END}\n\n[demo]\nkey = mine\n"
        )
        merged = tine.merge(path, [tine.BLOCK_BEGIN, "[tine]", "version-base = 2.0.0", tine.BLOCK_END])
        self.assertEqual(merged.count(tine.BLOCK_BEGIN), 1)
        self.assertNotIn("1.0.0", merged)
        self.assertIn("[demo]\nkey = mine\n", merged)

    def test_a_block_missing_its_end_is_reported(self) -> None:
        path = self.local(f"{tine.BLOCK_BEGIN}\n[tine]\nversion-base = 1.0.0\n")
        with self.assertRaisesRegex(SystemExit, "has no .* line"):
            tine.merge(path, [tine.BLOCK_BEGIN, tine.BLOCK_END])

    def test_a_block_missing_its_beginning_is_reported(self) -> None:
        # Keeping it would leave the old block after the new one, where it wins for good.
        path = self.local(f"[tine]\nversion-base = 1.0.0\n{tine.BLOCK_END}\n")
        with self.assertRaisesRegex(SystemExit, "ends without beginning"):
            tine.merge(path, [tine.BLOCK_BEGIN, tine.BLOCK_END])

    def test_the_block_is_not_read_back_as_configuration(self) -> None:
        # Reading our own `disabled` back would make the cell look unexpanded and drop the entry.
        root = scratch(self)
        (root / ".buckconfig").write_text("[external_cells]\nsub = git\n")
        (root / tine.LOCAL).write_text(
            f"{tine.BLOCK_BEGIN}\n[external_cells]\nsub = disabled\n{tine.BLOCK_END}\n"
        )
        self.assertEqual(tine.project_config(root)["external_cells"], {"sub": "git"})


class TestReadLines(unittest.TestCase):
    def test_a_file_that_is_not_text(self) -> None:
        # Buck2 answers the same file with a parse error naming it; a traceback would not.
        path = scratch(self) / ".buckconfig"
        path.write_bytes(b"[cells]\nroot = \xff\n")
        with self.assertRaisesRegex(SystemExit, "cannot read"):
            tine.read_lines(path)


class TestReadToml(unittest.TestCase):
    def test_an_absent_file_is_empty_configuration(self) -> None:
        self.assertEqual(tine.read_toml(scratch(self) / tine.CONFIG), {})

    def test_a_malformed_file_names_itself(self) -> None:
        path = scratch(self) / tine.CONFIG
        path.write_text("[commands\n")
        with self.assertRaisesRegex(SystemExit, rf"cannot parse {re.escape(str(path))}"):
            tine.read_toml(path)

    def test_project_settings_reject_unknown_top_level_keys(self) -> None:
        root = scratch(self)
        (root / tine.CONFIG).write_text("[command.check]\nsteps = []\n")
        with self.assertRaisesRegex(SystemExit, "unsupported keys: command"):
            tine.project_settings(root)


class TestParseBuckconfig(unittest.TestCase):
    def parse(self, files: dict[str, str]) -> dict[str, dict[str, str]]:
        root = scratch(self)
        for name, content in files.items():
            (root / name).write_text(content)
        return tine.project_config(root)

    def test_sections_and_entries(self) -> None:
        config = self.parse({".buckconfig": "[cells]\nroot = .\n# comment\nsub = sub\n"})
        self.assertEqual(config["cells"], {"root": ".", "sub": "sub"})

    def test_section_marker_with_a_trailing_comment(self) -> None:
        # Buck2 accepts these, and losing the marker would file the entries under the last section.
        config = self.parse(
            {".buckconfig": "[cells]\nroot = .\n[external_cells] # @oss-enable\nsub = git\n"}
        )
        self.assertEqual(config["external_cells"], {"sub": "git"})
        self.assertEqual(config["cells"], {"root": "."})

    def test_continuation_lines(self) -> None:
        config = self.parse({".buckconfig": "[cells]\nsub = \\\n    some/deep/path\n"})
        self.assertEqual(config["cells"]["sub"], "some/deep/path")

    def test_values_keep_what_is_not_a_comment(self) -> None:
        config = self.parse({".buckconfig": "[project]\nignore = a, b # c\nempty =\n"})
        self.assertEqual(config["project"], {"ignore": "a, b # c", "empty": ""})

    def test_includes_are_followed(self) -> None:
        config = self.parse(
            {
                ".buckconfig": "[cells]\nsub = sub\n<file:cells.bcfg>\n",
                "cells.bcfg": "[external_cells]\nsub = git\n",
            }
        )
        self.assertEqual(config["external_cells"], {"sub": "git"})

    def test_an_include_carrying_a_comment_is_still_an_include(self) -> None:
        # Buck2 reads the include off the front of the line, as `# @oss-enable` markers rely on.
        config = self.parse(
            {
                ".buckconfig": "[cells]\nsub = sub\n<file:cells.bcfg> # @oss-enable\n",
                "cells.bcfg": "[external_cells]\nsub = git\n",
            }
        )
        self.assertEqual(config["external_cells"], {"sub": "git"})

    def test_what_follows_an_include_continues_in_the_included_file(self) -> None:
        # Buck2 does not go back to the section the including file was in.
        config = self.parse(
            {
                ".buckconfig": "[demo]\na = 1\n<file:sub.bcfg>\nafter = 2\n",
                "sub.bcfg": "[other]\nx = 1\n",
            }
        )
        self.assertEqual(config["demo"], {"a": "1"})
        self.assertEqual(config["other"], {"x": "1", "after": "2"})

    def test_a_missing_optional_include_is_not_an_error(self) -> None:
        self.assertEqual(
            self.parse({".buckconfig": "[cells]\nsub = sub\n<?file:absent.bcfg>\n"})["cells"], {"sub": "sub"}
        )

    def test_a_line_with_no_key_is_not_an_entry(self) -> None:
        self.assertEqual(
            self.parse({".buckconfig": "[cells]\n= orphan\nsub = sub\n"})["cells"], {"sub": "sub"}
        )

    def test_a_value_may_contain_the_separator(self) -> None:
        config = self.parse({".buckconfig": "[build]\nflags = -c a=b\n"})
        self.assertEqual(config["build"]["flags"], "-c a=b")

    def test_an_unterminated_section_marker_starts_no_section(self) -> None:
        config = self.parse({".buckconfig": "[cells]\nsub = sub\n[broken\nkey = value\n"})
        self.assertEqual(config["cells"], {"sub": "sub", "key": "value"})

    def test_the_lowest_layer_is_read_too(self) -> None:
        # Buck2 reads `.buckconfig.d` whole, below `.buckconfig`.
        root = scratch(self)
        (root / ".buckconfig.d" / "nested").mkdir(parents=True)
        (root / ".buckconfig").write_text("[cells]\nroot = .\n")
        (root / ".buckconfig.d" / "10-project.bcfg").write_text("[project]\nname = example\n")
        (root / ".buckconfig.d" / "nested" / "20-cells.bcfg").write_text("[cells]\nsub = sub\n")
        config = tine.project_config(root)
        self.assertEqual(config["project"]["name"], "example")
        self.assertEqual(config["cells"], {"root": ".", "sub": "sub"})

    def test_a_symlink_in_the_lowest_layer_is_not_read(self) -> None:
        # Buck2 lists that directory with `lstat` and skips one, so reading it would configure the
        # build with something Buck2 never sees.
        root = scratch(self)
        (root / ".buckconfig.d").mkdir()
        (root / ".buckconfig").write_text("[cells]\nroot = .\n")
        (root / "elsewhere.bcfg").write_text("[cells]\nsub = sub\n")
        (root / ".buckconfig.d" / "10-cells.bcfg").symlink_to(root / "elsewhere.bcfg")
        self.assertEqual(tine.project_config(root)["cells"], {"root": "."})

    def test_local_overrides_win(self) -> None:
        config = self.parse(
            {
                ".buckconfig": "[cells]\nsub = sub\n",
                ".buckconfig.local": "[cells]\nsub = elsewhere\n",
            }
        )
        self.assertEqual(config["cells"]["sub"], "elsewhere")


class MountTestCase(unittest.TestCase):
    """Test mounts from `self.source` into the project at `self.root`."""

    @override
    def setUp(self) -> None:
        # Commands resolve the project root from the working directory.
        self.root = scratch(self, "tine-test-project.").resolve()
        (self.root / ".buckconfig").write_text(
            "[cells]\nroot = .\nsub = sub\nother = other\ninner = sub/inner\n"
        )
        (self.root / "sub").mkdir()
        self.source = scratch(self, "tine-test-source.").resolve()

    def mount(self, *arguments: str) -> str:
        """Run `tine mount` and return its stderr."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tine.mount(self.root, list(arguments))
        return stderr.getvalue()

    def declared(self) -> dict[str, str]:
        return tine.declared_mounts(self.root)

    def local(self) -> str:
        return (self.root / tine.LOCAL_CONFIG).read_text()


class TestMountAdd(MountTestCase):
    def test_add_records_an_external_source(self) -> None:
        report = self.mount("add", str(self.root / "sub"), str(self.source))
        self.assertEqual(report, f"tine: sub is built from {self.source}\n")
        self.assertEqual(self.declared(), {"sub": str(self.source)})
        self.assertIn(f'[{tine.MOUNTS}]\n"sub" = "{self.source}"', self.local())

    def test_target_is_project_relative(self) -> None:
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)
        self.mount("add", "sub", str(self.source))
        self.assertEqual(self.declared(), {"sub": str(self.source)})

    def test_add_replaces_an_existing_mount(self) -> None:
        other = scratch(self).resolve()
        self.mount("add", str(self.root / "sub"), str(self.source))
        self.mount("add", str(self.root / "sub"), str(other))
        self.assertEqual(self.declared(), {"sub": str(other)})

    def test_add_does_not_rewrite_buckconfig_local(self) -> None:
        local = self.root / tine.LOCAL
        local.write_text("[build]\nthreads = 4\n")
        before = local.read_bytes()
        self.mount("add", str(self.root / "sub"), str(self.source))
        self.assertEqual(local.read_bytes(), before)

    def test_target_cannot_hide_tines_private_config(self) -> None:
        (self.root / tine.HOME / "nested").mkdir(parents=True)
        for target in (self.root / tine.HOME, self.root / tine.HOME / "nested"):
            with self.subTest(target=target), self.assertRaisesRegex(SystemExit, "reserved for tine"):
                self.mount("add", str(target), str(self.source))

    def test_target_cannot_contain_whitespace_or_commas(self) -> None:
        for name in ("sub dir", "sub,dir", "sub\tdir"):
            (self.root / name).mkdir()
            with self.subTest(target=name), self.assertRaisesRegex(SystemExit, "whitespace or commas"):
                self.mount("add", str(self.root / name), str(self.source))

    def test_add_rejects_a_project_daemon_buster(self) -> None:
        (self.root / ".buckconfig").write_text(
            f"[cells]\nroot = .\nsub = sub\n[buck2]\n{tine.DAEMON_BUSTER} = project-owned\n"
        )
        with self.assertRaisesRegex(SystemExit, "daemon_buster is reserved"):
            self.mount("add", str(self.root / "sub"), str(self.source))
        self.assertEqual(self.declared(), {})

    def test_add_rejects_overlapping_mounts(self) -> None:
        (self.root / "sub" / "inner").mkdir()
        for existing, target in (("sub", "sub/inner"), ("sub/inner", "sub")):
            with self.subTest(existing=existing, target=target):
                self.mount("add", str(self.root / existing), str(self.source))
                with self.assertRaisesRegex(SystemExit, "overlaps mount"):
                    self.mount("add", str(self.root / target), str(self.source))
                self.assertEqual(self.declared(), {existing: str(self.source)})
                self.mount("remove", str(self.root / existing))

    def test_target_must_be_inside_the_project(self) -> None:
        with self.assertRaisesRegex(SystemExit, "outside the project"):
            self.mount("add", str(self.source), str(self.source))

    def test_target_cannot_be_the_project_root(self) -> None:
        with self.assertRaisesRegex(SystemExit, "project root"):
            self.mount("add", str(self.root), str(self.source))

    def test_unknown_target_is_rejected(self) -> None:
        with (
            unittest.mock.patch.object(tine, "graph_mount_targets", return_value=set()),
            self.assertRaisesRegex(SystemExit, "not a valid target.*tine mount list"),
        ):
            self.mount("add", str(self.root / "missing"), str(self.source))
        self.assertFalse((self.root / "missing").exists())

    def test_declared_checkout_target_is_created(self) -> None:
        with unittest.mock.patch.object(
            tine,
            "graph_mount_targets",
            return_value={"missing"},
        ):
            self.mount("add", str(self.root / "missing"), str(self.source))
        self.assertTrue((self.root / "missing").is_dir())
        self.assertEqual(self.declared(), {"missing": str(self.source)})

    def test_source_must_be_a_directory(self) -> None:
        with self.assertRaisesRegex(SystemExit, "is not a directory"):
            self.mount("add", str(self.root / "sub"), str(self.source / "missing"))

    def test_toml_quotes_a_source_buckconfig_could_not_represent(self) -> None:
        awkward = self.source / "trailing\\"
        awkward.mkdir()
        self.mount("add", str(self.root / "sub"), str(awkward))
        self.assertEqual(self.declared(), {"sub": str(awkward)})

    def test_toml_quotes_targets_buckconfig_could_not_represent(self) -> None:
        for name in ("a=b", "[weird]", "<weird>"):
            (self.root / name).mkdir()
            with unittest.mock.patch.object(tine, "graph_mount_targets", return_value={name}):
                self.mount("add", str(self.root / name), str(self.source))
            self.assertEqual(self.declared(), {name: str(self.source)})
            self.mount("remove", str(self.root / name))

    def test_target_cannot_be_a_symlink(self) -> None:
        # The kernel would apply the bind mount to the symlink's destination.
        (self.root / "link").symlink_to(self.root / "sub")
        with self.assertRaisesRegex(SystemExit, "target is a symlink"):
            self.mount("add", str(self.root / "link"), str(self.source))

    def test_source_must_be_outside_the_project(self) -> None:
        (self.root / "inside").mkdir()
        with self.assertRaisesRegex(SystemExit, "must be outside the project"):
            self.mount("add", str(self.root / "sub"), str(self.root / "inside"))


class TestMountRemove(MountTestCase):
    def test_remove_restores_the_project_directory(self) -> None:
        self.mount("add", str(self.root / "sub"), str(self.source))
        report = self.mount("remove", str(self.root / "sub"))
        self.assertEqual(report, "tine: sub is no longer mounted\n")
        self.assertEqual(self.declared(), {})
        self.assertEqual(tine.local_mounts(self.root), {})

    def test_remove_preserves_other_mounts(self) -> None:
        (self.root / "other").mkdir()
        self.mount("add", str(self.root / "sub"), str(self.source))
        self.mount("add", str(self.root / "other"), str(self.source))
        self.mount("remove", str(self.root / "sub"))
        self.assertEqual(set(self.declared()), {"other"})

    def test_remove_rejects_an_unknown_mount(self) -> None:
        with self.assertRaisesRegex(SystemExit, "sub is not mounted"):
            self.mount("remove", str(self.root / "sub"))

    def test_remove_accepts_a_stale_declaration(self) -> None:
        # Removing a mount must not require its missing source to validate.
        declare(self.root, {"sub": "/nonexistent/source"})
        self.mount("remove", str(self.root / "sub"))
        self.assertEqual(self.declared(), {})


class TestMountList(MountTestCase):
    def test_list_prints_mount_targets_and_sources(self) -> None:
        (self.root / "other").mkdir()
        self.mount("add", str(self.root / "sub"), str(self.source))
        stdout = io.StringIO()
        with (
            unittest.mock.patch.object(
                tine,
                "graph_mount_targets",
                return_value={"checkout", "sub"},
            ),
            contextlib.redirect_stdout(stdout),
        ):
            report = self.mount("list")
        self.assertEqual(
            stdout.getvalue(),
            f"TARGET   SOURCE\ncheckout default\nother    default\nsub      {self.source}\n",
        )
        self.assertEqual(report, "")

    def test_graph_targets_come_from_labeled_buck_targets(self) -> None:
        output = "root//:top.git\nroot//packages/hello:hello.git\n"
        proc = subprocess.CompletedProcess([], 0, stdout=output)
        with unittest.mock.patch("subprocess.run", return_value=proc) as run:
            targets = tine.graph_mount_targets(self.root)
        self.assertEqual(
            targets,
            {"top", "packages/hello/hello"},
        )
        self.assertIn(tine.MOUNT_TARGET_LABEL, run.call_args.args[0][-1])


class TestDeclaredMounts(MountTestCase):
    """Invalid declarations stop the command instead of falling back to project contents."""

    def test_missing_source_is_rejected(self) -> None:
        declare(self.root, {"sub": "/nonexistent/source"})
        with self.assertRaisesRegex(SystemExit, "is not a directory; `tine mount remove sub`"):
            self.declared()

    def test_missing_target_is_rejected(self) -> None:
        declare(self.root, {"gone": str(self.source)})
        with self.assertRaisesRegex(SystemExit, "no such directory; `tine mount remove gone`"):
            self.declared()

    def test_parent_target_is_rejected(self) -> None:
        declare(self.root, {"../elsewhere": str(self.source)})
        with self.assertRaisesRegex(SystemExit, "target must be a project-relative path"):
            self.declared()

    def test_absolute_target_is_rejected(self) -> None:
        declare(self.root, {str(self.root / "sub"): str(self.source)})
        with self.assertRaisesRegex(SystemExit, "target must be a project-relative path"):
            self.declared()

    def test_relative_source_is_rejected(self) -> None:
        declare(self.root, {"sub": "../elsewhere"})
        with self.assertRaisesRegex(SystemExit, "source must be an absolute path"):
            self.declared()

    def test_reserved_target_is_rejected(self) -> None:
        (self.root / tine.HOME).mkdir()
        declare(self.root, {tine.HOME: str(self.source)})
        with self.assertRaisesRegex(SystemExit, "reserved for tine"):
            self.declared()

    def test_overlapping_mounts_are_rejected(self) -> None:
        # The outer mount can hide the inner target, making the result order-dependent.
        (self.root / "sub" / "inner").mkdir()
        declare(self.root, {"sub": str(self.source), "sub/inner": str(self.source)})
        with self.assertRaisesRegex(SystemExit, "overlaps mount"):
            self.declared()

    def test_target_that_becomes_a_symlink_is_rejected(self) -> None:
        # A target can become a symlink after the declaration was written.
        elsewhere = scratch(self).resolve()
        declare(self.root, {"sub": str(self.source)})
        (self.root / "sub").rmdir()
        (self.root / "sub").symlink_to(elsewhere)
        with self.assertRaisesRegex(SystemExit, "target is a symlink.*`tine mount remove sub`"):
            self.declared()

    def test_symlink_target_can_still_be_removed(self) -> None:
        elsewhere = scratch(self).resolve()
        declare(self.root, {"sub": str(self.source)})
        (self.root / "sub").rmdir()
        (self.root / "sub").symlink_to(elsewhere)
        self.mount("remove", str(self.root / "sub"))
        self.assertEqual(self.declared(), {})

    def test_hand_written_internal_source_is_rejected(self) -> None:
        # An internal source can itself be covered by a mount, making its digest namespace-dependent.
        (self.root / "inside").mkdir()
        declare(self.root, {"sub": str(self.root / "inside")})
        with self.assertRaisesRegex(SystemExit, "must be outside the project"):
            self.declared()

    def test_a_project_with_no_block_to_read(self) -> None:
        self.assertEqual(self.declared(), {})

    def test_local_mount_values_must_be_strings(self) -> None:
        (self.root / tine.LOCAL_CONFIG).write_text("[mounts]\nsub = 1\n")
        with self.assertRaisesRegex(SystemExit, "must contain only strings"):
            self.declared()

    def test_the_mount_file_rejects_unrelated_local_settings(self) -> None:
        (self.root / tine.LOCAL_CONFIG).write_text('[commands.check]\nsteps = [["buck"]]\n')
        with self.assertRaisesRegex(SystemExit, "owned by `tine mount`.*unsupported keys"):
            self.declared()


class TestMountDigest(MountTestCase):
    """Mount digests identify equivalent namespaces to Buck's daemon constraint."""

    def digest(self, mounts: dict[str, str]) -> str | None:
        return tine.mount_digest(mounts, (self.root / ".buckconfig").read_bytes())

    def test_no_mounts_need_no_digest(self) -> None:
        self.assertIsNone(self.digest({}))

    def test_replaced_source_changes_the_digest(self) -> None:
        # A stale namespace retains the old directory even when its path is reused.
        first, second = self.source / "a", self.source / "b"
        first.mkdir()
        second.mkdir()
        before = self.digest({"sub": str(first)})
        first.rmdir()
        second.rename(first)
        self.assertNotEqual(self.digest({"sub": str(first)}), before)

    def test_target_changes_the_digest(self) -> None:
        self.assertNotEqual(
            self.digest({"sub": str(self.source)}),
            self.digest({"other": str(self.source)}),
        )

    def test_root_config_changes_the_digest(self) -> None:
        before = self.digest({"sub": str(self.source)})
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\nsub = elsewhere\n")
        self.assertNotEqual(self.digest({"sub": str(self.source)}), before)

    def test_unreadable_source_is_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "cannot read"):
            tine.mount_digest({"sub": "/nonexistent/source"}, b"")


class TestNamespaces(MountTestCase):
    """Create and inspect bind mounts in child-process namespaces."""

    def answer(self, work: collections.abc.Callable[[], str]) -> str:
        """Run namespace-changing work in a child and return its result."""
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                os.close(read)
                with contextlib.redirect_stderr(io.StringIO()):
                    answer = work()
                os.write(write, answer.encode())
            except BaseException:
                code = 1
            os._exit(code)
        os.close(write)
        with os.fdopen(read, "rb") as pipe:
            answer = pipe.read().decode()
        self.assertEqual(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]), 0)
        return answer

    def create(self, mounts: dict[str, str]) -> None:
        buckconfig = (self.root / ".buckconfig").read_bytes()
        digest = tine.mount_digest(mounts, buckconfig)
        assert digest is not None
        tine.create(self.root, mounts, digest, buckconfig)

    @override
    def setUp(self) -> None:
        super().setUp()
        (self.root / "sub" / "witness").write_text("the project's own")
        (self.source / "witness").write_text("the mounted directory's")

    def test_bind_mount_replaces_target_contents(self) -> None:
        def mounted() -> str:
            self.create({"sub": str(self.source)})
            return (self.root / "sub" / "witness").read_text()

        self.assertEqual(self.answer(mounted), "the mounted directory's")
        # The child's mount must not propagate back into the test process.
        self.assertEqual((self.root / "sub" / "witness").read_text(), "the project's own")

    def test_user_namespace_maps_only_the_caller(self) -> None:
        def mapped() -> str:
            self.create({"sub": str(self.source)})
            return Path("/proc/self/uid_map").read_text()

        lines = self.answer(mapped).splitlines()
        uid = str(os.getuid())
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].split(), [uid, uid, "1"])

    def test_all_declared_mounts_are_applied(self) -> None:
        (self.root / "other").mkdir()
        other = scratch(self).resolve()
        (other / "witness").write_text("the other one's")

        def mounted() -> str:
            self.create({"sub": str(self.source), "other": str(other)})
            return (self.root / "other" / "witness").read_text()

        self.assertEqual(self.answer(mounted), "the other one's")

    def test_mount_error_names_its_target(self) -> None:
        def mounted() -> str:
            with self.assertRaisesRegex(SystemExit, "mount sub: cannot build it from"):
                tine.create(
                    self.root,
                    {"sub": "/nonexistent/source"},
                    "digest",
                    (self.root / ".buckconfig").read_bytes(),
                )
            return "refused"

        self.assertEqual(self.answer(mounted), "refused")

    def test_symlinked_root_config_is_rejected(self) -> None:
        original = self.root / ".buckconfig"
        shared = scratch(self).resolve() / ".buckconfig"
        shared.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(shared)

        def mounted() -> str:
            with self.assertRaisesRegex(
                SystemExit, "is a symlink, so the daemon constraint cannot be bound over it"
            ):
                self.create({"sub": str(self.source)})
            return "refused"

        self.assertEqual(self.answer(mounted), "refused")

    def test_constrained_config_carries_the_mount_table(self) -> None:
        mounts = {"sub/inner": str(self.source), "other": str(self.source)}
        written = tine.constrained_config(b"[cells]\nroot = .\n", "0123456789abcdef", mounts).decode()
        self.assertTrue(written.startswith("[cells]\nroot = .\n"))
        self.assertIn(f"[buck2]\n{tine.DAEMON_BUSTER} = {tine.BUSTER_PREFIX}0123456789abcdef\n", written)
        # Sorted, so the snapshot is stable for the same table however it was declared.
        self.assertTrue(written.endswith(f"[{tine.SECTION}]\n{tine.MOUNTS} = other, sub/inner\n"))

    def test_root_config_is_constrained_only_inside_the_namespace(self) -> None:
        mounts = {"sub": str(self.source)}
        buckconfig = (self.root / ".buckconfig").read_bytes()
        digest = tine.mount_digest(mounts, buckconfig)
        assert digest is not None

        def mounted() -> str:
            tine.create(self.root, mounts, digest, buckconfig)
            return (self.root / ".buckconfig").read_text()

        self.assertEqual(
            self.answer(mounted),
            tine.constrained_config(buckconfig, digest, mounts).decode(),
        )
        self.assertEqual((self.root / ".buckconfig").read_bytes(), buckconfig)
        self.assertFalse((self.root / tine.PRIVATE_CONFIG).exists())


class TestEntrypoint(MountTestCase):
    """Select the command to run after entering the namespace."""

    def config(self, cell: str) -> dict[str, dict[str, str]]:
        return {"cells": {"root": ".", tine.CELL: cell}}

    def command(self) -> Path:
        path = self.root / "sub" / tine.COMMAND
        path.parent.mkdir(parents=True)
        path.write_text("")
        return path

    def test_mounted_cell_uses_its_own_command(self) -> None:
        command = self.command()
        mounts = {"sub": str(self.source)}
        self.assertEqual(tine.entrypoint(self.root, self.config("sub"), mounts), command)

    def test_unmounted_cell_keeps_current_command(self) -> None:
        self.command()
        mounts = {"other": str(self.source)}
        entrypoint = tine.entrypoint(self.root, self.config("sub"), mounts)
        self.assertEqual(entrypoint, TOOL_PATH.absolute())

    def test_mounted_cell_requires_a_command(self) -> None:
        # Falling back would combine the outer command with mounted rules.
        with self.assertRaisesRegex(SystemExit, "mounted tine cell has no bin/tine"):
            tine.entrypoint(self.root, self.config("sub"), {"sub": str(self.source)})

    def test_mount_covering_cell_uses_mounted_command(self) -> None:
        # Keep the rules and Buck2 pin from the same checkout.
        path = self.root / "sub" / "vendor" / tine.COMMAND
        path.parent.mkdir(parents=True)
        path.write_text("")
        config = self.config("sub/vendor")
        self.assertEqual(tine.entrypoint(self.root, config, {"sub": str(self.source)}), path)

    def test_shared_path_prefix_does_not_cover_cell(self) -> None:
        self.command()
        config = self.config("subsidiary")
        (self.root / "subsidiary").mkdir()
        entrypoint = tine.entrypoint(self.root, config, {"sub": str(self.source)})
        self.assertEqual(entrypoint, TOOL_PATH.absolute())

    def test_project_without_tine_cell_keeps_current_command(self) -> None:
        entrypoint = tine.entrypoint(self.root, {"cells": {"root": "."}}, {"sub": str(self.source)})
        self.assertEqual(entrypoint, TOOL_PATH.absolute())

    def test_empty_cells_do_not_fall_back_to_repositories(self) -> None:
        self.command()
        config = {"cells": {}, "repositories": {tine.CELL: "sub"}}
        entrypoint = tine.entrypoint(self.root, config, {"sub": str(self.source)})
        self.assertEqual(entrypoint, TOOL_PATH.absolute())


class TestEnter(MountTestCase):
    """Choose and enter a namespace without performing real namespace operations."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.made: list[object] = []
        created = unittest.mock.patch.object(
            tine, "create", side_effect=lambda *args: self.made.append(args)
        )
        created.start()
        self.addCleanup(created.stop)
        patched = unittest.mock.patch.dict(os.environ)
        patched.start()
        os.environ.pop("BUCK2_BINARY", None)
        os.environ.pop(tine.MARKER, None)
        self.addCleanup(patched.stop)

    @contextlib.contextmanager
    def running(self) -> collections.abc.Iterator[list[object]]:
        execve: list[object] = []
        with unittest.mock.patch.object(os, "execve", side_effect=lambda *a: execve.extend(a)):
            yield execve

    def declare_one(self) -> str:
        self.mount("add", str(self.root / "sub"), str(self.source))
        digest = tine.mount_digest(self.declared(), (self.root / ".buckconfig").read_bytes())
        assert digest is not None
        return digest

    def test_no_mounts_need_no_namespace(self) -> None:
        with self.running() as execve:
            tine.enter(self.root, {}, ["buck", "build"])
        self.assertEqual((self.made, execve), ([], []))

    def test_buck_child_process_is_already_inside(self) -> None:
        self.declare_one()
        with (
            unittest.mock.patch.dict(os.environ, {"BUCK2_BINARY": "/somewhere/buck2"}),
            self.running() as execve,
        ):
            tine.enter(self.root, {}, ["buck", "build"])
        self.assertEqual((self.made, execve), ([], []))

    def test_matching_current_namespace_needs_no_handover(self) -> None:
        digest = self.declare_one()
        config = {"buck2": {tine.DAEMON_BUSTER: f"{tine.BUSTER_PREFIX}{digest}"}}
        with (
            unittest.mock.patch.dict(os.environ, {tine.MARKER: tine.marker(digest)}),
            self.running() as execve,
        ):
            tine.enter(self.root, config, ["buck", "build"])
        self.assertEqual((self.made, execve), ([], []))

    def test_stale_marker_does_not_skip_namespace_creation(self) -> None:
        # A manually exported or inherited marker does not prove that this process is in its namespace.
        digest = self.declare_one()
        with (
            unittest.mock.patch.dict(os.environ, {tine.MARKER: f"{digest} wrong-namespace"}),
            self.running() as execve,
        ):
            tine.enter(self.root, {}, ["buck", "build"])
        self.assertEqual(len(self.made), 1)
        self.assertTrue(execve)

    def test_mounts_create_an_equivalent_namespace_and_handover(self) -> None:
        digest = self.declare_one()
        buckconfig = (self.root / ".buckconfig").read_bytes()
        with self.running() as execve:
            tine.enter(self.root, {}, ["buck", "build"])
        self.assertEqual(self.made, [(self.root, {"sub": str(self.source)}, digest, buckconfig)])
        _, argv, environment = execve
        self.assertEqual(argv, [str(TOOL_PATH.absolute()), "buck", "build"])
        assert isinstance(environment, dict)
        self.assertEqual(environment[tine.MARKER], tine.marker(digest))

    def test_project_daemon_buster_is_rejected(self) -> None:
        self.declare_one()
        config = {"buck2": {tine.DAEMON_BUSTER: "project-owned"}}
        with self.running() as execve:
            with self.assertRaisesRegex(SystemExit, "daemon_buster is reserved"):
                tine.enter(self.root, config, ["buck", "build"])
        self.assertEqual((self.made, execve), ([], []))


class TestBuck2(unittest.TestCase):
    """Resolving the pin, short of fetching anything."""

    def pin(self, sha256: str = "a" * 64) -> dict[str, object]:
        platform = tine._platform()
        return {
            "buck2": {
                "repository": "example/buck2",
                "release": "2026-01-01",
                "platforms": {platform: {"artifact": "buck2.zst", "sha256": sha256}},
            },
        }

    def cache(self, sha256: str) -> Path:
        home = scratch(self)
        binary = home / "tine" / "buck2" / sha256 / "buck2"
        binary.parent.mkdir(parents=True)
        binary.write_text("")
        return home

    def cell(self, spec: object | None = None) -> Path:
        cell = scratch(self, "tine-test-cell.")
        pins(cell, spec)
        return cell

    def test_the_cell_declares_the_pin(self) -> None:
        home = self.cache("a" * 64)
        with unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(home)}):
            self.assertEqual(tine.buck2({}, self.cell()), home / "tine" / "buck2" / ("a" * 64) / "buck2")

    def test_a_project_pin_overrides_the_cell_key_by_key(self) -> None:
        platform = tine._platform()
        home = self.cache("b" * 64)
        config = {"buck2": {"platforms": {platform: {"sha256": "b" * 64}}}}
        with unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(home)}):
            binary = tine.buck2(config, self.cell())
        self.assertEqual(binary, home / "tine" / "buck2" / ("b" * 64) / "buck2")

    def test_the_override_is_read_from_tine_toml(self) -> None:
        platform = tine._platform()
        root = scratch(self)
        (root / tine.CONFIG).write_text(
            f'''[buck2.platforms."{platform}"]
sha256 = "{"b" * 64}"
'''
        )
        self.assertEqual(
            tine.configured_pin(tine.project_settings(root), platform),
            {"sha256": "b" * 64},
        )

    def test_override_values_have_their_structured_type(self) -> None:
        with self.assertRaisesRegex(SystemExit, "sha256.*must be a non-empty string"):
            tine.configured_pin(
                {"buck2": {"platforms": {tine._platform(): {"sha256": 1}}}},
                tine._platform(),
            )

    def test_unknown_override_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "unsupported keys: repositroy"):
            tine.configured_pin({"buck2": {"repositroy": "example/buck2"}}, tine._platform())

    def test_unknown_override_platforms_are_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "unsupported platforms: Linux-x86-64"):
            tine.configured_pin(
                {"buck2": {"platforms": {"Linux-x86-64": {"sha256": "a" * 64}}}},
                tine._platform(),
            )

    def test_every_platform_override_is_validated(self) -> None:
        other = next(platform for platform in tine.PLATFORMS if platform != tine._platform())
        with self.assertRaisesRegex(SystemExit, rf"{other}.*unsupported keys: sh256"):
            tine.configured_pin(
                {"buck2": {"platforms": {other: {"sh256": "a" * 64}}}},
                tine._platform(),
            )

    def test_every_platform_hash_is_validated(self) -> None:
        other = next(platform for platform in tine.PLATFORMS if platform != tine._platform())
        with self.assertRaisesRegex(SystemExit, rf"{other}.*64 lowercase hexadecimal"):
            tine.configured_pin(
                {"buck2": {"platforms": {other: {"sha256": "abc"}}}},
                tine._platform(),
            )

    def test_an_uncached_pin_is_nothing_to_complete_with(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(scratch(self))}):
            self.assertIsNone(tine.buck2(self.pin(), self.cell(), fetch=False))

    def test_a_cell_declaring_no_buck2(self) -> None:
        with self.assertRaisesRegex(SystemExit, "declares no Buck2"):
            tine.buck2({}, self.cell({"ruff": {}}))

    def test_a_cell_with_no_tools_json(self) -> None:
        with self.assertRaisesRegex(SystemExit, "cannot read .*tools.json"):
            tine.buck2({}, scratch(self, "tine-test-cell."))

    def test_a_machine_buck2_is_not_published_for(self) -> None:
        uname = os.uname_result(("Linux", "host", "7.0", "#1", "m68k"))
        with unittest.mock.patch.object(os, "uname", return_value=uname):
            with self.assertRaisesRegex(SystemExit, "unsupported platform: Linux-m68k"):
                tine.buck2(self.pin(), scratch(self, "tine-test-cell."))

    def test_a_pin_that_is_not_a_sha256(self) -> None:
        with self.assertRaisesRegex(SystemExit, "64 lowercase hexadecimal"):
            tine.buck2(self.pin("abc"), self.cell())


class TestDownload(unittest.TestCase):
    """The pin is what makes an unverified binary safe to run, so it is checked before it is kept."""

    def artifact(self, content: bytes) -> str:
        path = scratch(self) / "buck2.zst"
        from compression import zstd

        path.write_bytes(zstd.compress(content))
        return path.as_uri()

    def test_a_download_that_matches_its_pin(self) -> None:
        import hashlib

        url = self.artifact(b"binary")
        digest = hashlib.sha256(Path(url.removeprefix("file://")).read_bytes()).hexdigest()
        into = scratch(self) / "cache"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tine._download(url, digest, into).read_bytes(), b"binary")

    def test_a_download_that_does_not(self) -> None:
        into = scratch(self) / "cache" / ("a" * 64)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "not the pinned"):
                tine._download(self.artifact(b"binary"), "a" * 64, into)
        # Nothing half-verified is left where the next command would take it for the binary, and
        # nothing at all under the name the pin is cached by.
        self.assertFalse(into.exists())

    def test_an_artifact_that_is_not_what_the_pin_says_it_is(self) -> None:
        import hashlib
        from compression import zstd

        # Wrong magic, and a stream that ends early: a pin can match either.
        for payload in (b"not zstd", zstd.compress(b"binary")[:-4]):
            path = scratch(self) / "buck2.zst"
            path.write_bytes(payload)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(SystemExit, "not a whole zstd stream"):
                    tine._download(path.as_uri(), hashlib.sha256(payload).hexdigest(), scratch(self))


class TestBuck(unittest.TestCase):
    """The verb every keypress in a completing shell reaches."""

    @override
    def setUp(self) -> None:
        self.root = scratch(self)
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\n")
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)
        self.binary = self.root / "buck2"
        self.binary.write_text("")

    @contextlib.contextmanager
    def running(self) -> collections.abc.Iterator[list[object]]:
        execve: list[object] = []
        with unittest.mock.patch.object(tine, "buck2", return_value=self.binary):
            with unittest.mock.patch.object(os, "execve", side_effect=lambda *a: execve.extend(a)):
                yield execve

    def test_it_configures_then_hands_over(self) -> None:
        with self.running() as execve:
            tine.buck(["build", "//x"])
        binary, argv, environment = execve
        self.assertEqual((binary, argv), (self.binary, [str(self.binary), "build", "//x"]))
        assert isinstance(environment, dict)
        self.assertEqual(environment["BUCK2_BINARY"], str(self.binary))
        self.assertEqual(environment["BUCK2_ARG0"], "tine buck")
        self.assertTrue((self.root / tine.LOCAL).is_file())

    def test_completing_writes_nothing(self) -> None:
        # A keypress must not race a build that is writing the configuration it is completing from.
        with self.running() as execve:
            tine.buck(["complete", "--target=//x"])
        self.assertTrue(execve)
        self.assertFalse((self.root / tine.LOCAL).exists())

    def test_a_run_without_a_home_gets_one(self) -> None:
        environment = dict(os.environ)
        environment.pop("HOME", None)
        with unittest.mock.patch.dict(os.environ, environment, clear=True):
            with self.running() as execve:
                tine.buck(["build", "//x"])
            self.assertEqual(os.environ["HOME"], str(self.root / tine.HOME))
        home = execve[2]
        assert isinstance(home, dict)
        self.assertEqual(home["HOME"], str(self.root / tine.HOME))
        self.assertTrue((self.root / tine.HOME).is_dir())

    def test_a_run_with_a_home_keeps_it(self) -> None:
        home = scratch(self, "tine-home.")
        with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
            with self.running():
                tine.buck(["build", "//x"])
            self.assertEqual(os.environ["HOME"], str(home))
        self.assertFalse((self.root / tine.HOME).exists())


CELL_BUCKCONFIG = """# Standalone root cell. Consuming projects supply their own; see README.md.

[cells]
# Named `tine`, not `root`, so same-cell labels read `tine//...` as in a consuming project.
tine = .
prelude = prelude
none = none

[cell_aliases]
config = prelude
toolchains = tine
# Satisfy the bundled prelude's internal platform alias.
fbsource = none

[external_cells]
prelude = bundled

[parser]
target_platform_detector_spec = target:tine//...->prelude//platforms:default

[project]
ignore = .git, **/buck-out, \\
    **/target

[buck2]
defer_write_actions = true
"""


class TestProjectBuckconfig(unittest.TestCase):
    """What a project's configuration takes from the cell's own, and what it decides for itself."""

    def written(self, cell: str = "tine") -> str:
        source = scratch(self) / ".buckconfig"
        source.write_text(CELL_BUCKCONFIG)
        return tine.project_buckconfig(source, cell)

    def config(self, cell: str = "tine") -> dict[str, dict[str, str]]:
        root = scratch(self, "tine-test-project.")
        (root / ".buckconfig").write_text(self.written(cell))
        return tine.project_config(root)

    def test_the_cell_is_a_path_in_the_project_rather_than_its_root(self) -> None:
        cells = self.config("vendor/tine")["cells"]
        self.assertEqual(cells["root"], ".")
        self.assertEqual(cells["tine"], "vendor/tine")
        # Cells the project has no say in are the cell's own, and the toolchain is reached through an
        # alias to this cell rather than a cell of the project's.
        self.assertEqual(cells["prelude"], "prelude")
        self.assertEqual(self.config("vendor/tine")["cell_aliases"]["toolchains"], "tine")

    def test_the_project_gets_a_platform_for_its_own_targets_too(self) -> None:
        detector = self.config()["parser"][tine.DETECTOR].split()
        self.assertEqual(
            detector, [f"target:{cell}//...->{tine.DEFAULT_PLATFORM}" for cell in ("root", "tine")]
        )

    def test_what_the_project_has_no_say_in_is_copied_with_its_comments(self) -> None:
        written = self.written()
        for kept in ("fbsource = none", "# Satisfy the bundled prelude", "**/target", "prelude = bundled"):
            self.assertIn(kept, written)
        # A value written across lines stays one, which is what Buck2 reads it as.
        self.assertEqual(self.config()["project"]["ignore"], ".git, **/buck-out, **/target")

    def test_what_the_cell_says_about_being_one_is_left_behind(self) -> None:
        written = self.written()
        self.assertNotIn("Standalone root cell", written)
        self.assertNotIn("Named `tine`, not `root`", written)
        self.assertIn("`tine init`", written.splitlines()[0])


class TestInit(unittest.TestCase):
    """The whole command, against a checkout that is a directory with a tools.json in it."""

    @override
    def setUp(self) -> None:
        isolate_git(self)
        self.into = scratch(self, "tine-test-project.")
        self.checkout = self.into / "tine"
        pins(self.checkout)
        (self.checkout / ".buckconfig").write_text(CELL_BUCKCONFIG)

    def init(self, *arguments: str) -> dict[str, dict[str, str]]:
        with contextlib.redirect_stderr(io.StringIO()):
            tine.init(self.into, list(arguments))
        return tine.project_config(self.into)

    def test_it_writes_a_project_buck_can_be_run_in(self) -> None:
        self.assertEqual(self.init("tine")["cells"]["tine"], "tine")
        self.assertEqual(sorted(p.name for p in self.into.iterdir()), [".buckconfig", ".gitignore", "tine"])
        gitignore = (self.into / ".gitignore").read_text()
        self.assertIn("/buck-out\n", gitignore)
        self.assertIn(f"/{tine.LOCAL}\n", gitignore)
        # The name an interrupted write leaves behind, which is nobody's to commit either.
        self.assertIn(f"/{tine.LOCAL}.*.tmp\n", gitignore)
        self.assertIn(f"/{tine.LOCAL_CONFIG}\n", gitignore)
        self.assertIn(f"/{tine.LOCAL_CONFIG}.*.tmp\n", gitignore)

    def test_the_checkout_it_is_part_of_is_the_one_it_writes(self) -> None:
        with unittest.mock.patch.object(tine, "cell_root", return_value=self.checkout):
            self.assertEqual(self.init()["cells"]["tine"], "tine")

    def test_a_checkout_outside_the_project(self) -> None:
        with self.assertRaisesRegex(SystemExit, "cannot be a cell of this project"):
            self.init(str(scratch(self)))
        self.assertFalse((self.into / ".buckconfig").exists())

    def test_a_directory_that_is_no_checkout_of_the_cell(self) -> None:
        (self.into / "elsewhere").mkdir()
        with self.assertRaisesRegex(SystemExit, "no checkout of the tine cell"):
            self.init("elsewhere")

    def test_it_refuses_to_overwrite_a_project(self) -> None:
        (self.into / ".buckconfig").write_text("")
        with self.assertRaisesRegex(SystemExit, "already exists"):
            self.init("tine")

    def test_it_leaves_a_file_the_project_already_has(self) -> None:
        (self.into / ".gitignore").write_text("mine\n")
        self.init("tine")
        self.assertEqual((self.into / ".gitignore").read_text(), "mine\n")

    def test_a_gitignore_it_may_not_write_is_excluded_in_the_checkout_instead(self) -> None:
        git("init", "--quiet", cwd=self.into)
        (self.into / ".gitignore").write_text("mine\n")
        self.init("tine")
        excluded = (self.into / ".git" / "info" / "exclude").read_text()
        for entry in tine.GITIGNORE.split():
            self.assertIn(f"{entry}\n", excluded)

    def test_it_takes_one_path(self) -> None:
        # `--help` is argparse's to answer, and neither it nor a second path writes a project.
        for arguments in (["one", "two"], ["--help"], ["--nope"]):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.init(*arguments)
        self.assertFalse((self.into / ".buckconfig").exists())


class TestExclude(RepositoryTestCase):
    """What a `.gitignore` this command may not write says, said where only the checkout reads it."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.checkout = self.repo
        self.path = self.checkout / ".git" / "info" / "exclude"

    def exclude(self, entries: str = tine.GITIGNORE) -> str:
        with contextlib.redirect_stderr(io.StringIO()):
            tine.exclude(self.checkout, entries)
        return self.path.read_text()

    def test_what_the_file_already_says_is_kept(self) -> None:
        self.path.write_text("theirs\n")
        self.assertTrue(self.exclude().startswith("theirs\n"))

    def test_an_entry_it_already_has_is_not_added_twice(self) -> None:
        self.exclude()
        before = self.path.read_text()
        self.assertEqual(self.exclude(), before)

    def test_the_entries_it_is_missing_are_the_ones_added(self) -> None:
        self.path.write_text("/buck-out\n")
        added = self.exclude("/buck-out\n/elsewhere\n")
        self.assertEqual(added.count("/buck-out\n"), 1)
        self.assertIn("/elsewhere\n", added)

    def test_a_worktree_is_excluded_in_the_checkout_it_belongs_to(self) -> None:
        # `.git` is a file there, and the exclude file is the one the whole repository shares.
        self.commit()
        worktree = scratch(self) / "tree"
        git("worktree", "add", "--quiet", str(worktree), "-b", "other", cwd=self.checkout)
        with contextlib.redirect_stderr(io.StringIO()):
            tine.exclude(worktree, "/buck-out\n")
        self.assertIn("/buck-out\n", self.path.read_text())

    def test_a_directory_that_is_not_a_checkout(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            tine.exclude(scratch(self), tine.GITIGNORE)


class TestProjectRoot(unittest.TestCase):
    def test_the_furthest_ancestor_wins(self) -> None:
        root = scratch(self)
        (root / ".buckconfig").write_text("")
        (root / "nested").mkdir()
        (root / "nested" / ".buckconfig").write_text("")
        self.assertEqual(tine.project_root(root / "nested"), root)

    def test_buckroot_stops_the_search(self) -> None:
        root = scratch(self)
        (root / ".buckconfig").write_text("")
        (root / "nested").mkdir()
        (root / "nested" / ".buckconfig").write_text("")
        (root / "nested" / ".buckroot").write_text("")
        self.assertEqual(tine.project_root(root / "nested"), root / "nested")

    def test_the_home_directory_is_never_the_root(self) -> None:
        # `~/.buckconfig` is a configuration override, which Buck refuses to take for a project.
        home = scratch(self)
        (home / ".buckconfig").write_text("")
        (home / "project").mkdir()
        (home / "project" / ".buckconfig").write_text("")
        with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(tine.project_root(home / "project"), home / "project")

    def test_an_unset_home_skips_no_directory(self) -> None:
        # Buck reads `$HOME` and nothing else, so with it unset there is no home directory to skip,
        # whatever `Path.home()` falls back to.
        root = scratch(self)
        (root / ".buckconfig").write_text("")
        environment = dict(os.environ)
        environment.pop("HOME", None)
        with unittest.mock.patch.dict(os.environ, environment, clear=True):
            with unittest.mock.patch.object(tine.Path, "home", return_value=root):
                self.assertEqual(tine.project_root(root), root)

    def test_no_buckconfig_at_all(self) -> None:
        with self.assertRaisesRegex(SystemExit, "no .buckconfig"):
            tine.project_root(scratch(self))


class TestCompletion(unittest.TestCase):
    """The rewriting of Buck2's script, against the anchors this reaches into it for."""

    # What the real script says where each rewrite lands; the rest of it is passed through.
    FISH = """# @generated by buck2
complete -c buck2 -n "__fish_buck2_needs_command" -f -a build
complete -c buck -w buck2
function __fish_buck2_needs_command
    set -l cmd (commandline -opc)
    set -e cmd[1]
    argparse -s (__fish_buck2_global_optspecs)
end
function __buck2_add_target_completions
    set -l subcommand (__buck2_subcommand $cmd)
    buck2 complete --target="$cur"
end
"""
    BASH = """# @generated by buck2
_BUCK_COMPLETE_BIN="${_BUCK_COMPLETE_BIN:-buck2}"
    complete -F _buck2 -o bashdefault -o default buck2
    complete -F __buck2_fix -o bashdefault -o default buck
"""
    ZSH = """#compdef buck2 buck
# @generated by buck2
_buck2() { _describe commands ;}
if [ "$funcstack[1]" = "_buck2" ]; then
    _buck2 "$@"
else
    compdef _buck2 buck2
fi
compdef -d buck2

_BUCK_COMPLETE_BIN="${_BUCK_COMPLETE_BIN:-buck2}"
compdef __buck2_fix buck buck2
"""

    def scripts(self) -> dict[str, str]:
        return {shell: tine.completion(getattr(self, shell.upper()), shell) for shell in tine.SHELLS}

    def test_every_verb_is_completed_by_every_shell(self) -> None:
        # Generated from VERBS, so a verb added there cannot be missed by a shell.
        with unittest.mock.patch.dict(tine.VERBS, {"newverb": "something else it does"}):
            scripts = self.scripts()
        for shell, script in scripts.items():
            self.assertNotIn(tine.VERBS_MARKER, script, shell)
            for verb in (*tine.VERBS, "newverb"):
                self.assertIn(verb, script, shell)

    def test_nothing_it_emits_still_names_buck2(self) -> None:
        # A surviving name completes against whatever buck2 the shell finds, the `buck` command
        # Buck2 registers for included.
        scripts = self.scripts()
        for shell, script in scripts.items():
            # `_tine_buck2` is what Buck2's own completer is called once this is done with it.
            self.assertNotIn("buck2", script.replace("_tine_buck2", ""), shell)
        self.assertNotIn("default buck\n", scripts["bash"])

    def test_target_completion_routes_back_through_this_command(self) -> None:
        # Left alone it would run whatever `buck2` is on PATH, against unwrapped configuration.
        for shell, script in self.scripts().items():
            self.assertIn("tine buck complete" if shell == "fish" else "(tine buck)", script, shell)

    def test_zsh_autoloads_this_command_rather_than_buck2(self) -> None:
        # `_tine` is the name a shell autoloads the file under, so it has to be the completer that
        # knows this command's verbs; Buck2's own entry point cannot stay where it sits either.
        script = tine.completion(self.ZSH, "zsh")
        self.assertEqual(script.count('if [ "$funcstack[1]" = "_tine" ]; then'), 1)
        self.assertIn('if [ "$funcstack[1]" = "_tine" ]; then\n    _tine "$@"', script)
        self.assertIn("_tine() {\n    local context state state_descr line", script)
        self.assertIn("_tine_buck2", script)
        # Buck2 unregisters the command before registering its own completer; both have to go.
        self.assertNotIn("compdef -d", script)

    def test_descriptions_are_quoted_the_way_each_shell_reads_them(self) -> None:
        description = r"report the project's health: \fast"
        configured = tine.validate_commands(
            {"report": {"description": description, "steps": [["buck", "build", "//..."]]}}
        )
        self.assertIn(
            r"-d 'report the project\'s health: \\fast'",
            tine.completion(self.FISH, "fish", configured),
        )
        self.assertIn(
            r"'report:report the project'\''s health\: \\fast'",
            tine.completion(self.ZSH, "zsh", configured),
        )

    def test_an_anchor_that_is_no_longer_there_is_reported(self) -> None:
        # Rewriting nothing would emit a script that silently completes nothing.
        for shell in tine.SHELLS:
            with self.assertRaisesRegex(SystemExit, "the pin moved under it"):
                tine.completion("# @generated by buck2\n", shell)

    def test_project_commands_are_completed_by_every_shell(self) -> None:
        configured = tine.validate_commands(
            {
                "smoke": {
                    "description": "run the smoke tests",
                    "steps": [["buck", "test", "//:smoke"]],
                }
            }
        )
        for shell, script in {
            shell: tine.completion(getattr(self, shell.upper()), shell, configured) for shell in tine.SHELLS
        }.items():
            self.assertIn("smoke", script, shell)
        for shell in ("fish", "zsh"):
            self.assertIn(
                "run the smoke tests", tine.completion(getattr(self, shell.upper()), shell, configured)
            )


class TestRunStep(unittest.TestCase):
    def process(self) -> unittest.mock.MagicMock:
        process = unittest.mock.MagicMock()
        process.__enter__.return_value = process
        return process

    def test_forwards_termination_and_restores_the_handler(self) -> None:
        process = self.process()
        previous = signal.getsignal(signal.SIGTERM)

        def wait() -> int:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            cast(collections.abc.Callable[[int, object], object], handler)(signal.SIGTERM, None)
            # A child may handle the signal and exit cleanly; the chain must still stop.
            return 0

        process.wait.side_effect = wait
        with unittest.mock.patch("subprocess.Popen", return_value=process):
            self.assertEqual(tine.run_step(["helper"]), -signal.SIGTERM)
        process.send_signal.assert_called_once_with(signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_forwards_an_interrupt_without_a_traceback(self) -> None:
        process = self.process()
        process.wait.side_effect = [KeyboardInterrupt, -signal.SIGINT]
        with unittest.mock.patch("subprocess.Popen", return_value=process):
            self.assertEqual(tine.run_step(["helper"]), -signal.SIGINT)
        process.send_signal.assert_called_once_with(signal.SIGINT)


class TestProjectCommands(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.addCleanup(os.chdir, Path.cwd())

    def project(self, content: str) -> Path:
        root = scratch(self, "tine-test-project.")
        (root / ".buckconfig").write_text("[cells]\nroot = .\n")
        (root / tine.CONFIG).write_text(content)
        return root

    def test_steps_run_from_the_configured_directory_and_the_last_gets_the_callers_arguments(
        self,
    ) -> None:
        root = self.project(
            """[commands.build]
cwd = "work"
steps = [
  ["buck", "build", "//:artifacts"],
  ["buck", "test", "//:sysext-duplicates"],
]
"""
        )
        work = root / "work"
        work.mkdir()
        nested = root / "nested"
        nested.mkdir()

        def run_step(_argv: list[str]) -> int:
            self.assertEqual(Path.cwd(), work)
            return 0

        def execv(_executable: str, _argv: list[str]) -> None:
            self.assertEqual(Path.cwd(), work)

        with (
            unittest.mock.patch.object(tine, "cwd", return_value=nested),
            unittest.mock.patch.object(tine, "run_step", side_effect=run_step) as run,
            unittest.mock.patch.object(os, "execv", side_effect=execv) as execute,
        ):
            tine.main(["build", "--show-output"])
        executable = str(tine.COMMAND_PATH.absolute())
        run.assert_called_once_with([executable, "buck", "build", "//:artifacts"])
        execute.assert_called_once_with(
            executable,
            [
                executable,
                "buck",
                "test",
                "//:sysext-duplicates",
                "--show-output",
            ],
        )

    def test_a_failed_step_stops_the_command(self) -> None:
        root = self.project(
            """[commands.check]
steps = [
  ["buck", "test", "//:first"],
  ["buck", "test", "//:second"],
]
"""
        )
        with (
            unittest.mock.patch.object(tine, "cwd", return_value=root),
            unittest.mock.patch.object(tine, "run_step", return_value=7) as run,
            self.assertRaises(SystemExit) as raised,
        ):
            tine.main(["check"])
        self.assertEqual(raised.exception.code, 7)
        run.assert_called_once()

    def test_a_step_killed_by_a_signal_uses_the_shell_exit_status(self) -> None:
        command = tine.validate_commands(
            {
                "check": {
                    "steps": [
                        ["buck", "first"],
                        ["buck", "second"],
                    ]
                }
            }
        )["check"]
        with (
            unittest.mock.patch.object(tine, "run_step", return_value=-signal.SIGTERM),
            self.assertRaises(SystemExit) as raised,
        ):
            tine.run_project_command(Path.cwd(), "check", command, [])
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)

    def test_a_final_step_execution_error_names_the_step(self) -> None:
        command = tine.validate_commands({"check": {"steps": [["buck", "build"]]}})["check"]
        error = FileNotFoundError("absent")
        with (
            unittest.mock.patch.object(os, "execv", side_effect=error),
            self.assertRaisesRegex(SystemExit, "cannot run command check step 1: absent"),
        ):
            tine.run_project_command(Path.cwd(), "check", command, [])

    def test_steps_are_argument_arrays_not_shell_command_lines(self) -> None:
        configured = tine.validate_commands(
            {"query": {"steps": [["buck", "uquery", "attrfilter(labels, one, //...)", "$HOME"]]}}
        )
        self.assertEqual(
            configured["query"].body,
            (("buck", "uquery", "attrfilter(labels, one, //...)", "$HOME"),),
        )
        self.assertEqual(configured["query"].description, tine.PROJECT_COMMAND_DESCRIPTION)

    def test_a_multiline_script_receives_literal_caller_arguments(self) -> None:
        root = self.project(
            r"""[commands.inline]
script = '''
set -eu
pwd
printf "<%s>\n" "$0" "$@"
'''
"""
        )
        nested = root / "nested"
        nested.mkdir()
        proc = subprocess.run(
            [str(TOOL_PATH), "inline", "one two", "$(false)"],
            cwd=nested,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        self.assertEqual(proc.stdout, f"{nested}\n<inline>\n<one two>\n<$(false)>\n")

    def test_a_script_failure_is_the_commands_exit_status(self) -> None:
        root = self.project('[commands.fail]\nscript = "exit 23"\n')
        proc = subprocess.run(
            [str(TOOL_PATH), "fail"],
            cwd=root,
            text=True,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 23)
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_shell_execution_error_names_the_command(self) -> None:
        command = tine.validate_commands({"check": {"script": "true"}})["check"]
        error = FileNotFoundError("absent")
        with (
            unittest.mock.patch.object(os, "execvp", side_effect=error),
            self.assertRaisesRegex(SystemExit, "cannot run command check: absent"),
        ):
            tine.run_project_command(Path.cwd(), "check", command, [])

    def test_an_unavailable_configured_working_directory_stops_the_command(self) -> None:
        root = scratch(self)
        directory = root / "absent"
        command = tine.validate_commands({"check": {"cwd": "absent", "script": "true"}})["check"]
        error = FileNotFoundError("absent")
        with (
            unittest.mock.patch.object(os, "chdir", side_effect=error),
            unittest.mock.patch.object(os, "execvp") as execute,
            self.assertRaisesRegex(SystemExit, rf"command check from {re.escape(str(directory))}: absent"),
        ):
            tine.run_project_command(root, "check", command, [])
        execute.assert_not_called()

    def test_cwd_must_be_a_non_empty_relative_path(self) -> None:
        for directory in ("", "/elsewhere", "contains\0nul", 1):
            with (
                self.subTest(directory=directory),
                self.assertRaisesRegex(SystemExit, "cwd.*non-empty relative path"),
            ):
                tine.validate_commands({"broken": {"cwd": directory, "script": "true"}})

    def test_a_command_defines_exactly_one_body(self) -> None:
        for definition in (
            {},
            {
                "script": "true",
                "steps": [["buck", "build", "//..."]],
            },
        ):
            with (
                self.subTest(definition=definition),
                self.assertRaisesRegex(SystemExit, "must define exactly one of steps or script"),
            ):
                tine.validate_commands({"broken": definition})

    def test_scripts_must_be_non_empty_strings(self) -> None:
        for script in ("", " \n\t", 1):
            with self.subTest(script=script), self.assertRaisesRegex(SystemExit, "script.*non-empty string"):
                tine.validate_commands({"broken": {"script": script}})

    def test_a_toml_script_cannot_contain_nul(self) -> None:
        root = self.project(
            r"""[commands.broken]
script = "\u0000"
"""
        )
        with (
            unittest.mock.patch.object(tine, "cwd", return_value=root),
            self.assertRaisesRegex(SystemExit, "script.*must not contain NUL"),
        ):
            tine.main(["broken"])

    def test_steps_must_be_non_empty_arrays_of_strings(self) -> None:
        invalid = (
            ({"steps": []}, "non-empty array"),
            ({"steps": [[]]}, "step 1.*non-empty array"),
            ({"steps": [["buck", 1]]}, "step 1.*only strings"),
            ({"steps": [["buck", "\0"]]}, "step 1.*must not contain NUL"),
        )
        for definition, message in invalid:
            with self.subTest(definition=definition), self.assertRaisesRegex(SystemExit, message):
                tine.validate_commands({"broken": definition})

    def test_command_names_are_safe_single_words(self) -> None:
        for name in ("two words", "$(command)", "-h"):
            with self.subTest(name=name), self.assertRaisesRegex(SystemExit, "invalid command name"):
                tine.validate_commands({name: {"steps": [["buck", "build", "//..."]]}})

    def test_reserved_command_names_are_rejected(self) -> None:
        for name in ("buck", "help"):
            with self.subTest(name=name), self.assertRaisesRegex(SystemExit, "reserved command name"):
                tine.validate_commands({name: {"steps": [["buck", "build", "//..."]]}})

    def test_only_buck_can_be_a_step(self) -> None:
        with self.assertRaisesRegex(SystemExit, "must start with buck, not mount"):
            tine.validate_commands({"broken": {"steps": [["mount", "list"]]}})

    def test_descriptions_must_be_non_empty_printable_strings(self) -> None:
        for description in ("", "   ", "two\nlines", 1):
            with (
                self.subTest(description=description),
                self.assertRaisesRegex(SystemExit, "description.*non-empty printable string"),
            ):
                tine.validate_commands(
                    {
                        "check": {
                            "description": description,
                            "steps": [["buck", "test", "//..."]],
                        }
                    }
                )

    def test_command_definitions_reject_unknown_keys(self) -> None:
        with self.assertRaisesRegex(SystemExit, "unsupported keys: summary"):
            tine.validate_commands(
                {
                    "check": {
                        "summary": "build and test the image",
                        "steps": [["buck", "test", "//..."]],
                    }
                }
            )

    def test_help_lists_project_commands(self) -> None:
        root = self.project(
            """[commands.check]
description = "build and test the image"
steps = [["buck", "test", "//..."]]
"""
        )
        printed = io.StringIO()
        with (
            unittest.mock.patch.object(tine, "cwd", return_value=root),
            contextlib.redirect_stdout(printed),
        ):
            tine.main(["help"])
        self.assertIn("    check       build and test the image\n", printed.getvalue())


class TestMain(unittest.TestCase):
    def usage(self, *argv: str) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            tine.main(list(argv))
        return stdout.getvalue()

    def test_it_prints_what_it_can_do(self) -> None:
        for argv in ([], ["help"], ["-h"], ["--help"]):
            self.assertEqual(self.usage(*argv), tine.USAGE)

    def test_asking_a_verb_for_help_does_not_do_its_job(self) -> None:
        # `init --help` would otherwise take `--help` for the checkout to register.
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), self.assertRaises(SystemExit):
            tine.main(["init", "--help"])
        self.assertIn("usage: tine init", printed.getvalue())

    def test_box_quietly_runs_the_root_box_target(self) -> None:
        with unittest.mock.patch.object(tine, "buck") as buck:
            tine.main(["box", "--", "pytest", "-q"])
        buck.assert_called_once_with(["-v", "0", "run", "//:box", "--", "pytest", "-q"])

    def test_no_such_command(self) -> None:
        with self.assertRaisesRegex(SystemExit, "no such command: nope"):
            tine.main(["nope"])

    def test_completion_takes_a_shell_it_can_write(self) -> None:
        with self.assertRaisesRegex(SystemExit, "completion takes one of"):
            tine.main(["completion", "tcsh"])


class TestBuckSubcommand(unittest.TestCase):
    """What Buck2 will make of the arguments `tine buck` forwards, which decides the fast path."""

    def test_plain(self) -> None:
        self.assertEqual(tine.buck_subcommand(["build", "//x"]), "build")

    def test_options_before_the_subcommand(self) -> None:
        self.assertEqual(tine.buck_subcommand(["-v", "0", "complete", "--target=x"]), "complete")

    def test_joined_option_values(self) -> None:
        self.assertEqual(tine.buck_subcommand(["--isolation-dir=x", "build"]), "build")

    def test_separated_option_values(self) -> None:
        self.assertEqual(tine.buck_subcommand(["--isolation-dir", "x", "build"]), "build")

    def test_no_subcommand(self) -> None:
        self.assertIsNone(tine.buck_subcommand(["--help"]))
        self.assertIsNone(tine.buck_subcommand([]))
        self.assertIsNone(tine.buck_subcommand(["--", "build"]))
