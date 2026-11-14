"""
Add the OrvaHR Gold colour theme and make it the default.

* Fresh database (no themes yet): seed the full built-in theme list, with
  OrvaHR Gold as the default. The post_migrate seeding signal then sees
  themes already exist and does nothing, exactly as before.
* Existing database: create OrvaHR Gold if it is missing and make it the
  default only when the current default is still the stock Coral Red (or
  there is none). A default an admin picked deliberately is left alone.

Nothing is deleted, and CompanyTheme rows (a company's own selected theme)
are never touched, so companies that chose a theme keep it.
"""

from django.db import migrations

ORVAHR_THEME_NAME = "OrvaHR Gold Theme"
CORAL_THEME_NAME = "Coral Red Theme (Default)"


def _theme_rows():
    # The built-in palette data lives with the model; only keep keys the
    # historical model knows, so later field changes cannot break this step.
    from horilla_theme.models import THEMES_DATA

    return THEMES_DATA


def forwards(apps, schema_editor):
    Theme = apps.get_model("horilla_theme", "HorillaColorTheme")
    field_names = {f.name for f in Theme._meta.get_fields()}
    rows = [
        {k: v for k, v in row.items() if k in field_names} for row in _theme_rows()
    ]

    if not Theme.objects.exists():
        for row in rows:
            Theme.objects.get_or_create(name=row["name"], defaults=row)
        return

    orva_row = next(r for r in rows if r["name"] == ORVAHR_THEME_NAME)
    orva, _created = Theme.objects.get_or_create(
        name=ORVAHR_THEME_NAME, defaults={**orva_row, "is_default": False}
    )

    current_defaults = Theme.objects.filter(is_default=True).exclude(pk=orva.pk)
    if current_defaults.exclude(name=CORAL_THEME_NAME).exists():
        return  # an admin chose another default; respect it
    current_defaults.update(is_default=False)
    Theme.objects.filter(pk=orva.pk).update(is_default=True)


def backwards(apps, schema_editor):
    Theme = apps.get_model("horilla_theme", "HorillaColorTheme")
    orva = Theme.objects.filter(name=ORVAHR_THEME_NAME).first()
    if not orva:
        return
    if orva.is_default:
        Theme.objects.filter(name=CORAL_THEME_NAME).update(is_default=True)
    orva.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("horilla_theme", "0002_alter_companytheme_is_active_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
