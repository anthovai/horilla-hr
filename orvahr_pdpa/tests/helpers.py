from django.contrib.auth.models import Group
from django.test import Client, TestCase

from horilla.testkit import make_company, make_employee, make_user
from orvahr_pdpa.apps import OFFICER_GROUP, ensure_officer_group


def thai_id(prefix12):
    """A checksum-valid 13-digit Thai ID from 12 digits."""
    digits = [int(c) for c in prefix12]
    check = (11 - sum(d * (13 - i) for i, d in enumerate(digits)) % 11) % 10
    return prefix12 + str(check)


class PdpaTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.company = make_company("PDPA Co")
        ensure_officer_group()

    def make_emp(self, username, first_name="Test", company=None, **kwargs):
        user = make_user(username, password="pass", **kwargs)
        return make_employee(
            company=company or self.company,
            email=f"{username}@test.horilla",
            first_name=first_name,
            user=user,
        )

    def make_officer(self, username="officer"):
        emp = self.make_emp(username, first_name="Officer")
        from base.models import CompanyGroupAssignment

        group = Group.objects.get(name=OFFICER_GROUP)
        user = emp.employee_user_id
        user.groups.add(group)
        # COMPANY_SCOPED_PERMISSIONS: group perms resolve per company assignment.
        CompanyGroupAssignment.objects.create(user=user, company=self.company, group=group)
        return emp

    def client_for(self, emp):
        client = Client()
        client.force_login(emp.employee_user_id)
        return client
