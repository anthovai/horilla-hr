"""
Seed the commonly cited Labour Protection Act s.118 severance bands.

Service is counted in days; one year is taken as 365 days. These are data,
not law: verify every band and the effective date against current law
before relying on them, and add a new set with a later effective date when
the law changes (the newest set effective on the last working day is used).
"""

import datetime

from django.db import migrations

EFFECTIVE_FROM = datetime.date(2019, 5, 5)  # LPA (No. 7) B.E. 2562 -- verify
NOTE = "Commonly cited LPA s.118 band. Verify against current law."
REFERENCE = "Labour Protection Act B.E. 2541 s.118 (as amended)"

BANDS = [
    # (min_days, max_days, days_of_wages)
    (120, 365, 30),
    (365, 3 * 365, 90),
    (3 * 365, 6 * 365, 180),
    (6 * 365, 10 * 365, 240),
    (10 * 365, 20 * 365, 300),
    (20 * 365, None, 400),
]


def seed(apps, schema_editor):
    SeveranceRule = apps.get_model("orvahr_benefits", "SeveranceRule")
    if SeveranceRule.objects.filter(effective_from=EFFECTIVE_FROM).exists():
        return
    SeveranceRule.objects.bulk_create(
        [
            SeveranceRule(
                min_days=low,
                max_days=high,
                days_of_wages=days,
                effective_from=EFFECTIVE_FROM,
                legal_reference=REFERENCE,
                note=NOTE,
            )
            for low, high, days in BANDS
        ]
    )


def unseed(apps, schema_editor):
    SeveranceRule = apps.get_model("orvahr_benefits", "SeveranceRule")
    SeveranceRule.objects.filter(effective_from=EFFECTIVE_FROM, note=NOTE).delete()


class Migration(migrations.Migration):
    dependencies = [("orvahr_benefits", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
