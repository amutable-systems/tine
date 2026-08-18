"""Tests for the wrapper's git query, buckconfig reading and argument handling.

    buck test tine//bin:test

Each test builds a real throwaway repository or checkout; the wrapper's git calls run for real.
"""

import collections.abc
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import override

TOOL_PATH = Path(__file__).parent / "tine"

# A command carries no suffix, so it needs the loader naming the language it is written in.
spec = importlib.util.spec_from_loader("tine", SourceFileLoader("tine", str(TOOL_PATH)))
assert spec and spec.loader
tine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tine)


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


def declare(root: Path, entries: str) -> None:
    """The generated block as an earlier command, or a hand, left it in `.buckconfig.local`."""
    (root / tine.LOCAL).write_text(f"{tine.BLOCK_BEGIN}\n{entries}{tine.BLOCK_END}\n")


def scratch(case: unittest.TestCase, prefix: str = "tine-test.") -> Path:
    tmp = tempfile.TemporaryDirectory(prefix=prefix)
    case.addCleanup(tmp.cleanup)
    return Path(tmp.name)


def checkout(case: unittest.TestCase) -> Path:
    path = scratch(case)
    git("init", "--quiet", cwd=path)
    return path


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

    def committer_epoch(self) -> int:
        return int(git("log", "-1", "--format=%ct", cwd=self.repo))


