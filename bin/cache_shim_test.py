"""Tests for the cache shim's settings and lifecycle.

    buck test tine//bin:test

A script answering on the status socket stands in for the shim: under test is how tine recognises,
shares and starts one, not the shim itself.
"""

import contextlib
import io
import os
import signal
import sys
import unittest
import unittest.mock
import warnings
from pathlib import Path
from typing import cast, override

import cache_shim
from tine_test import scratch

import status

SOURCE = "tine.toml"
URL = "https://cache.example"


def _table(base: dict[str, object], keys: dict[str, object]) -> dict[str, object]:
    """`base` with `keys` laid over it; a None takes the key away, so a test can drop one."""
    return {key: value for key, value in (base | keys).items() if value is not None}


def reader(**keys: object) -> dict[str, object]:
    return _table({"read_url": URL, "unsigned": True}, keys)


def builder(root: Path, **keys: object) -> dict[str, object]:
    for name in ("ca.pem", "s3.key", "leaf.key", "leaf.pem"):
        (root / name).write_text(name)
    base: dict[str, object] = {
        "read_url": URL,
        "authority": ["ca.pem"],
        "s3_bucket": "results",
        "s3_endpoint": "s3.example",
        "s3_key_file": "s3.key",
        "signing_key": "leaf.key",
        "signing_certificate": "leaf.pem",
    }
    return _table(base, keys)


class SettingsCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.root = scratch(self)
        self.home = scratch(self)
        self.enterContext(unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.home)}))

    def settings(self, table: dict[str, object] | None) -> cache_shim.CacheSettings | None:
        return cache_shim.settings({} if table is None else {"cache": table}, self.root, SOURCE)

    def configured(self, table: dict[str, object]) -> cache_shim.CacheSettings:
        cache = self.settings(table)
        assert cache is not None
        return cache


