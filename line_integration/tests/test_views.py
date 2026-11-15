from django.test import Client
from django.urls import reverse

from line_integration.models import LineChannel, LineLinkCode, LineUserLink
from line_integration.tests.helpers import FAKE_SECRET, FAKE_TOKEN, LineTestCase


class PermissionTests(LineTestCase):
    def setUp(self):
        super().setUp()
        self.staff = self.make_emp("plain")
        self.admin = self.make_emp("boss", is_superuser=True)

    def _client(self, emp):
        c = Client()
        c.force_login(emp.employee_user_id)
        return c

    def test_plain_employee_cannot_open_settings(self):
        r = self._client(self.staff).get(reverse("line-settings"))
        self.assertNotContains(r, "line-settings-view", status_code=r.status_code)
        self.assertNotIn(FAKE_TOKEN[-4:].encode() + b"</code>", r.content)

    def test_plain_employee_cannot_create_channel(self):
        before = LineChannel.all_objects.count()
        self._client(self.staff).post(
            reverse("line-channel-create"),
            {"name": "evil", "access_token": "x", "channel_secret": "y", "is_active": "on"},
        )
        self.assertEqual(LineChannel.all_objects.count(), before)

    def test_plain_employee_cannot_delete_or_test(self):
        c = self._client(self.staff)
        c.post(reverse("line-channel-delete", args=[self.channel.pk]))
        self.assertTrue(LineChannel.all_objects.filter(pk=self.channel.pk).exists())
        LineUserLink.objects.create(employee=self.staff, channel=self.channel, line_user_id="Up")
        c.post(reverse("line-test-message"))
        self.assertFalse(self.http.called)

    def test_anonymous_redirected(self):
        r = Client().get(reverse("line-settings"))
        self.assertEqual(r.status_code, 302)

    def test_admin_settings_page_masks_credentials(self):
        r = self._client(self.admin).get(reverse("line-settings"))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(FAKE_TOKEN.encode(), r.content)
        self.assertNotIn(FAKE_SECRET.encode(), r.content)
        self.assertIn(self.channel.webhook_url().encode(), r.content)

    def test_admin_edit_form_does_not_render_credentials(self):
        r = self._client(self.admin).get(reverse("line-channel-update", args=[self.channel.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(FAKE_TOKEN.encode(), r.content)
        self.assertNotIn(FAKE_SECRET.encode(), r.content)

    def test_admin_create_encrypts_and_blank_edit_keeps(self):
        c = self._client(self.admin)
        r = c.post(
            reverse("line-channel-create"),
            {"name": "Default OA", "access_token": "tok-fake-2", "channel_secret": "sec-fake-2",
             "is_active": "on"},
        )
        self.assertEqual(r.status_code, 302)
        ch = LineChannel.all_objects.get(name="Default OA")
        self.assertNotIn("tok-fake-2", ch.access_token_encrypted)
        self.assertEqual(ch.access_token, "tok-fake-2")
        # "testserver" has no TLD, so it is not stored as the public URL; a
        # real host (a tenant's own, in SaaS mode) is.
        host = r.wsgi_request.get_host()
        self.assertEqual(ch.public_base_url, "" if "." not in host else f"http://{host}")
        c.post(
            reverse("line-channel-update", args=[ch.pk]),
            {"name": "Renamed", "access_token": "", "channel_secret": "", "is_active": "on",
             "public_base_url": "https://hr.example.test"},
        )
        ch.refresh_from_db()
        self.assertEqual(ch.name, "Renamed")
        self.assertEqual(ch.access_token, "tok-fake-2")
        self.assertEqual(ch.channel_secret, "sec-fake-2")

    def test_admin_test_message_to_self(self):
        LineUserLink.objects.create(employee=self.admin, channel=self.channel, line_user_id="Uboss")
        self._client(self.admin).post(reverse("line-test-message"))
        self.assertEqual(self.http.call_args.kwargs["json"]["to"], "Uboss")

    def test_admin_test_message_requires_link(self):
        self._client(self.admin).post(reverse("line-test-message"))
        self.assertFalse(self.http.called)


class SelfServiceTests(LineTestCase):
    def setUp(self):
        super().setUp()
        self.emp = self.make_emp("selfie")
        self.c = Client()
        self.c.force_login(self.emp.employee_user_id)

    def test_status_page(self):
        r = self.c.get(reverse("line-my-link"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "@fake123")

    def test_generate_code_shows_it_once(self):
        r = self.c.post(reverse("line-my-link"), {"action": "generate"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertEqual(LineLinkCode.objects.filter(employee=self.emp).count(), 1)
        self.assertRegex(r.content.decode(), r"<code>[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}</code>")

    def test_unlink(self):
        LineUserLink.objects.create(employee=self.emp, channel=self.channel, line_user_id="Uself")
        self.c.post(reverse("line-my-link"), {"action": "unlink"})
        self.assertFalse(LineUserLink.objects.filter(employee=self.emp).exists())

    def test_cannot_unlink_someone_else(self):
        other = self.make_emp("other2")
        LineUserLink.objects.create(employee=other, channel=self.channel, line_user_id="Uother2")
        self.c.post(reverse("line-my-link"), {"action": "unlink", "employee": other.pk})
        self.assertTrue(LineUserLink.objects.filter(employee=other).exists())
