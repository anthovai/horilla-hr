"""OrvaHR Gold theme: seeding/migration behaviour, palette contrast, auth pages."""

import importlib

from django.apps import apps as django_apps
from django.test import TestCase

from horilla_theme.models import (
    CORAL_THEME_NAME,
    ORVAHR_THEME_NAME,
    THEMES_DATA,
    CompanyTheme,
    HorillaColorTheme,
)

migration = importlib.import_module("horilla_theme.migrations.0003_orvahr_gold_theme")


def _luminance(hex_colour):
    hex_colour = hex_colour.lstrip("#")
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    channels = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a, b):
    hi, lo = sorted([_luminance(a), _luminance(b)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class OrvaThemeMigrationTests(TestCase):
    def _theme_data(self, name, **extra):
        data = dict(next(t for t in THEMES_DATA if t["name"] == name))
        data.update(extra)
        return data

    def test_fresh_database_has_orvahr_gold_as_default(self):
        # The test database is built by running migrations, i.e. the
        # fresh-install path: 0003 seeds the built-in themes itself.
        self.assertEqual(
            list(
                HorillaColorTheme.objects.filter(is_default=True).values_list(
                    "name", flat=True
                )
            ),
            [ORVAHR_THEME_NAME],
        )
        self.assertTrue(HorillaColorTheme.objects.filter(name=CORAL_THEME_NAME).exists())
        self.assertEqual(HorillaColorTheme.objects.count(), len(THEMES_DATA))
        self.assertEqual(HorillaColorTheme.get_default_theme().name, ORVAHR_THEME_NAME)

    def test_existing_install_switches_stock_default_and_keeps_company_choices(self):
        HorillaColorTheme.objects.all().delete()
        HorillaColorTheme.objects.create(**self._theme_data(CORAL_THEME_NAME, is_default=True))
        ocean = HorillaColorTheme.objects.create(
            **self._theme_data("Ocean Blue Theme", is_default=False)
        )
        chosen = CompanyTheme.objects.create(theme=ocean, company=None)

        migration.forwards(django_apps, None)

        self.assertEqual(
            HorillaColorTheme.objects.get(is_default=True).name, ORVAHR_THEME_NAME
        )
        self.assertEqual(HorillaColorTheme.objects.count(), 3)  # nothing deleted
        chosen.refresh_from_db()
        self.assertEqual(chosen.theme, ocean)

        # Re-running is harmless.
        migration.forwards(django_apps, None)
        self.assertEqual(HorillaColorTheme.objects.filter(name=ORVAHR_THEME_NAME).count(), 1)

    def test_existing_install_keeps_an_admin_chosen_default(self):
        HorillaColorTheme.objects.all().delete()
        HorillaColorTheme.objects.create(**self._theme_data(CORAL_THEME_NAME, is_default=False))
        HorillaColorTheme.objects.create(
            **self._theme_data("Ocean Blue Theme", is_default=True)
        )

        migration.forwards(django_apps, None)

        self.assertEqual(HorillaColorTheme.objects.get(is_default=True).name, "Ocean Blue Theme")
        self.assertTrue(HorillaColorTheme.objects.filter(name=ORVAHR_THEME_NAME).exists())

    def test_backwards_restores_coral_default(self):
        migration.backwards(django_apps, None)
        self.assertFalse(HorillaColorTheme.objects.filter(name=ORVAHR_THEME_NAME).exists())
        self.assertEqual(HorillaColorTheme.objects.get(is_default=True).name, CORAL_THEME_NAME)


class OrvaPaletteContrastTests(TestCase):
    """WCAG AA (4.5:1) for the pairings the templates actually use."""

    theme = next(t for t in THEMES_DATA if t["name"] == ORVAHR_THEME_NAME)

    def test_white_text_on_primary_buttons(self):
        for shade in ("primary_600", "primary_700", "primary_800", "secondary_600"):
            with self.subTest(shade=shade):
                self.assertGreaterEqual(contrast("#FFFFFF", self.theme[shade]), 4.5)

    def test_primary_text_on_tints(self):
        for tint in ("primary_50", "primary_100", "primary_200", "primary_300"):
            with self.subTest(tint=tint):
                self.assertGreaterEqual(
                    contrast(self.theme["primary_600"], self.theme[tint]), 4.5
                )

    def test_charcoal_text_on_brand_gold(self):
        self.assertGreaterEqual(contrast(self.theme["dark_500"], self.theme["primary_500"]), 4.5)
        self.assertGreaterEqual(contrast(self.theme["dark_500"], "#B8904F"), 4.5)


class AuthPageBrandingTests(TestCase):
    def test_login_page_is_branded_and_self_hosted(self):
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("orvahr-brand.css", html)
        self.assertIn("orva-btn-primary", html)
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertNotIn("fonts.gstatic.com", html)
