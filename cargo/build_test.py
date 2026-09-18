"""Exercise Cargo source isolation and persistent build output without a Rust toolchain."""

import errno
import os
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from typing import override
from unittest.mock import patch

import build


class TestBuildCargo(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="cargo-build-test.", dir="/var/tmp"))
        )
        self.project = self.scratch / "project"
        self.source = self.project / "checkout/nested"
        self.source.mkdir(parents=True)
        (self.source / "Cargo.toml").write_text('[package]\nname = "example"\n', encoding="utf-8")
        (self.source / "source.rs").write_text("original", encoding="utf-8")
        os.utime(self.source / "source.rs", ns=(1234567890123456789, 1234567890123456789))
        (self.source / "link").symlink_to("source.rs")
        (self.source / "dangling").symlink_to("missing")
        (self.project / "outside").write_text("outside", encoding="utf-8")
        (self.source / "outside").symlink_to(self.project / "outside")

    def test_source_writes_are_disposable(self) -> None:
        self.check_build(persistent=False)

    def test_incremental_outputs_survive_success_and_failure(self) -> None:
        self.check_build(persistent=True)

    def check_build(self, *, persistent: bool) -> None:
        original = (self.source / "source.rs").stat()
        for iteration, fail in enumerate((False, True, False)):
            scratch = self.scratch / str(iteration)
            scratch.mkdir()
            spec = build.Spec(
                auditable="cargo-auditable",
                bin=f"bin-{iteration}",
                binaries=["example"],
                git={},
                root="nested",
                src="checkout",
                target="incremental" if persistent else None,
                vendor="vendor",
            )

            stale = self.project / spec["bin"] / "undeclared"
            stale.parent.mkdir()
            stale.touch()

            def run(
                command: list[str | Path],
                *,
                check: bool,
                cwd: Path,
                env: dict[str, str],
                index: int = iteration,
                fails: bool = fail,
            ) -> None:
                self.assertTrue(check)
                self.assertTrue(cwd.is_absolute())
                self.assertTrue(Path(env["CARGO_HOME"]).is_absolute())
                self.assertTrue(Path(env["CARGO_TARGET_DIR"]).is_absolute())
                self.assertNotIn("--locked", command)
                self.assertEqual((cwd / "source.rs").stat().st_mtime_ns, original.st_mtime_ns)
                self.assertEqual((cwd / "link").readlink(), Path("source.rs"))
                self.assertEqual((cwd / "dangling").readlink(), Path("missing"))
                self.assertFalse((cwd / "generated").exists())
                (cwd / "link").write_text("changed", encoding="utf-8")
                (cwd / "generated").touch()
                # Through a source symlink, and relative to the project the driver stands in.
                for path in (cwd / "outside", Path("outside")):
                    with self.assertRaises(OSError) as caught:
                        path.write_text("changed", encoding="utf-8")
                    self.assertEqual(caught.exception.errno, errno.EROFS)

                target = Path(env["CARGO_TARGET_DIR"])
                target.mkdir(parents=True, exist_ok=True)
                state = target / "state"
                self.assertEqual(
                    state.read_text() if state.exists() else "0", str(index if persistent else 0)
                )
                state.write_text(str(index + 1), encoding="utf-8")
                if fails:
                    raise subprocess.CalledProcessError(1, command)
                binary = target / "release/example"
                binary.parent.mkdir(exist_ok=True)
                binary.write_text("built", encoding="utf-8")
                binary.chmod(0o755)

            with (
                chdir(self.project),
                patch.object(build.subprocess, "run", side_effect=run),
                patch.dict(os.environ, {"TMPDIR": str(scratch)}),
                patch.object(tempfile, "tempdir", None),
            ):
                relative_scratch = Path("..") / str(iteration)
                if fail:
                    with self.assertRaises(subprocess.CalledProcessError):
                        build.build_cargo(spec, relative_scratch)
                else:
                    build.build_cargo(spec, relative_scratch)

            self.assertEqual((self.source / "source.rs").read_text(encoding="utf-8"), "original")
            self.assertEqual((self.source / "source.rs").stat().st_mtime_ns, original.st_mtime_ns)
            self.assertFalse((self.source / "generated").exists())
            self.assertEqual(list((scratch / "build").iterdir()), [])
            self.assertEqual(list(scratch.glob("source.*")), [])
            output = self.project / spec["bin"] / "example"
            if fail:
                self.assertFalse(output.exists())
            else:
                self.assertFalse(stale.exists())
                self.assertEqual(output.read_text(encoding="utf-8"), "built")
                self.assertEqual(output.stat().st_mode & 0o777, 0o755)
            if persistent:
                self.assertEqual((self.project / "incremental/state").read_text(), str(iteration + 1))
            (self.project / "outside").write_text("writable again", encoding="utf-8")
