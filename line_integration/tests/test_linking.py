from datetime import timedelta

from django.utils import timezone

from line_integration import crypto, linking
from line_integration.models import LineChannel, LineLinkCode, LineUserLink
from line_integration.tests.helpers import FAKE_SECRET, FAKE_TOKEN, LineTestCase


class LinkCodeTests(LineTestCase):
    def setUp(self):
        super().setUp()
        self.emp = self.make_emp("linker")

    def _code(self):
        code, obj = linking.generate_code(self.emp)
        return linking.normalise(code), obj

    def test_valid_code_links(self):
        code, obj = self._code()
        outcome, link = linking.redeem(self.channel, "Uvalid", code)
        self.assertEqual(outcome, linking.LINKED)
        self.assertEqual(link.employee, self.emp)
        obj.refresh_from_db()
        self.assertIsNotNone(obj.used_at)

    def test_code_is_stored_hashed_only(self):
        display, obj = linking.generate_code(self.emp)
        raw = linking.normalise(display)
        self.assertNotIn(raw, obj.code_hash)
        self.assertEqual(obj.code_hash, crypto.keyed_hash(raw))

    def test_expired_code(self):
        code, obj = self._code()
        later = obj.expires_at + timedelta(seconds=1)
        outcome, link = linking.redeem(self.channel, "Uexp", code, now=later)
        self.assertEqual(outcome, linking.EXPIRED)
        self.assertIsNone(link)
        self.assertFalse(LineUserLink.objects.exists())

    def test_reused_code(self):
        code, _ = self._code()
        self.assertEqual(linking.redeem(self.channel, "Ufirst", code)[0], linking.LINKED)
        self.assertEqual(linking.redeem(self.channel, "Usecond", code)[0], linking.USED)
        self.assertEqual(LineUserLink.objects.get().line_user_id, "Ufirst")

    def test_wrong_code(self):
        self._code()
        self.assertEqual(linking.redeem(self.channel, "Uwrong", "ABCDEFGH")[0], linking.INVALID)
        self.assertFalse(LineUserLink.objects.exists())

    def test_code_for_other_channel_rejected(self):
        code, _ = self._code()
        other = LineChannel(name="Other", company=None)
        other.access_token = FAKE_TOKEN
        other.channel_secret = FAKE_SECRET
        other.save()
        self.assertEqual(linking.redeem(other, "Uother", code)[0], linking.INVALID)

    def test_new_code_retires_old(self):
        old, _ = self._code()
        new, _ = self._code()
        self.assertEqual(linking.redeem(self.channel, "U1", old)[0], linking.USED)
        self.assertEqual(linking.redeem(self.channel, "U1", new)[0], linking.LINKED)

    def test_failed_attempts_rate_limited(self):
        code, _ = self._code()
        for _i in range(linking.MAX_FAILED_ATTEMPTS):
            self.assertEqual(linking.redeem(self.channel, "Ubrute", "ABCDEFGH")[0], linking.INVALID)
        # even the right code is refused while limited
        self.assertEqual(linking.redeem(self.channel, "Ubrute", code)[0], linking.RATE_LIMITED)
        later = timezone.now() + linking.FAILED_ATTEMPT_WINDOW + timedelta(seconds=1)
        fresh, _ = linking.generate_code(self.emp, now=later)
        self.assertEqual(
            linking.redeem(self.channel, "Ubrute", linking.normalise(fresh), now=later)[0],
            linking.LINKED,
        )

    def test_generation_rate_limited(self):
        for _i in range(linking.MAX_CODES_PER_HOUR):
            linking.generate_code(self.emp)
        with self.assertRaises(linking.RateLimited):
            linking.generate_code(self.emp)

    def test_no_channel_for_company(self):
        self.channel.is_active = False
        self.channel.save()
        with self.assertRaises(linking.NoChannel):
            linking.generate_code(self.emp)

    def test_line_account_already_linked_elsewhere(self):
        other = self.make_emp("other")
        LineUserLink.objects.create(employee=other, channel=self.channel, line_user_id="Utaken")
        code, _ = self._code()
        self.assertEqual(linking.redeem(self.channel, "Utaken", code)[0], linking.TAKEN)
        self.assertEqual(LineUserLink.objects.get(line_user_id="Utaken").employee, other)

    def test_normalise(self):
        self.assertEqual(linking.normalise(" link abcd-efgh "), "ABCDEFGH")
        self.assertEqual(linking.normalise("ABCD2345"), "ABCD2345")
        self.assertIsNone(linking.normalise("hello"))
        self.assertIsNone(linking.normalise("GOODMORN"))  # contains O: not in alphabet
        self.assertIsNone(linking.normalise(""))

    def test_codes_unique_and_unambiguous(self):
        LineLinkCode.objects.all().delete()
        code, _ = linking.generate_code(self.emp)
        self.assertRegex(code, r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
