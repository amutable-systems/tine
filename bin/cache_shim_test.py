"""Tests for cache shim management.

    buck test tine//bin:test

The sandbox unshares the network and never brings the loopback up, so `bind()` works but `connect()`
does not: the port probe and the wait for a shim to listen are patched, and everything else -- the
lock, the spawn, the state it records -- runs for real.
"""

import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import threading
import unittest
import unittest.mock
import warnings
from pathlib import Path
from typing import override

import cache_shim
import tine
from tine_test import scratch


class TestCacheSettings(unittest.TestCase):
    MINIMAL = {"endpoint": "s3.example.com", "bucket": "cache"}

    def settings(self, **keys: object) -> cache_shim.CacheSettings:
        configured = cache_shim.settings({"cache": self.MINIMAL | keys}, tine.SETTINGS)
        assert configured is not None
        return configured

    def test_nothing_configured_is_no_cache(self) -> None:
        self.assertIsNone(cache_shim.settings({}, tine.SETTINGS))

    def test_an_empty_table_is_no_cache(self) -> None:
        self.assertIsNone(cache_shim.settings({"cache": {}}, tine.SETTINGS))

    def test_it_can_be_switched_off_without_removing_the_rest(self) -> None:
        self.assertIsNone(cache_shim.settings({"cache": self.MINIMAL | {"enabled": False}}, tine.SETTINGS))

    def test_the_defaults(self) -> None:
        configured = self.settings()
        self.assertEqual(configured.region, "auto")
        self.assertEqual(configured.auth_method, "access_key")
        self.assertFalse(configured.write)
        self.assertIsNone(configured.key_file)
        self.assertEqual(configured.max_size, cache_shim.MAX_SIZE)

    def test_the_directory_is_keyed_by_the_bucket_it_caches(self) -> None:
        home = scratch(self)
        with unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(home)}):
            one = self.settings()
            other = self.settings(bucket="other")
        self.assertNotEqual(one.dir, other.dir)
        for configured in (one, other):
            self.assertEqual(configured.dir.parent, (home / "bazel-remote").resolve())

    def test_the_port_follows_the_directory(self) -> None:
        one = self.settings()
        self.assertEqual(one.port, self.settings(dir=str(one.dir)).port)
        self.assertNotEqual(one.port, self.settings(bucket="other").port)

    def test_the_port_stays_out_of_the_ephemeral_range(self) -> None:
        ceiling = cache_shim.PORT_BASE + cache_shim.PORT_COUNT
        for bucket in (f"bucket-{index}" for index in range(200)):
            self.assertIn(self.settings(bucket=bucket).port, range(cache_shim.PORT_BASE, ceiling))
        low = int(Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()[0])
        self.assertLessEqual(ceiling, low)

    def test_one_directory_is_one_port_however_it_is_spelled(self) -> None:
        directory = scratch(self)
        link = directory.parent / (directory.name + ".link")
        link.symlink_to(directory)
        self.addCleanup(link.unlink)
        self.assertEqual(self.settings(dir=str(directory)).port, self.settings(dir=str(link)).port)

    def test_a_configured_port_wins(self) -> None:
        self.assertEqual(self.settings(port=31000).port, 31000)

    def test_mandatory_keys(self) -> None:
        for key in ("bucket", "endpoint"):
            with self.subTest(key=key), self.assertRaisesRegex(SystemExit, f"{key} in .* non-empty string"):
                cache_shim.settings(
                    {"cache": {other: "x" for other in self.MINIMAL if other != key}}, tine.SETTINGS
                )

    def test_unsupported_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "unsupported keys: buckett"):
            cache_shim.settings({"cache": self.MINIMAL | {"buckett": "typo"}}, tine.SETTINGS)

    def test_writing_needs_a_key_to_write_with(self) -> None:
        with self.assertRaisesRegex(SystemExit, "needs a key_file"):
            self.settings(write=True)
        configured = self.settings(write=True, key_file="~/key")
        self.assertEqual(configured.key_file, Path.home() / "key")

    def test_an_auth_method_that_finds_its_own_key_needs_no_key_file(self) -> None:
        for method in ("iam_role", "aws_credentials_file"):
            with self.subTest(method=method):
                self.assertIsNone(self.settings(write=True, auth_method=method).key_file)

    def test_strings_that_would_split_into_two_words(self) -> None:
        for value in ("with space", "\tabbed"):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "no whitespace"):
                self.settings(bucket=value)

    def test_a_value_starting_like_a_flag_is_still_a_value(self) -> None:
        """Joined to its flag on the command line, so it needs no rejecting here."""
        cache = self.settings(bucket="--s3.endpoint")
        self.assertIn("--s3.bucket=--s3.endpoint", cache_shim.command(cache, Path("/shim")))

    def test_strings_must_be_non_empty(self) -> None:
        for value in ("", 1, True):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "non-empty string"):
                self.settings(region=value)

    def test_flags_must_be_boolean(self) -> None:
        for value in ("yes", 1):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "true or false"):
                self.settings(write=value)

    def test_numbers_reject_a_boolean_that_would_pass_for_one(self) -> None:
        for value in (True, 0, -1, "20", 1 << 21):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "whole number"):
                self.settings(max_size=value)

    def test_paths_must_be_non_empty_strings(self) -> None:
        for value in ("", 1):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "non-empty path"):
                self.settings(key_file=value)

    def test_the_table_must_be_a_table(self) -> None:
        with self.assertRaisesRegex(SystemExit, r"\[cache\] in .* must be a table"):
            cache_shim.settings({"cache": "yes"}, tine.SETTINGS)

    def test_it_reads_through_the_local_overlay(self) -> None:
        root = scratch(self)
        (root / tine.CONFIG).write_text('[cache]\nendpoint = "s3.example.com"\nbucket = "cache"\n')
        (root / tine.LOCAL_SETTINGS).write_text('[cache]\nwrite = true\nkey_file = "key"\n')
        configured = cache_shim.settings(tine.project_settings(root), tine.SETTINGS)
        assert configured is not None
        self.assertEqual(configured.bucket, "cache")
        self.assertTrue(configured.write)


