"""
SAMPLE plans (Starter / Business / Enterprise) so a fresh control plane has
something to show and assign. They are examples -- edit or replace them
before selling anything. Prices are not here: they belong to orvahr_billing.

Also bridges the old free-text ``Tenant.plan_code`` to the new ``plan`` FK
for tenants whose code matches a plan.
"""

from django.db import migrations

SAMPLE_PLANS = [
    {
        "code": "starter",
        "name_en": "Starter (sample)",
        "name_th": "Starter (ตัวอย่าง)",
        "description_en": "Employee records, attendance and leave for small teams.",
        "description_th": "ข้อมูลพนักงาน การลงเวลา และการลาสำหรับทีมขนาดเล็ก",
        "max_employees": 25,
        "modules": ["attendance", "leave"],
        "trial_days": 14,
        "sort": 10,
    },
    {
        "code": "business",
        "name_en": "Business (sample)",
        "name_th": "Business (ตัวอย่าง)",
        "description_en": "Thai payroll, benefits, recruitment and assets for growing companies.",
        "description_th": "เงินเดือนตามกฎหมายไทย สวัสดิการ การสรรหา และทรัพย์สินสำหรับบริษัทที่กำลังเติบโต",
        "max_employees": 200,
        "modules": [
            "attendance",
            "leave",
            "payroll",
            "orvahr_benefits",
            "recruitment",
            "onboarding",
            "offboarding",
            "asset",
        ],
        "trial_days": 14,
        "sort": 20,
    },
    {
        "code": "enterprise",
        "name_en": "Enterprise (sample)",
        "name_th": "Enterprise (ตัวอย่าง)",
        "description_en": "Every module, no employee limit.",
        "description_th": "ทุกโมดูล ไม่จำกัดจำนวนพนักงาน",
        "max_employees": None,
        "modules": [
            "attendance",
            "leave",
            "payroll",
            "orvahr_benefits",
            "recruitment",
            "onboarding",
            "offboarding",
            "pms",
            "project",
            "helpdesk",
            "asset",
        ],
        "trial_days": 30,
        "sort": 30,
    },
]


def seed(apps, schema_editor):
    Plan = apps.get_model("orvahr_saas", "Plan")
    Tenant = apps.get_model("orvahr_saas", "Tenant")
    for data in SAMPLE_PLANS:
        Plan.objects.get_or_create(code=data["code"], defaults=data)
    for tenant in Tenant.objects.filter(plan__isnull=True).exclude(plan_code=""):
        plan = Plan.objects.filter(code=tenant.plan_code).first()
        if plan is not None:
            Tenant.objects.filter(pk=tenant.pk).update(plan=plan)


def unseed(apps, schema_editor):
    Plan = apps.get_model("orvahr_saas", "Plan")
    Plan.objects.filter(
        code__in=[p["code"] for p in SAMPLE_PLANS], tenants__isnull=True
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        (
            "orvahr_saas",
            "0002_consoleoperator_plan_tenant_status_before_suspension_and_more",
        ),
    ]

    operations = [migrations.RunPython(seed, unseed)]
