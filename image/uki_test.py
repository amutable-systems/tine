"""Tests for the optional ukify arguments.

    buck test tine//image:test

Secure Boot signing and expected-PCR sealing are two independent keys, each held in the build graph
or in a token, and profiles can opt out of the policy, so one image build exercises one combination.
Integration tests only cover a few combinations; the rest are asserted here.
"""

import unittest

import uki

SECURE_BOOT: uki.Key = {"private_key": "/keys/sb.key", "certificate": "/keys/sb.crt", "source": None}
PCR: uki.Key = {"private_key": "/keys/pcr.key", "certificate": "/keys/pcr.crt", "source": None}

SECURE_BOOT_ARGUMENTS = [
    "--signtool", "systemd-sbsign",
    "--secureboot-private-key", "/keys/sb.key",
    "--secureboot-certificate", "/keys/sb.crt",
    "--sign-kernel",
]  # fmt: skip

PCR_ARGUMENTS = [
    "--pcr-banks", "sha256",
    "--pcr-private-key", "/keys/pcr.key",
    "--sign-initrd-pcrs",
]  # fmt: skip

# What the driver found this ukify accepts. Only what a tool takes is passed to it, so the same
# build works either side of an option's arrival.
OPTIONS = {"--sign-initrd-pcrs"}


def _token(key: uki.Key, source: str = "provider:pkcs11") -> uki.Key:
    """The same role held in a PKCS#11 token, as pkcs11_signing_key() spells it."""
    return {**key, "source": source}


def _profiles(*ids: str, unsealed: str = "") -> list[uki.Profile]:
    """Profiles by id, `unsealed` naming the one that opts out of the expected-PCR policy."""
    return [{"id": id, "title": id, "cmdline": [], "sign_expected_pcr": id != unsealed} for id in ids]


class TestSigningArguments(unittest.TestCase):
    def test_unsigned(self) -> None:
        self.assertEqual(uki._signing_arguments(None, None, [], OPTIONS), [])

    def test_secure_boot_alone(self) -> None:
        self.assertEqual(uki._signing_arguments(SECURE_BOOT, None, [], OPTIONS), SECURE_BOOT_ARGUMENTS)

    def test_expected_pcr_alone(self) -> None:
        """The driver keeps the roles separate; only uki.bzl insists on signing what it seals."""
        self.assertEqual(uki._signing_arguments(None, PCR, [], OPTIONS), PCR_ARGUMENTS)

    def test_the_initrd_policy_rides_along_with_the_expected_pcr_key(self) -> None:
        """NvPCR initialization only accepts a signature bound to the "initrd" policy reference."""
        self.assertIn("--sign-initrd-pcrs", uki._signing_arguments(SECURE_BOOT, PCR, [], OPTIONS))
        self.assertNotIn("--sign-initrd-pcrs", uki._signing_arguments(SECURE_BOOT, None, [], OPTIONS))

    def test_a_ukify_that_cannot_sign_the_initrd_policy(self) -> None:
        """Passing an option an older ukify does not know would fail the build instead."""
        self.assertEqual(
            uki._signing_arguments(None, PCR, [], set()),
            ["--pcr-banks", "sha256", "--pcr-private-key", "/keys/pcr.key"],
        )

    def test_both_roles(self) -> None:
        self.assertEqual(
            uki._signing_arguments(SECURE_BOOT, PCR, [], OPTIONS), SECURE_BOOT_ARGUMENTS + PCR_ARGUMENTS
        )

    def test_profiles_are_sealed_by_default(self) -> None:
        """ukify signs every profile by default, so the common case passes no --sign-profile at all."""
        self.assertEqual(
            uki._signing_arguments(None, PCR, _profiles("dev", "rescue"), OPTIONS), PCR_ARGUMENTS
        )

    def test_one_profile_opting_out_signs_all_the_others(self) -> None:
        """--sign-profile whitelists what is sealed, so opting one out signs the base and rest by name."""
        self.assertEqual(
            uki._signing_arguments(None, PCR, _profiles("dev", "rescue", unsealed="rescue"), OPTIONS),
            [*PCR_ARGUMENTS, "--sign-profile", "main", "--sign-profile", "dev"],
        )

    def test_profiles_without_the_key_seal_nothing(self) -> None:
        """The profiles the test above signs by name yield nothing when no key seals them."""
        self.assertEqual(
            uki._signing_arguments(
                SECURE_BOOT, None, _profiles("dev", "rescue", unsealed="rescue"), OPTIONS
            ),
            SECURE_BOOT_ARGUMENTS,
        )

    def test_a_token_holds_the_secure_boot_key(self) -> None:
        """The URIs stay the key and certificate; the provider is what loads them."""
        self.assertEqual(
            uki._signing_arguments(_token(SECURE_BOOT), None, [], OPTIONS),
            [*SECURE_BOOT_ARGUMENTS, "--signing-provider", "pkcs11", "--certificate-provider", "pkcs11"],
        )

    def test_a_token_holds_the_expected_pcr_key(self) -> None:
        """systemd-measure cannot derive .pcrpkey from a key it does not hold, so it takes the cert."""
        self.assertEqual(
            uki._signing_arguments(None, _token(PCR), [], OPTIONS),
            [*PCR_ARGUMENTS, "--pcr-certificate", "/keys/pcr.crt"],
        )

    def test_a_key_source_ukify_cannot_load(self) -> None:
        """ukify only speaks providers, so anything else must fail the build, not the boot."""
        with self.assertRaises(SystemExit):
            uki._signing_arguments(_token(SECURE_BOOT, "box:pkcs11"), None, [], OPTIONS)


class TestSplashArguments(unittest.TestCase):
    def test_no_splash(self) -> None:
        self.assertEqual(uki._splash_arguments(None), [])

    def test_splash(self) -> None:
        self.assertEqual(uki._splash_arguments("boot.bmp"), ["--splash", "boot.bmp"])