class TestComponents(RepositoryTestCase):
    def test_release(self) -> None:
        commit = self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.assertEqual(
            tine.components(self.repo),
            {"base": "1.2.3", "count": 0, "height": 1, "commit": commit, "seconds": None},
        )

    def test_snapshot_counts_commits(self) -> None:
        # The whole commit, never an abbreviation: how much of it fits is the renderer's question.
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.commit("second")
        commit = self.commit("third")
        self.assertEqual(
            tine.components(self.repo),
            {"base": "1.2.3", "count": 2, "height": 3, "commit": commit, "seconds": None},
        )

    def test_no_tag(self) -> None:
        commit = self.commit()
        self.assertEqual(
            tine.components(self.repo),
            {"base": "0.0.0", "count": 1, "height": 1, "commit": commit, "seconds": None},
        )

    def test_v_prefixed_word_is_not_a_version_tag(self) -> None:
        # `v*` would match this and derive the version `endor-drop-2024` from it.
        commit = self.commit()
        git("tag", "vendor-drop-2024", cwd=self.repo)
        self.assertEqual(
            tine.components(self.repo),
            {"base": "0.0.0", "count": 1, "height": 1, "commit": commit, "seconds": None},
        )

    def test_latest_tag_wins(self) -> None:
        self.commit()
        git("tag", "v1.0.0", cwd=self.repo)
        commit = self.commit("second")
        git("tag", "v2.0.0", cwd=self.repo)
        self.assertEqual(
            tine.components(self.repo),
            {"base": "2.0.0", "count": 0, "height": 2, "commit": commit, "seconds": None},
        )

    def test_tag_sharing_a_branch_name(self) -> None:
        # An unqualified revision would be ambiguous, which git resolves with a warning.
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        git("branch", "v1.2.3", cwd=self.repo)
        commit = self.commit("second")
        self.assertEqual(
            tine.components(self.repo),
            {"base": "1.2.3", "count": 1, "height": 2, "commit": commit, "seconds": None},
        )

    def test_dirty_tree_has_seconds(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        commit = self.commit("second")
        self.dirty()
        with unittest.mock.patch("time.time", return_value=self.committer_epoch() + 86400):
            self.assertEqual(
                tine.components(self.repo),
                {"base": "1.2.3", "count": 1, "height": 2, "commit": commit, "seconds": 86400},
            )

    def test_untracked_files_are_dirty_whatever_git_is_configured_to_show(self) -> None:
        # A dirty tree is never a release, even right at the tag.
        commit = self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        git("config", "status.showUntrackedFiles", "no", cwd=self.repo)
        self.dirty()
        with unittest.mock.patch("time.time", return_value=self.committer_epoch()):
            self.assertEqual(
                tine.components(self.repo),
                {"base": "1.2.3", "count": 0, "height": 1, "commit": commit, "seconds": 0},
            )

    def test_clock_skew_clamps(self) -> None:
        commit = self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.dirty()
        with unittest.mock.patch("time.time", return_value=self.committer_epoch() - 100):
            self.assertEqual(
                tine.components(self.repo),
                {"base": "1.2.3", "count": 0, "height": 1, "commit": commit, "seconds": 0},
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
                {"base": "1.2.3", "count": 0, "height": 1, "commit": commit, "seconds": None},
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
        self.assertIn("# no version components: not a git checkout", tine.generate(scratch(self), {}, {}))


class TestGenerate(RepositoryTestCase):
    def test_writes_every_component_it_has(self) -> None:
        commit = self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.assertEqual(
            tine.generate(self.repo, {}, {}),
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

    def test_seconds_appear_only_for_a_dirty_tree(self) -> None:
        self.commit()
        self.dirty()
        self.assertTrue(
            any(line.startswith("version-seconds = ") for line in tine.generate(self.repo, {}, {}))
        )

    def test_every_line_it_writes_is_buckconfig(self) -> None:
        # A checkout without commits fails `rev-parse` with three lines of git advice; all but the
        # first would land in the file as configuration, and Buck would refuse to parse it.
        (self.repo / ".buckconfig").write_text("")
        tine.refresh(self.repo, {})
        for line in (self.repo / tine.LOCAL).read_text().splitlines():
            self.assertTrue(not line or line.startswith("#") or "=" in line or line.startswith("["))
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
        # Buck2 reads `.buckconfig.d` whole, below `.buckconfig`, and a project may pin only there.
        root = scratch(self)
        (root / ".buckconfig.d" / "nested").mkdir(parents=True)
        (root / ".buckconfig").write_text("[cells]\nroot = .\n")
        (root / ".buckconfig.d" / "10-pins.bcfg").write_text("[tine]\nbuck2-release = old\n")
        (root / ".buckconfig.d" / "nested" / "20-cells.bcfg").write_text("[cells]\nsub = sub\n")
        config = tine.project_config(root)
        self.assertEqual(config[tine.SECTION]["buck2-release"], "old")
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


class TestLocalCheckout(unittest.TestCase):
    def test_a_plain_absolute_path(self) -> None:
        path = checkout(self)
        self.assertEqual(tine.local_checkout(str(path)), path)

    def test_a_file_url(self) -> None:
        path = checkout(self)
        self.assertEqual(tine.local_checkout(f"file://{path}"), path)

    def test_a_file_url_with_an_authority_and_an_escape(self) -> None:
        path = checkout(self)
        escaped = str(path).replace("/", "%2F", 1)
        self.assertEqual(tine.local_checkout(f"file://localhost{escaped}"), path)

    def test_a_home_relative_path(self) -> None:
        path = checkout(self)
        with unittest.mock.patch.dict(os.environ, {"HOME": str(path.parent)}):
            self.assertEqual(tine.local_checkout(f"~/{path.name}"), path)

    def test_a_remote_is_not_a_checkout(self) -> None:
        self.assertIsNone(tine.local_checkout("https://example.invalid/repo"))
        self.assertIsNone(tine.local_checkout("git@example.invalid:repo.git"))

    def test_a_relative_path_names_nothing_a_fetch_could_reach(self) -> None:
        self.assertIsNone(tine.local_checkout("../sibling"))

    def test_a_directory_that_is_not_a_repository(self) -> None:
        self.assertIsNone(tine.local_checkout(str(scratch(self))))

    def test_a_bare_repository(self) -> None:
        # Buck2 fetches from one of these too, and `init` is a plausible way to name one.
        path = scratch(self)
        git("init", "--quiet", "--bare", cwd=path)
        self.assertEqual(tine.local_checkout(str(path)), path)


class TestCheckoutAt(unittest.TestCase):
    """What an argument names, which is a path the caller typed rather than a recorded origin."""

    def test_a_path_relative_to_the_caller(self) -> None:
        path = checkout(self)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(path)
        self.assertEqual(tine.checkout_at(f"../{path.name}"), path.resolve())

    def test_a_home_relative_path(self) -> None:
        path = checkout(self)
        with unittest.mock.patch.dict(os.environ, {"HOME": str(path.parent)}):
            self.assertEqual(tine.checkout_at(f"~/{path.name}"), path.resolve())

    def test_what_is_not_a_path_at_all(self) -> None:
        # It would otherwise be resolved against the caller's directory and land somewhere real.
        self.assertIsNone(tine.checkout_at("https://example.invalid/repo"))
        self.assertIsNone(tine.checkout_at(""))


class CellTestCase(RepositoryTestCase):
    """`self.repo` is the checkout a cell is overridden with; `self.root` the project overriding it."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = scratch(self, "tine-test-project.")
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\nsub = sub\n")

    def cell(self, *arguments: str) -> str:
        """The command, reporting to a string rather than into the suite's output."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tine.cell(self.root, list(arguments))
        return stderr.getvalue()

    def declared(self) -> dict[str, tuple[str, str | None]]:
        return tine.declared_cells(self.root)

    def local(self) -> str:
        return (self.root / tine.LOCAL).read_text()


class TestCellOverride(CellTestCase):
    def test_a_cell_follows_the_head_of_the_checkout_it_is_overridden_with(self) -> None:
        commit = self.commit()
        report = self.cell("override", "sub", str(self.repo))
        self.assertEqual(report, f"tine: cell sub is {self.repo} at HEAD\n")
        self.assertEqual(self.declared(), {"sub": (str(self.repo), None)})
        self.assertIn(f"[external_cell_sub]\ngit_origin = {self.repo}\ncommit_hash = {commit}", self.local())
        # The entry too, so that a cell the project pins differently, or not at all, is overridable.
        self.assertIn("[external_cells]\nsub = git", self.local())

    def test_the_pin_moves_with_the_checkout(self) -> None:
        self.commit()
        self.cell("override", "sub", str(self.repo))
        second = self.commit("second")
        tine.refresh(self.root, tine.project_config(self.root))
        self.assertIn(f"commit_hash = {second}", self.local())

    def test_uncommitted_work_in_the_checkout_does_not_move_the_pin(self) -> None:
        commit = self.commit()
        self.dirty()
        self.cell("override", "sub", str(self.repo))
        self.assertIn(f"commit_hash = {commit}", self.local())

    def test_a_named_revision_stays_where_it_was_put(self) -> None:
        first = self.commit()
        git("tag", "v1.0.0", cwd=self.repo)
        self.cell("override", "sub", str(self.repo), "--commit", "v1.0.0")
        self.commit("second")
        tine.refresh(self.root, tine.project_config(self.root))
        self.assertEqual(self.declared(), {"sub": (str(self.repo), first)})
        self.assertIn(f"commit_hash = {first}", self.local())

    def test_a_revision_the_checkout_does_not_have(self) -> None:
        self.commit()
        with self.assertRaisesRegex(SystemExit, "has no revision v9"):
            self.cell("override", "sub", str(self.repo), "--commit", "v9")

    def test_a_relative_path_is_recorded_as_one_buck_can_fetch_from(self) -> None:
        commit = self.commit()
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.repo.parent)
        self.cell("override", "sub", self.repo.name)
        self.assertEqual(self.declared(), {"sub": (str(self.repo), None)})
        self.assertIn(f"commit_hash = {commit}", self.local())

    def test_an_alias_is_recorded_as_the_cell_it_resolves_to(self) -> None:
        self.commit()
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\nsub = sub\n[cell_aliases]\nalias = sub\n")
        self.cell("override", "alias", str(self.repo))
        self.assertEqual(set(self.declared()), {"sub"})

    def test_declaring_it_again_without_a_revision_goes_back_to_following(self) -> None:
        commit = self.commit()
        self.cell("override", "sub", str(self.repo), "--commit", commit)
        self.cell("override", "sub", str(self.repo))
        self.assertEqual(self.declared(), {"sub": (str(self.repo), None)})

    def test_a_cell_whose_name_cannot_be_configured(self) -> None:
        # Buck2 reads `[external_cell_x # y]` as a section marker it cannot parse.
        self.commit()
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\nx # y = sub\n")
        with self.assertRaisesRegex(SystemExit, "cannot be configured in a buckconfig"):
            self.cell("override", "x # y", str(self.repo))

    def test_a_cell_the_project_does_not_have(self) -> None:
        with self.assertRaisesRegex(SystemExit, "no cell named absent"):
            self.cell("override", "absent", str(self.repo))

    def test_the_project_itself_is_not_a_cell_to_override(self) -> None:
        # Buck2 answers this one with `External cell 'root' cannot have a nested cell`.
        self.commit()
        with self.assertRaisesRegex(SystemExit, "cell root is this project"):
            self.cell("override", "root", str(self.repo))

    def test_a_path_that_cannot_be_written_as_a_declaration(self) -> None:
        # A trailing backslash continues the line, swallowing the declaration written after it.
        self.commit()
        odd = scratch(self) / "cell\\"
        git("clone", "--quiet", str(self.repo), str(odd), cwd=self.repo)
        with self.assertRaisesRegex(SystemExit, "cannot be configured"):
            self.cell("override", "sub", str(odd))
        self.assertEqual(self.declared(), {})

    def test_a_path_that_is_not_a_checkout(self) -> None:
        with self.assertRaisesRegex(SystemExit, "is not a git repository"):
            self.cell("override", "sub", str(scratch(self)))

    def test_a_checkout_without_commits(self) -> None:
        with self.assertRaisesRegex(SystemExit, "has no commit to follow"):
            self.cell("override", "sub", str(self.repo))
        # The declaration does not outlive the failure to make it.
        self.assertEqual(self.declared(), {})


class TestCellRevert(CellTestCase):
    def test_the_cell_goes_back_to_what_the_project_pins(self) -> None:
        self.commit()
        self.cell("override", "sub", str(self.repo))
        self.cell("revert", "sub")
        self.assertEqual(self.declared(), {})
        self.assertNotIn("external_cell_sub", self.local())
        # Nothing declared, so nothing is left in the block to declare it.
        self.assertNotIn("cell-sub-commit", self.local())

    def test_only_the_cell_named_is_reverted(self) -> None:
        self.commit()
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\nsub = sub\nother = other\n")
        self.cell("override", "sub", str(self.repo))
        self.cell("override", "other", str(self.repo))
        self.cell("revert", "sub")
        self.assertEqual(set(self.declared()), {"other"})

    def test_a_cell_that_is_not_overridden(self) -> None:
        with self.assertRaisesRegex(SystemExit, "cell sub is not overridden"):
            self.cell("revert", "sub")

    def test_a_cell_the_project_no_longer_declares(self) -> None:
        self.commit()
        self.cell("override", "sub", str(self.repo))
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\n")
        self.cell("revert", "sub")
        self.assertEqual(self.declared(), {})


class TestCellList(CellTestCase):
    def listed(self) -> tuple[str, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            report = self.cell("list")
        return stdout.getvalue(), report

    def test_nothing_is_overridden(self) -> None:
        printed, report = self.listed()
        self.assertEqual(printed, "")
        self.assertIn("no cell is overridden", report)

    def test_what_each_cell_is_overridden_with(self) -> None:
        commit = self.commit()
        self.cell("override", "sub", str(self.repo))
        self.assertEqual(self.listed()[0], f"sub {self.repo} HEAD\n")
        self.cell("override", "sub", str(self.repo), "--commit", commit)
        self.assertEqual(self.listed()[0], f"sub {self.repo} {commit}\n")


class TestOverriddenCells(CellTestCase):
    """A declaration that cannot be honoured stops the command, rather than building something else."""

    def overridden(self) -> dict[str, tuple[str, str]]:
        config = tine.project_config(self.root)
        return tine.overridden_cells(config, tine.declared_cells(self.root))

    def test_a_checkout_that_is_no_longer_there(self) -> None:
        declare(self.root, "[external_cell_sub]\ngit_origin = /nonexistent/checkout\n")
        with self.assertRaisesRegex(SystemExit, "is not a git repository; `tine cell revert sub`"):
            self.overridden()

    def test_a_checkout_with_no_commit_left_to_follow(self) -> None:
        declare(self.root, f"[external_cell_sub]\ngit_origin = {self.repo}\n")
        with self.assertRaisesRegex(SystemExit, "cell sub: git rev-parse .*HEAD"):
            self.overridden()

    def test_a_cell_the_project_stopped_declaring(self) -> None:
        # A branch switch does this, and Buck2 answers with `dep is not a known cell alias`.
        self.commit()
        self.cell("override", "sub", str(self.repo))
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\n")
        with self.assertRaisesRegex(SystemExit, "cell sub is overridden but this project has none"):
            self.overridden()

    def test_a_project_with_no_block_to_read(self) -> None:
        self.assertEqual(self.overridden(), {})

    def test_a_commit_declared_by_hand_is_taken_as_it_stands(self) -> None:
        # Not resolved again here: Buck fetches it, and reports it if the checkout has lost it.
        declare(
            self.root,
            f"[external_cell_sub]\ngit_origin = {self.repo}\ncommit_hash = {'a' * 40}\n"
            f"[tine]\ncell-sub-commit = {'a' * 40}\n",
        )
        self.assertEqual(self.overridden(), {"sub": (str(self.repo), "a" * 40)})

    def test_a_commit_the_block_records_without_pinning_it_is_resolved_again(self) -> None:
        # `commit_hash` alone is what following a checkout leaves behind, and is not a declaration
        # to stay there: the next command asks the checkout again.
        commit = self.commit()
        declare(self.root, f"[external_cell_sub]\ngit_origin = {self.repo}\ncommit_hash = {'a' * 40}\n")
        self.assertEqual(self.overridden(), {"sub": (str(self.repo), commit)})

    def test_an_origin_declared_by_hand_that_cannot_be_configured(self) -> None:
        # The command refuses to write one; the file it writes can still be edited.
        declare(self.root, f"[external_cell_sub]\ngit_origin = {self.repo}\\\n")
        with self.assertRaisesRegex(SystemExit, "cannot be configured"):
            self.overridden()

    def test_a_commit_declared_by_hand_that_is_not_one(self) -> None:
        # It would be written into the block as a commit_hash Buck2 reads.
        declare(
            self.root,
            f"[external_cell_sub]\ngit_origin = {self.repo}\n[tine]\ncell-sub-commit = wip\n",
        )
        with self.assertRaisesRegex(SystemExit, "is not a commit"):
            self.overridden()

    def test_a_declaration_with_no_checkout_to_it(self) -> None:
        declare(self.root, "[external_cell_sub]\ncommit_hash = abc\n[tine]\ncell-other-commit = abc\n")
        self.assertEqual(self.overridden(), {})


class TestBuck2(unittest.TestCase):
    """Resolving the pin, short of fetching anything."""

    def pin(self, **overrides: str) -> dict[str, dict[str, str]]:
        platform = tine._platform()
        return {
            "tine": {
                "buck2-repository": "example/buck2",
                "buck2-release": "2026-01-01",
                f"buck2-{platform}-artifact": "buck2.zst",
                f"buck2-{platform}-sha256": "a" * 64,
            }
            | overrides
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
        config = {"tine": {f"buck2-{platform}-sha256": "b" * 64}}
        with unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(home)}):
            binary = tine.buck2(config, self.cell())
        self.assertEqual(binary, home / "tine" / "buck2" / ("b" * 64) / "buck2")

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
        platform = tine._platform()
        with self.assertRaisesRegex(SystemExit, "no SHA-256 to verify against"):
            tine.buck2(self.pin(**{f"buck2-{platform}-sha256": "abc"}), self.cell())


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


class TestHandover(CellTestCase):
    """A project overriding the tine cell runs that checkout's command rather than this one."""

    def command(self, checkout: Path) -> Path:
        path = checkout / tine.COMMAND
        path.parent.mkdir(parents=True)
        path.write_text("#!/usr/bin/python3 -SI\n")
        return path

    @contextlib.contextmanager
    def running(self) -> collections.abc.Iterator[list[object]]:
        execv: list[object] = []
        with unittest.mock.patch.object(os, "execv", side_effect=lambda *a: execv.extend(a)):
            yield execv

    def declare(self, origin: Path, cell: str = tine.CELL) -> None:
        declare(self.root, f"[external_cell_{cell}]\ngit_origin = {origin}\n")

    def test_the_overridden_checkouts_command_runs_instead(self) -> None:
        command = self.command(self.repo)
        self.declare(self.repo)
        with self.running() as execv:
            tine.handover(self.root, ["buck", "build", "//x"])
        self.assertEqual(execv, [str(command), [str(command), "buck", "build", "//x"]])

    def test_the_checkouts_own_command_is_where_it_stops(self) -> None:
        # Otherwise the command handed to would find the same declaration and hand over again.
        command = self.command(self.repo)
        self.declare(self.repo)
        with unittest.mock.patch.object(tine, "__file__", str(command)), self.running() as execv:
            tine.handover(self.root, ["buck"])
        self.assertEqual(execv, [])

    def test_a_checkout_carrying_no_command_is_left_to_the_generated_block(self) -> None:
        self.declare(self.repo)
        with self.running() as execv:
            tine.handover(self.root, ["buck"])
        self.assertEqual(execv, [])

    def test_another_cell_overridden_is_not_this_one(self) -> None:
        self.command(self.repo)
        self.declare(self.repo, "sub")
        with self.running() as execv:
            tine.handover(self.root, ["buck"])
        self.assertEqual(execv, [])

    def test_nothing_overridden_hands_nothing_over(self) -> None:
        with self.running() as execv:
            tine.handover(self.root, ["buck"])
        self.assertEqual(execv, [])


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
        self.assertIn("_tine() {\n    local line", script)
        self.assertIn("_tine_buck2", script)
        # Buck2 unregisters the command before registering its own completer; both have to go.
        self.assertNotIn("compdef -d", script)

    def test_a_description_is_quoted_the_way_the_shell_reads_it(self) -> None:
        # An apostrophe would end fish's quoting and take the rest of the file down with it.
        with unittest.mock.patch.dict(tine.VERBS, {"newverb": "report the project's health"}):
            script = tine.completion(self.FISH, "fish")
        self.assertIn(r"-d 'report the project\'s health'", script)

    def test_an_anchor_that_is_no_longer_there_is_reported(self) -> None:
        # Rewriting nothing would emit a script that silently completes nothing.
        for shell in tine.SHELLS:
            with self.assertRaisesRegex(SystemExit, "the pin moved under it"):
                tine.completion("# @generated by buck2\n", shell)


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
