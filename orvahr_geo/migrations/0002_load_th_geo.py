"""Load the shipped Thai geography fixture (re-runnable: ``load_th_geo``)."""

from django.db import migrations


def load(apps, schema_editor):
    from orvahr_geo.loader import load_th_geo

    load_th_geo(
        apps.get_model("orvahr_geo", "Province"),
        apps.get_model("orvahr_geo", "District"),
        apps.get_model("orvahr_geo", "Subdistrict"),
        bump_cache=False,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("orvahr_geo", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(load, migrations.RunPython.noop),
    ]