class TestShim(unittest.TestCase):
    """The lock, and every way starting a shim can go wrong."""

    @override
    def setUp(self) -> None:
        self.home = scratch(self)
        patch = unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        self.cache = self.settings()
        self.state = cache_shim.state_dir(self.cache)
        self.addCleanup(self.reap)
        # A shim outlives the call that starts it, so its Popen object is collected unreaped
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter("ignore", ResourceWarning)

    def reap(self) -> None:
        """Kill what a test let `start` spawn, which would otherwise outlive the run holding a lock."""
        with contextlib.suppress(OSError, ValueError, KeyError):
            pid = json.loads((self.state / cache_shim.SHIM_STATE).read_text())["pid"]
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)

    def settings(self, **keys: object) -> cache_shim.CacheSettings:
        table = {"endpoint": "s3.example.com", "bucket": "b", "dir": str(scratch(self) / "cache")}
        configured = cache_shim.settings({"cache": table | keys}, tine.SETTINGS)
        assert configured is not None
        return configured

    def writer(self, **keys: object) -> cache_shim.CacheSettings:
        key = scratch(self) / "key"
        key.write_text("theid thesecret\n")
        return self.settings(write=True, key_file=str(key), **keys)

    def fake(self, script: str) -> Path:
        """A stand-in shim.

        `exec` where it has to outlive the shell, so the lock has one holder: a forked child would
        inherit the descriptor and keep the lock after the shell is killed.
        """
        binary = scratch(self) / cache_shim.SHIM
        binary.write_text(f"#!/bin/sh\n{script}\n")
        binary.chmod(0o755)
        return binary

    def hold(self, directory: Path) -> subprocess.Popen[bytes]:
        """Take the lock the way ensure_shim does, and hand it to a child that outlives us."""
        import fcntl

        directory.mkdir(parents=True, exist_ok=True)
        lock = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = subprocess.Popen(["sh", "-c", "exec sleep 30"], pass_fds=(lock,))
        os.close(lock)
        self.addCleanup(held.wait)
        self.addCleanup(held.kill)
        return held

    def locked(self, directory: Path) -> bool:
        import fcntl

        probe = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except OSError:
            return True
        finally:
            os.close(probe)

    def test_the_lock_outlives_the_process_that_took_it(self) -> None:
        directory = self.cache.dir
        held = self.hold(directory)
        self.assertTrue(self.locked(directory))
        held.kill()
        held.wait()
        self.assertFalse(self.locked(directory))

    def test_a_shim_that_dies_is_diagnosed(self) -> None:
        binary = self.fake("echo boom >&2; exit 3")
        with self.assertRaisesRegex(SystemExit, "exited with 3.*boom"):
            cache_shim.ensure(self.cache, binary)
        # Nothing holds the directory afterwards, so the next command may try again.
        self.assertFalse(self.locked(self.cache.dir))

    def test_a_held_directory_with_no_state_is_not_adopted(self) -> None:
        self.hold(self.cache.dir)
        binary = self.fake("exec sleep 30")
        with self.assertRaisesRegex(SystemExit, "tine did not start it"):
            cache_shim.ensure(self.cache, binary)

    def test_a_shim_for_another_bucket_is_refused_by_name(self) -> None:
        self.hold(self.cache.dir)
        self.state.mkdir(parents=True)
        (self.state / cache_shim.SHIM_STATE).write_text(
            json.dumps({"digest": "other", "bucket": "theirs", "endpoint": "elsewhere", "pid": 1})
        )
        binary = self.fake("exec sleep 30")
        with self.assertRaisesRegex(SystemExit, "as pid 1, for theirs at elsewhere through"):
            cache_shim.ensure(self.cache, binary)

    def share(self, binary: Path) -> None:
        self.hold(self.cache.dir)
        self.state.mkdir(parents=True)
        digest = cache_shim.identity(self.cache, binary)
        (self.state / cache_shim.SHIM_STATE).write_text(
            json.dumps({"digest": digest, "dir": str(self.cache.dir)})
        )

    def test_a_shim_already_serving_this_cache_is_shared(self) -> None:
        binary = self.fake("exec sleep 30")
        self.share(binary)
        record = self.state / cache_shim.SHIM_STATE
        before = record.read_text()
        with unittest.mock.patch.object(cache_shim, "_is_port_listening", return_value=True):
            cache_shim.ensure(self.cache, binary)
        # Nothing spawned and nothing rewritten: the shim that holds this cache is somebody else's.
        self.assertEqual(record.read_text(), before)
        self.assertFalse(cache_shim.log_path(self.cache).exists())

    def test_sharing_one_waits_for_it_to_listen(self) -> None:
        binary = self.fake("exec sleep 30")
        self.share(binary)
        with unittest.mock.patch.object(cache_shim, "SHIM_START_TIMEOUT", 0):
            with unittest.mock.patch.object(cache_shim, "_is_port_listening", return_value=False):
                with self.assertRaisesRegex(SystemExit, "has not served"):
                    cache_shim.ensure(self.cache, binary)

    def test_a_stale_socket_does_not_outlive_the_shim_that_left_it(self) -> None:
        binary = self.fake("echo boom >&2; exit 3")
        self.state.mkdir(parents=True)
        stale = cache_shim.socket_path(self.cache)
        stale.touch()
        with self.assertRaises(SystemExit):
            cache_shim.ensure(self.cache, binary)
        self.assertFalse(stale.exists())

    def test_a_shim_that_never_serves_is_not_left_holding_the_lock(self) -> None:
        binary = self.fake("exec sleep 30")
        with unittest.mock.patch.object(cache_shim, "SHIM_START_TIMEOUT", 0):
            with unittest.mock.patch.object(cache_shim, "_is_port_listening", return_value=False):
                with self.assertRaisesRegex(SystemExit, "did not serve"):
                    cache_shim.ensure(self.cache, binary)
        self.assertFalse(self.locked(self.cache.dir))
        # And it named a pid, so `tine cache-status` can still point at it.
        recorded = json.loads((self.state / cache_shim.SHIM_STATE).read_text())
        self.assertIn("pid", recorded)

    def test_the_state_is_this_users_alone(self) -> None:
        binary = self.fake("exec sleep 30")
        with unittest.mock.patch.object(cache_shim, "_is_port_listening", side_effect=[False, True]):
            cache_shim.ensure(self.cache, binary)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def test_a_taken_port_is_refused_rather_than_doubled_up_on(self) -> None:
        with unittest.mock.patch.object(cache_shim, "_is_port_listening", return_value=True):
            binary = self.fake("exec sleep 30")
            with self.assertRaisesRegex(SystemExit, "is already in use"):
                cache_shim.ensure(self.cache, binary)

    def test_the_key_never_reaches_the_command_line(self) -> None:
        cache = self.writer()
        command = cache_shim.command(cache, Path("/shim"))
        self.assertNotIn("thesecret", " ".join(command))
        environment = cache_shim.environment(cache)
        self.assertEqual(environment["BAZEL_REMOTE_S3_ACCESS_KEY_ID"], "theid")
        self.assertEqual(environment["BAZEL_REMOTE_S3_SECRET_ACCESS_KEY"], "thesecret")
        self.assertNotIn("BAZEL_REMOTE_S3_SIGNATURE_TYPE", environment)

    def test_no_key_asks_for_no_signature(self) -> None:
        """Signing with the placeholder would be answered 403, which the shim reports as a miss."""
        environment = cache_shim.environment(self.cache)
        self.assertEqual(environment["BAZEL_REMOTE_S3_SIGNATURE_TYPE"], "anonymous")
        self.assertEqual(environment["BAZEL_REMOTE_S3_ACCESS_KEY_ID"], "unused")

    def test_it_passes_on_what_the_shim_reaches_the_bucket_with(self) -> None:
        """A proxy or a role the shim cannot see is a bucket it cannot reach."""
        with unittest.mock.patch.dict(
            os.environ, {"https_proxy": "http://proxy:3128", "AWS_ROLE_ARN": "arn:x", "EDITOR": "vi"}
        ):
            environment = cache_shim.environment(self.settings(auth_method="iam_role"))
        self.assertEqual(environment["https_proxy"], "http://proxy:3128")
        self.assertEqual(environment["AWS_ROLE_ARN"], "arn:x")
        self.assertNotIn("EDITOR", environment)

    def test_a_key_file_that_is_not_one(self) -> None:
        key = scratch(self) / "key"
        key.write_text("onlyone\n")
        with self.assertRaisesRegex(SystemExit, "must hold an access key id and a secret"):
            cache_shim.environment(self.settings(key_file=str(key)))

    def test_a_key_file_that_is_not_there(self) -> None:
        with self.assertRaises(OSError):
            cache_shim.environment(self.settings(key_file=str(scratch(self) / "absent")))

    def test_a_reader_never_reaches_the_network_to_upload(self) -> None:
        command = cache_shim.command(self.cache, Path("/shim"))
        self.assertIn("--num_uploaders=0", command)
        writing = cache_shim.command(self.writer(), Path("/shim"))
        self.assertNotIn("--num_uploaders=0", writing)

    def test_the_digest_separates_what_must_not_be_shared(self) -> None:
        binary, other = Path("/shim"), Path("/other")
        digest = cache_shim.identity(self.cache, binary)
        for changed in (self.settings(bucket="other"), self.writer()):
            self.assertNotEqual(cache_shim.identity(changed, binary), digest)
        self.assertNotEqual(cache_shim.identity(self.cache, other), digest)


