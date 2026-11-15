"""
Seed the PDPA consent purpose ``biometric_face`` (global, employee audience).

It is optional in orvahr_pdpa's sense: explicit, separate, and withdrawable
at any time. Face enrolment refuses without an active grant. Skipped when
orvahr_pdpa is not installed. The text is legal content: have counsel review
it; edit it under PDPA settings > Consent purposes.
"""

from django.conf import settings
from django.db import migrations

from orvahr_face.consent import PURPOSE_CODE, PURPOSE_NAME, PURPOSE_NAME_TH, PURPOSE_TEXT


def seed(apps, schema_editor):
    try:
        ConsentPurpose = apps.get_model("orvahr_pdpa", "ConsentPurpose")
    except LookupError:
        return
    if ConsentPurpose.objects.filter(company__isnull=True, audience="employee", code=PURPOSE_CODE).exists():
        return
    ConsentPurpose.objects.create(
        company=None,
        audience="employee",
        code=PURPOSE_CODE,
        name=PURPOSE_NAME,
        name_th=PURPOSE_NAME_TH,
        description=PURPOSE_TEXT,
        optional=True,
        is_active=True,
    )


def unseed(apps, schema_editor):
    try:
        ConsentPurpose = apps.get_model("orvahr_pdpa", "ConsentPurpose")
        ConsentRecord = apps.get_model("orvahr_pdpa", "ConsentRecord")
    except LookupError:
        return
    purposes = ConsentPurpose.objects.filter(company__isnull=True, code=PURPOSE_CODE)
    # Consent records are evidence (PROTECT): keep the purpose when any exist.
    if not ConsentRecord.objects.filter(purpose__in=purposes).exists():
        purposes.delete()


dependencies = [("orvahr_face", "0001_initial")]
if "orvahr_pdpa" in settings.INSTALLED_APPS:
    dependencies.append(("orvahr_pdpa", "0001_initial"))


class Migration(migrations.Migration):
    dependencies = dependencies

    operations = [migrations.RunPython(seed, unseed)]
