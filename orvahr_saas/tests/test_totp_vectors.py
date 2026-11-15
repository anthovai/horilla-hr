"""RFC 4226 / RFC 6238 test vectors and recovery-code hashing (no DB, both modes)."""

import base64

from django.test import SimpleTestCase

from orvahr_saas import totp

SEED_SHA1 = b"12345678901234567890"
SEED_SHA256 = b"12345678901234567890123456789012"
SEED_SHA512 = b"1234567890" * 6 + b"1234"

# RFC 6238 Appendix B: (time, sha1, sha256, sha512), 8 digits.
RFC6238 = [
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
]

# RFC 4226 Appendix D: HOTP, counters 0..9, 6 digits.
RFC4226 = [
    "755224",
    "287082",
    "359152",
    "969429",
    "338314",
    "254676",
    "287922",
    "162583",
    "399871",
    "520489",
]


class RfcVectorTests(SimpleTestCase):
    def test_rfc4226_hotp(self):
        for counter, expected in enumerate(RFC4226):
            self.assertEqual(totp.hotp(SEED_SHA1, counter), expected)

    def test_rfc6238_totp_all_algorithms(self):
        for at, sha1, sha256, sha512 in RFC6238:
            self.assertEqual(totp.totp(SEED_SHA1, at, digits=8), sha1)
            self.assertEqual(
                totp.totp(SEED_SHA256, at, digits=8, algorithm="sha256"), sha256
            )
            self.assertEqual(
                totp.totp(SEED_SHA512, at, digits=8, algorithm="sha512"), sha512
            )


class VerifyTests(SimpleTestCase):
    def setUp(self):
        self.secret = base64.b32encode(SEED_SHA1).decode()
        self.key = SEED_SHA1
        self.at = 1_700_000_000

    def code(self, offset=0):
        return totp.totp(self.key, self.at + 30 * offset)

    def test_current_and_adjacent_steps_accepted(self):
        now = totp.counter_at(self.at)
        self.assertEqual(totp.verify(self.secret, self.code(), self.at), now)
        self.assertEqual(totp.verify(self.secret, self.code(-1), self.at), now - 1)
        self.assertEqual(totp.verify(self.secret, self.code(1), self.at), now + 1)
        self.assertIsNone(totp.verify(self.secret, self.code(2), self.at))
        self.assertIsNone(totp.verify(self.secret, self.code(-2), self.at))

    def test_replay_refused(self):
        now = totp.counter_at(self.at)
        self.assertIsNone(totp.verify(self.secret, self.code(), self.at, now))
        self.assertIsNone(totp.verify(self.secret, self.code(-1), self.at, now - 1))
        self.assertEqual(totp.verify(self.secret, self.code(1), self.at, now), now + 1)

    def test_garbage_refused(self):
        for code in ("", "12345", "1234567", "abcdef", None):
            self.assertIsNone(totp.verify(self.secret, code, self.at))
        self.assertIsNone(totp.verify("", self.code(), self.at))
        self.assertIsNone(totp.verify("!!!", self.code(), self.at))

    def test_spaces_allowed(self):
        code = self.code()
        self.assertIsNotNone(
            totp.verify(self.secret, f"{code[:3]} {code[3:]}", self.at)
        )

    def test_secret_and_uri(self):
        secret = totp.new_secret()
        self.assertEqual(len(totp.decode_secret(secret)), totp.SECRET_BYTES)
        self.assertEqual(totp.grouped("ABCDEFGHIJ"), "ABCD EFGH IJ")
        uri = totp.provisioning_uri(secret, "ops")
        self.assertTrue(uri.startswith("otpauth://totp/OrvaHR%20Console:ops?"))
        self.assertIn(f"secret={secret}", uri)
        self.assertIn("issuer=OrvaHR+Console", uri)


class RecoveryCodeTests(SimpleTestCase):
    def test_hash_roundtrip_and_format(self):
        code = totp.new_recovery_code()
        self.assertRegex(code, r"^[a-z2-9]{4}(-[a-z2-9]{4}){3}$")
        encoded = totp.hash_recovery_code(code)
        self.assertNotIn(code, encoded)
        self.assertNotIn(code.replace("-", ""), encoded)
        self.assertTrue(totp.recovery_code_matches(code, encoded))
        self.assertTrue(
            totp.recovery_code_matches(code.upper().replace("-", " "), encoded)
        )
        self.assertFalse(totp.recovery_code_matches(totp.new_recovery_code(), encoded))
        self.assertFalse(totp.recovery_code_matches(code, "md5$x$y"))
        self.assertFalse(totp.recovery_code_matches(code, "garbage"))

    def test_salted(self):
        code = totp.new_recovery_code()
        self.assertNotEqual(
            totp.hash_recovery_code(code), totp.hash_recovery_code(code)
        )
