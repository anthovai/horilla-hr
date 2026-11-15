"""
protected_media responses must never be stored by a shared cache: media can
be served through Cloudflare, access is decided per user, and in SaaS mode the
same /media/ path is another tenant's file on every host.
"""

import os
import shutil
import tempfile

from django.test import TestCase, override_settings

from base.models import Company
from horilla.testkit import make_employee, make_user


class ProtectedMediaCacheControlTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, True)
        os.makedirs(os.path.join(self.media_root, "base", "icon"))
        with open(os.path.join(self.media_root, "base", "icon", "logo.png"), "wb") as fh:
            fh.write(b"png")
        with open(os.path.join(self.media_root, "visible.txt"), "w") as fh:
            fh.write("visible")

    def test_public_asset_is_private_no_store(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            response = self.client.get("/media/base/icon/logo.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_authenticated_file_is_private_no_store(self):
        company = Company.objects.create(company="Acme", hq=True)
        user = make_user("emp", password="pw-not-real")
        make_employee(company=company, email="emp@test.horilla", user=user)
        self.client.force_login(user)
        with override_settings(MEDIA_ROOT=self.media_root):
            response = self.client.get("/media/visible.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