class TestSettings(SettingsCase):
    def test_nothing_configured_is_no_cache(self) -> None:
        self.assertIsNone(self.settings(None))
        self.assertIsNone(self.settings({}))

    def test_it_can_be_switched_off_without_removing_the_rest(self) -> None:
        self.assertIsNone(self.settings(reader(enabled=False)))

    def test_the_defaults(self) -> None:
        cache = self.configured(reader())
        self.assertEqual(cache.dir, (self.home / "tine" / "cache").resolve())
        self.assertEqual(cache.authorities, ())
        self.assertIsNone(cache.s3_bucket)
        self.assertIsNone(cache.signing_key)
        self.assertIsNone(cache.store_size)
        self.assertTrue(cache_shim.PORT_BASE <= cache.port < cache_shim.PORT_BASE + cache_shim.PORT_COUNT)

    def test_the_url_is_required(self) -> None:
        with self.assertRaisesRegex(SystemExit, "needs read_url"):
            self.settings({"unsigned": True})
        with self.assertRaisesRegex(SystemExit, "read_url .* without whitespace"):
            self.settings(reader(read_url="https://a b"))

    def test_trust_is_an_authority_or_an_explicit_unsigned(self) -> None:
        with self.assertRaisesRegex(SystemExit, "needs authority, or unsigned"):
            self.settings({"read_url": URL})
        (self.root / "ca.pem").touch()
        with self.assertRaisesRegex(SystemExit, "needs authority, or unsigned"):
            self.settings({"read_url": URL, "authority": ["ca.pem"], "unsigned": True})
        with self.assertRaisesRegex(SystemExit, "authority .* must be a list"):
            self.settings({"read_url": URL, "authority": "ca.pem"})

    def test_paths_resolve_against_the_project(self) -> None:
        cache = self.configured(builder(self.root, dir="store"))
        self.assertEqual(cache.authorities, ((self.root / "ca.pem").resolve(),))
        self.assertEqual(cache.s3_key_file, (self.root / "s3.key").resolve())
        self.assertEqual(cache.dir, (self.root / "store").resolve())

    def test_a_builder_names_its_bucket_whole(self) -> None:
        with self.assertRaisesRegex(SystemExit, "s3_bucket .* needs s3_endpoint and s3_key_file"):
            self.settings(builder(self.root, s3_endpoint=None))
        with self.assertRaisesRegex(SystemExit, "s3_endpoint .* needs s3_bucket"):
            self.settings(reader(s3_endpoint="s3.example"))
        with self.assertRaisesRegex(SystemExit, "s3_endpoint .* must be a host"):
            self.settings(builder(self.root, s3_endpoint="https://s3.example"))
        with self.assertRaisesRegex(SystemExit, "s3_endpoint .* must be a host"):
            self.settings(builder(self.root, s3_endpoint="s3.example/foo"))

    def test_a_builder_signs_or_says_the_bucket_is_unsigned(self) -> None:
        with self.assertRaisesRegex(SystemExit, "s3_bucket .* needs signing_key, or unsigned"):
            self.settings(builder(self.root, signing_key=None, signing_certificate=None))
        (self.root / "s3.key").touch()
        unsigned = reader(s3_bucket="b", s3_endpoint="s3.example", s3_key_file="s3.key")
        self.assertIsNone(self.configured(unsigned).signing_key)

    def test_a_signing_key_needs_an_authority_a_bucket_and_a_certificate(self) -> None:
        with self.assertRaisesRegex(SystemExit, "signing_key .* needs authority"):
            self.settings(builder(self.root, authority=None, unsigned=True))
        with self.assertRaisesRegex(SystemExit, "signing_key .* needs s3_bucket"):
            self.settings(builder(self.root, s3_bucket=None, s3_endpoint=None, s3_key_file=None))
        with self.assertRaisesRegex(SystemExit, "signing_key .* needs signing_certificate"):
            self.settings(builder(self.root, signing_certificate=None))
        with self.assertRaisesRegex(SystemExit, "object_lifetime .* needs signing_key"):
            self.settings(reader(object_lifetime=30))

    def test_only_a_builder_accepts_uploads(self) -> None:
        self.assertTrue(self.configured(reader()).accepts_uploads)
        self.assertTrue(self.configured(builder(self.root)).accepts_uploads)
        self.assertFalse(self.configured(reader(unsigned=None, authority=["ca.pem"])).accepts_uploads)

    def test_the_port_follows_the_store(self) -> None:
        one = self.configured(reader(dir="one"))
        self.assertEqual(one.port, self.configured(reader(dir="./one/")).port)
        self.assertNotEqual(one.port, self.configured(reader(dir="two")).port)
        self.assertEqual(self.configured(reader(port=4242)).port, 4242)

    def test_values_are_checked_for_type(self) -> None:
        with self.assertRaisesRegex(SystemExit, "unsupported keys: bogus"):
            self.settings(reader(bogus=1))
        with self.assertRaisesRegex(SystemExit, "unsigned .* must be true or false"):
            self.settings({"read_url": URL, "unsigned": "yes"})
        with self.assertRaisesRegex(SystemExit, "store_size .* whole number"):
            self.settings(reader(store_size=True))
        with self.assertRaisesRegex(SystemExit, r"\[cache\] in tine.toml must be a table"):
            cache_shim.settings({"cache": 1}, self.root, SOURCE)


class TestArguments(SettingsCase):
    def test_a_reader(self) -> None:
        cache = self.configured(reader(dir="store", port=4242))
        self.assertEqual(
            cache_shim.arguments(cache),
            ["--store", str(cache.dir), "--port", "4242", "--read-url", URL, "--unsigned"],
        )

    def test_a_builder(self) -> None:
        cache = self.configured(
            builder(self.root, dir="store", s3_insecure=True, object_lifetime=30, store_size=5)
        )
        arguments = cache_shim.arguments(cache)
        self.assertEqual(arguments[6:], [
            "--authority", str(self.root.resolve() / "ca.pem"),
            "--s3-bucket", "results",
            "--s3-endpoint", "s3.example",
            "--s3-key-file", str(self.root.resolve() / "s3.key"),
            "--s3-insecure",
            "--signing-key", str(self.root.resolve() / "leaf.key"),
            "--signing-certificate", str(self.root.resolve() / "leaf.pem"),
            "--object-lifetime", "30",
            "--store-size", "5",
        ])  # fmt: skip


class SocketCase(SettingsCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(scratch(self))}))
        self.cache = self.configured(reader(dir="store"))
        self.cache.dir.mkdir()


