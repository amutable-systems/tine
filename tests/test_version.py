"""Tests for the git-derived image version tool.

    python3 -m unittest discover -s tests -t . -v

Each test builds a real throwaway git repository; the tool's git calls run for real.
"""

import contextlib
import importlib.util
import io
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import override

TINE_REPO = Path(__file__).resolve().parent.parent
TOOL_PATH = TINE_REPO / "tools" / "version.py"

# The longest default partition label pattern (DEFAULT_USR_VERITY_PARTITIONS, signed); the one
# place that spells out the suffix, which may grow a shorter alias.
LONGEST_LABEL_TEMPLATE = "myos_{version}_verity_sig"

spec = importlib.util.spec_from_file_location("version", TOOL_PATH)
assert spec and spec.loader
version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version)


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


class TestComputeVersion(unittest.TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="version-test.")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
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

    def test_release(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.assertEqual(version.compute_version(self.repo, "{version}"), "1.2.3")

    def test_snapshot(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        commit = self.commit("second")
        self.assertEqual(version.compute_version(self.repo, "{version}"), f"1.2.3^1-{commit[:12]}")

    def test_snapshot_counts_commits(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.commit("second")
        commit = self.commit("third")
        self.assertEqual(version.compute_version(self.repo, "{version}"), f"1.2.3^2-{commit[:12]}")

    def test_no_tag(self) -> None:
        commit = self.commit()
        self.assertEqual(version.compute_version(self.repo, "{version}"), f"0.0.0^1-{commit[:12]}")

    def test_non_version_tags_ignored(self) -> None:
        commit = self.commit()
        git("tag", "snapshot-1", cwd=self.repo)
        self.assertEqual(version.compute_version(self.repo, "{version}"), f"0.0.0^1-{commit[:12]}")

    def test_latest_tag_wins(self) -> None:
        self.commit()
        git("tag", "v1.0.0", cwd=self.repo)
        self.commit("second")
        git("tag", "v2.0.0", cwd=self.repo)
        self.assertEqual(version.compute_version(self.repo, "{version}"), "2.0.0")

    def test_dev_dirty_tree(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.commit("second")
        self.dirty()
        with unittest.mock.patch("time.time", return_value=self.committer_epoch() + 86400):
            self.assertEqual(version.compute_version(self.repo, "{version}"), "1.2.3^1^86400")

    def test_dev_at_tag(self) -> None:
        # A dirty tree is never a release, even right at the tag.
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.dirty()
        with unittest.mock.patch("time.time", return_value=self.committer_epoch() + 5):
            self.assertEqual(version.compute_version(self.repo, "{version}"), "1.2.3^0^5")

    def test_dev_clock_skew_clamps(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.dirty()
        with unittest.mock.patch("time.time", return_value=self.committer_epoch() - 100):
            self.assertEqual(version.compute_version(self.repo, "{version}"), "1.2.3^0^0")

    def test_hash_shrinks_to_fit_template(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        commit = self.commit("second")
        # 20 fixed characters leave 16 for the version: "1.2.3^1-" plus an 8-character hash
        template = ("x" * 20) + "{version}"
        self.assertEqual(version.compute_version(self.repo, template), f"1.2.3^1-{commit[:8]}")

    def test_hash_fills_longest_default_label(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        commit = self.commit("second")
        room = version.GPT_LABEL_LIMIT - len(LONGEST_LABEL_TEMPLATE.format(version="1.2.3^1-"))
        self.assertEqual(
            version.compute_version(self.repo, LONGEST_LABEL_TEMPLATE),
            f"1.2.3^1-{commit[: min(room, 12)]}",
        )

    def test_hash_never_shrinks_below_four(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.commit("second")
        template = ("x" * 25) + "{version}"
        with self.assertRaisesRegex(SystemExit, "does not fit"):
            version.compute_version(self.repo, template)

    def test_bare_tag_must_fit(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        template = ("x" * 32) + "{version}"
        with self.assertRaisesRegex(SystemExit, "does not fit"):
            version.compute_version(self.repo, template)

    def test_not_a_repository(self) -> None:
        with tempfile.TemporaryDirectory(prefix="version-test.") as empty:
            with self.assertRaises(subprocess.CalledProcessError):
                version.compute_version(Path(empty), "{version}")

    def test_repository_without_commits(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            version.compute_version(self.repo, "{version}")

    def test_invalid_tag_rejected(self) -> None:
        self.commit()
        git("tag", "v1.2.3+dirty", cwd=self.repo)
        with self.assertRaisesRegex(SystemExit, "valid version"):
            version.compute_version(self.repo, "{version}")


class TestCLI(unittest.TestCase):
    def test_prints_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="version-test.") as tmp:
            repo = Path(tmp)
            git("init", "--quiet", cwd=repo)
            git("config", "user.name", "Test", cwd=repo)
            git("config", "user.email", "test@example.com", cwd=repo)
            git("commit", "--allow-empty", "--quiet", "--message", "commit", cwd=repo)
            git("tag", "v0.1.0", cwd=repo)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                version.main(["--directory", str(repo), "{version}"])
            self.assertEqual(stdout.getvalue(), "0.1.0\n")

    def test_template_needs_placeholder(self) -> None:
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                version.main(["static"])
