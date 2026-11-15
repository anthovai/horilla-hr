"""Fixtures: seller settings, bank account, billing profiles for the SaaS test tenants."""

import datetime
import shutil
import tempfile
from decimal import Decimal

from django.test import override_settings

from employee.thai_validators import thai_national_id_checksum_digit
from orvahr_saas.tests.base import SaasTestCase


def tax_id(first_twelve):
    return first_twelve + str(thai_national_id_checksum_digit(first_twelve))


SELLER_TAX_ID = tax_id("010555012345")
BUYER_TAX_ID = tax_id("010556098765")
D = Decimal


class BillingTestCase(SaasTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._private_root = tempfile.mkdtemp(prefix="orvahr-billing-test-")
        cls._override = override_settings(ORVAHR_BILLING_PRIVATE_ROOT=cls._private_root)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._private_root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        from orvahr_billing.models import BankAccount, BillingProfile, SellerSettings

        self.seller = SellerSettings(
            legal_name_th="บริษัท ออร์วา เอชอาร์ จำกัด",
            legal_name_en="OrvaHR Co., Ltd.",
            tax_id=SELLER_TAX_ID,
            branch_code="00000",
            address_th="99 ถนนสุขุมวิท แขวงคลองเตย เขตคลองเตย กรุงเทพมหานคร 10110",
            address_en="99 Sukhumvit Rd, Khlong Toei, Bangkok 10110",
            email="billing@orvahr.test",
            vat_registered=True,
            vat_rate=D("7.00"),
            payment_terms_days=15,
        )
        self.seller.full_clean()
        self.seller.save()
        BankAccount.objects.create(
            bank_name="ธนาคารกสิกรไทย",
            account_name="บริษัท ออร์วา เอชอาร์ จำกัด",
            account_number="123-4-56789-0",
        )
        self.profile = BillingProfile(
            tenant=self.alpha,
            legal_name="บริษัท อัลฟา จำกัด",
            tax_id=BUYER_TAX_ID,
            is_vat_registered=True,
            branch_code="00001",
            address="1 ถนนสีลม กรุงเทพมหานคร 10500",
            email="ap@alpha.test",
        )
        self.profile.full_clean()
        self.profile.save()
        BillingProfile.objects.create(
            tenant=self.beta,
            legal_name="Beta Person",
            is_vat_registered=False,
            branch_code="",
            address="2 Rama IV, Bangkok",
        )

    # helpers -----------------------------------------------------------

    def make_invoice(
        self, amount=D("1000.00"), tenant=None, issue_date=None, issue=True, **kw
    ):
        from orvahr_billing import services

        doc = services.create_invoice(
            tenant or self.alpha,
            [
                {
                    "description": "OrvaHR subscription",
                    "quantity": 1,
                    "unit_price": amount,
                }
            ],
            issue_date=issue_date or datetime.date(2026, 11, 1),
            **kw,
        )
        return services.issue(doc, by="tester") if issue else doc