class TestSocket(SocketCase):
    def test_both_ends_agree_on_the_socket(self) -> None:
        self.assertEqual(cache_shim.socket_path(self.cache.dir), status.socket_path(self.cache.dir))

    def test_nothing_serving_is_none(self) -> None:
        self.assertIsNone(cache_shim.ask(self.cache.dir))

    def test_a_report_reads_back(self) -> None:
        with status.serving(self.cache.dir, lambda: {"pid": 1, "argv": ["--store"]}):
            self.assertEqual(cache_shim.ask(self.cache.dir), {"pid": 1, "argv": ["--store"]})
        self.assertIsNone(cache_shim.ask(self.cache.dir))

    def test_a_build_registers_itself_in_the_same_call(self) -> None:
        """Each end has its own copy of this; a shim that missed a registration would time out."""
        registered: list[int] = []
        with status.serving(self.cache.dir, lambda: {"pid": 1}, registered.append):
            self.assertEqual(cache_shim.ask(self.cache.dir, client=4242), {"pid": 1})
        self.assertEqual(registered, [4242])


class TestLifecycle(SocketCase):
    """Starting a shim through Buck, sharing one, and every way that goes wrong."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(self.reap)
        # A shim outlives the call that starts it, so its Popen object is collected unreaped.
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter("ignore", ResourceWarning)

    def reap(self) -> None:
        """Kill what a test let `start` spawn, which would otherwise outlive the run."""
        if (report := cache_shim.ask(self.cache.dir)) is not None:
            pid = cast(int, report["pid"])
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)

    def fake(self, body: str) -> list[str]:
        """A stand-in Buck: whatever it is asked to run, it does `body` with the shim's arguments.

        `status` comes from this test tree, so the fake answers exactly as the shim would.
        """
        script = scratch(self) / "buck"
        script.write_text(
            f"#!{sys.executable}\n"
            "import os, sys, time\n"
            f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
            "from pathlib import Path\n"
            "import status\n"
            'args = sys.argv[sys.argv.index("--") + 1 :]\n'
            'store = Path(args[args.index("--store") + 1])\n'
            f"{body}\n"
        )
        script.chmod(0o755)
        return [str(script)]

    def serving(self) -> list[str]:
        return self.fake(
            "clients = []\n"
            'report = lambda: {"pid": os.getpid(), "argv": args, "clients": clients}\n'
            "with status.serving(store, report, clients.append):\n    time.sleep(60)"
        )

    def test_it_starts_one_and_waits_for_it_to_serve(self) -> None:
        prepare = unittest.mock.Mock(return_value=self.serving())
        cache_shim.ensure(self.cache, prepare)
        prepare.assert_called_once_with()
        report = cache_shim.ask(self.cache.dir)
        assert report is not None
        self.assertEqual(report["argv"], cache_shim.arguments(self.cache))

    def test_the_build_that_wants_it_is_registered_with_it(self) -> None:
        """The shim times out on its own, and this process is the one that becomes Buck."""
        cache_shim.ensure(self.cache, unittest.mock.Mock(return_value=self.serving()))
        report = cache_shim.ask(self.cache.dir)
        assert report is not None
        self.assertEqual(report["clients"], [os.getpid()])

    def test_one_already_serving_these_settings_is_shared(self) -> None:
        cache_shim.ensure(self.cache, unittest.mock.Mock(return_value=self.serving()))
        prepare = unittest.mock.Mock()
        cache_shim.ensure(self.cache, prepare)
        prepare.assert_not_called()

    def test_one_serving_other_settings_is_refused(self) -> None:
        cache_shim.ensure(self.cache, unittest.mock.Mock(return_value=self.serving()))
        other = self.configured(reader(dir="store", port=self.cache.port + 1))
        with self.assertRaisesRegex(SystemExit, "already serves .* with other settings, as pid"):
            cache_shim.ensure(other, unittest.mock.Mock())

    def test_a_start_that_dies_is_diagnosed_from_its_log(self) -> None:
        buck = self.fake('print("boom, said the box", file=sys.stderr)\nsys.exit(3)')
        with self.assertRaisesRegex(SystemExit, "exited with 3 instead of serving .*boom, said the box"):
            cache_shim.start(self.cache, buck)

    def test_a_start_that_never_serves_is_given_up_on(self) -> None:
        with unittest.mock.patch.object(cache_shim, "START_TIMEOUT", 0.5):
            with self.assertRaisesRegex(SystemExit, "has not served .* after 0.5s"):
                cache_shim.start(self.cache, self.fake("time.sleep(60)"))

    def test_status_says_what_serves(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cache_shim.status(self.cache)
        self.assertIn("not running", out.getvalue())
        cache_shim.ensure(self.cache, unittest.mock.Mock(return_value=self.serving()))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cache_shim.status(self.cache)
        self.assertRegex(out.getvalue(), r"shim +pid \d+")
