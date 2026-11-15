"""Console 2FA: enrolment, sign-in verification, recovery codes, lockout, audit."""

import datetime
import os
import re
import time

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from orvahr_saas import totp
from orvahr_saas.tests.test_console import (
    STRONG,
    ConsoleTestCase,
    current_code,
    make_operator,
)


class EnrolmentTests(ConsoleTestCase):
    def test_mandatory_enrolment_after_password(self):
        from orvahr_saas.models import ConsoleOperator, OperatorRecoveryCode

        make_operator(with_totp=False)
        response = self.login()
        self.assertEqual(response["Location"], "/console/2fa/")
        # Password alone gets nowhere else -- console or billing.
        self.assertEqual(self.web.get("/console/")["Location"], "/console/2fa/")
        self.assertIn(self.web.get("/billing/").status_code, (302, 403))
        self.assertEqual(self.web.get("/console/audit/")["Location"], "/console/2fa/")

        page = self.web.get("/console/2fa/")
        self.assertEqual(page.status_code, 200)
        secret = self.web.session["orvahr_console_totp_pending"]
        self.assertIn(totp.grouped(secret), page.content.decode())
        self.assertIn("otpauth://totp/", page.content.decode())

        bad = self.web.post("/console/2fa/", {"action": "enrol", "code": "000000"})
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(ConsoleOperator.objects.get(username="ops").has_totp)

        good = self.web.post(
            "/console/2fa/",
            {"action": "enrol", "code": totp.totp(totp.decode_secret(secret))},
        )
        self.assertEqual(good.status_code, 200)
        codes = re.findall(
            r"[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}", good.content.decode()
        )
        self.assertEqual(len(set(codes)), 10)
        op = ConsoleOperator.objects.get(username="ops")
        self.assertTrue(op.has_totp)
        self.assertEqual(op.totp_secret, secret)
        stored = list(OperatorRecoveryCode.objects.filter(operator=op))
        self.assertEqual(len(stored), 10)
        for row in stored:
            self.assertTrue(row.code_hash.startswith("sha256$"))
            self.assertNotIn(row.code_hash.split("$")[2], codes)
        self.assertEqual(self.web.get("/console/").status_code, 200)
        self.assertTrue(self.actions(action="2fa_enrolled", operator=op).exists())
        self.assertEqual(
            self.actions(action="login", operator=op).first().detail["method"],
            "enrolment",
        )

    def test_enrolment_window_expires(self):
        make_operator(with_totp=False)
        self.login()
        session = self.web.session
        session["orvahr_console_auth_at"] = int(time.time()) - 3600
        session.save()
        response = self.web.get("/console/2fa/")
        self.assertEqual(response["Location"], "/console/login/")

    @override_settings(ORVAHR_CONSOLE_REQUIRE_2FA=False)
    def test_optional_when_switched_off(self):
        make_operator(with_totp=False)
        self.assertEqual(self.login()["Location"], "/console/")
        self.assertEqual(self.web.get("/console/").status_code, 200)

    def test_optional_before_the_mandatory_date(self):
        future = (timezone.localdate() + datetime.timedelta(days=10)).isoformat()
        with override_settings(ORVAHR_CONSOLE_REQUIRE_2FA_FROM=future):
            make_operator(with_totp=False)
            self.assertEqual(self.login()["Location"], "/console/")
            page = self.web.get("/console/")
            self.assertEqual(page.status_code, 200)
            self.assertIn(future, page.content.decode())  # warning banner

    def test_mandatory_from_past_date(self):
        past = (timezone.localdate() - datetime.timedelta(days=1)).isoformat()
        with override_settings(ORVAHR_CONSOLE_REQUIRE_2FA_FROM=past):
            make_operator(with_totp=False)
            self.assertEqual(self.login()["Location"], "/console/2fa/")