class TestCacheVerb(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.root = scratch(self)
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\n")
        self.home = scratch(self)
        patch = unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def configure(self) -> cache_shim.CacheSettings:
        (self.root / tine.CONFIG).write_text(
            f'[cache]\nendpoint = "s3.example.com"\nbucket = "b"\ndir = "{self.root / "cache"}"\n'
        )
        configured = cache_shim.settings(tine.project_settings(self.root), tine.SETTINGS)
        assert configured is not None
        return configured

    def report(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            tine.cache_status(self.root, [])
        return out.getvalue()

    def test_no_configured_cache_is_nothing_to_report(self) -> None:
        with self.assertRaisesRegex(SystemExit, "configures a shared cache"):
            tine.cache_status(self.root, [])

    def test_status_reports_the_settings_and_that_nothing_serves_them(self) -> None:
        cache = self.configure()
        reported = self.report()
        self.assertIn("s3.example.com", reported)
        self.assertIn(f"127.0.0.1:{cache.port}", reported)
        self.assertIn("not running; the next build starts one read-only", reported)

    def test_an_address_served_by_no_recorded_shim_is_reported(self) -> None:
        """A state dir can outlive its shim, and the address can be someone else's."""
        self.configure()
        with unittest.mock.patch.object(cache_shim, "_is_port_listening", return_value=True):
            self.assertIn("not one tine started", self.report())

    def test_status_asks_the_shim_what_it_holds(self) -> None:
        """Over the socket, so what it reports is the running shim and not the record beside it."""
        cache = self.configure()
        state = cache_shim.state_dir(cache)
        state.mkdir(parents=True)
        (state / cache_shim.SHIM_STATE).write_text(json.dumps({"pid": 4242, "write": True}))
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(cache_shim.socket_path(cache)))
        listener.listen(1)
        self.addCleanup(listener.close)

        def answer() -> None:
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
                body = json.dumps({"NumFiles": 7, "CurrSize": 1234})
                connection.sendall(f"HTTP/1.0 200 OK\r\n\r\n{body}".encode())

        server = threading.Thread(target=answer)
        server.start()
        self.addCleanup(server.join)
        with unittest.mock.patch.object(cache_shim, "_is_port_listening", return_value=True):
            reported = self.report()
        self.assertIn("7 files, 1234 bytes", reported)
        # The settings here configure no write key, so this names the running shim's own mode.
        self.assertIn("serving read-write as pid 4242", reported)