class VerifyTests(ConsoleTestCase):
    def test_password_then_code(self):
        op = make_operator()
        self.assertEqual(self.login()["Location"], "/console/login/verify/")
        # Half signed in: everything redirects to the second step.
        self.assertEqual(
            self.web.get("/console/")["Location"], "/console/login/verify/"
        )
        self.assertIn(self.web.get("/billing/").status_code, (302, 403))
        self.assertEqual(self.web.get("/console/login/verify/").status_code, 200)
        wrong = self.web.post("/console/login/verify/", {"code": "123456"})
        self.assertEqual(wrong.status_code, 403)
        ok = self.verify(op)
        self.assertEqual(ok["Location"], "/console/")
        self.assertEqual(self.web.get("/console/").status_code, 200)
        login = self.actions(action="login", operator=op).first()
        self.assertEqual(login.detail["method"], "totp")
        self.assertTrue(self.actions(action="2fa_failed", operator=op).exists())
        self.assertTrue(self.actions(action="login_password_ok", operator=op).exists())

    def test_code_cannot_be_replayed(self):
        from django.test import Client

        from orvahr_saas.tests.test_public_site import HOST

        op = make_operator()
        code = current_code(op)
        self.login()
        self.assertEqual(
            self.web.post("/console/login/verify/", {"code": code}).status_code, 302
        )
        other = Client(HTTP_HOST=HOST)
        self.login(client=other)
        self.assertEqual(
            other.post("/console/login/verify/", {"code": code}).status_code, 403
        )

    def test_second_step_times_out(self):
        op = make_operator()
        self.login()
        session = self.web.session
        session["orvahr_console_auth_at"] = int(time.time()) - 600
        session.save()
        response = self.verify(op)
        self.assertEqual(response["Location"], "/console/login/")
        self.assertEqual(self.web.get("/console/").status_code, 302)

    def test_recovery_code_works_once(self):
        from orvahr_saas import twofactor

        op = make_operator()
        codes = twofactor.new_recovery_codes(op)
        self.login()
        response = self.web.post("/console/login/verify/", {"code": codes[0].upper()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.actions(action="login", operator=op).first().detail["method"],
            "recovery_code",
        )
        self.assertEqual(twofactor.remaining_recovery_codes(op), 9)
        self.web.post("/console/logout/")
        self.login()
        again = self.web.post("/console/login/verify/", {"code": codes[0]})
        self.assertEqual(again.status_code, 403)

    def test_lockout_after_repeated_wrong_codes(self):
        op = make_operator()
        self.login()
        for _ in range(5):
            self.assertEqual(
                self.web.post("/console/login/verify/", {"code": "000000"}).status_code,
                403,
            )
        response = self.verify(op)  # right code, but locked
        self.assertEqual(response.status_code, 403)
        self.assertIn("Too many wrong codes", response.content.decode())
        self.assertEqual(self.actions(action="2fa_failed", operator=op).count(), 5)
        self.assertEqual(self.actions(action="2fa_locked", operator=op).count(), 1)
        self.assertEqual(self.web.get("/console/").status_code, 302)

    def test_reset_2fa_command_ends_sessions(self):
        from orvahr_saas.models import ConsoleOperator

        self.logged_in()
        call_command(
            "create_console_operator",
            username="ops",
            reset_2fa=True,
            stdout=open(os.devnull, "w"),
        )
        op = ConsoleOperator.objects.get(username="ops")
        self.assertFalse(op.has_totp)
        self.assertFalse(op.recovery_codes.exists())
        self.assertNotEqual(self.web.get("/console/").status_code, 200)
        self.assertTrue(self.actions(action="2fa_disabled").exists())


class ManageTests(ConsoleTestCase):
    def test_regenerate_needs_a_valid_code_and_disable_refused_when_mandatory(self):
        from orvahr_saas.models import ConsoleOperator

        self.logged_in()
        op = ConsoleOperator.objects.get(username="ops")
        bad = self.web.post("/console/2fa/", {"action": "regenerate", "code": "111111"})
        self.assertEqual(bad.status_code, 400)
        good = self.web.post(
            "/console/2fa/",
            {"action": "regenerate", "code": current_code(op, offset_steps=1)},
        )
        self.assertEqual(good.status_code, 200)
        self.assertTrue(
            self.actions(action="2fa_recovery_codes_regenerated", operator=op).exists()
        )
        refused = self.web.post(
            "/console/2fa/", {"action": "disable", "code": current_code(op, -1)}
        )
        # -1 step is older than the one just used: replay protection refuses it.
        self.assertEqual(refused.status_code, 400)
        op.refresh_from_db()
        self.assertTrue(op.has_totp)

    @override_settings(ORVAHR_CONSOLE_REQUIRE_2FA=False)
    def test_disable_when_optional(self):
        from orvahr_saas.models import ConsoleOperator

        op = make_operator()
        self.login()
        self.verify(op)
        response = self.web.post(
            "/console/2fa/", {"action": "disable", "code": current_code(op, 1)}
        )
        self.assertEqual(response.status_code, 302)
        op = ConsoleOperator.objects.get(pk=op.pk)
        self.assertFalse(op.has_totp)
        self.assertEqual(self.web.get("/console/").status_code, 200)

    def test_password_check_unchanged(self):
        make_operator()
        self.assertEqual(self.login(password=STRONG + "x").status_code, 403)
