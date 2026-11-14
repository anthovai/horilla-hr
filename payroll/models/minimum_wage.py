"""
Statutory minimum daily wage by province.

Thailand sets its minimum wage per province, by announcement of the Wage
Committee, and revises it from a stated date. That is DATA with an effective
date, so it lives in a table rather than in code, and a revision is a new row
rather than an edit: a past payroll has to be checkable against the rate that
applied at the time.

Nothing is seeded. Rates are loaded from the official announcement with
``manage.py load_th_minimum_wage --file <csv>``; the sample CSV shipped beside
that command is a format example, not the law.
"""

import datetime
import re

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from horilla.models import HorillaModel

# ISO 3166-2:TH -- "TH-" then two digits (TH-10 is Bangkok), or TH-S for
# Pattaya City, the one special administrative code.
PROVINCE_CODE_RE = r"^TH-(\d{2}|S)$"


class MinimumWageRate(HorillaModel):
    """
    One province's minimum daily wage from a given date.

    Not company-scoped: the rate is national law for the province, the same
    for every company that employs someone there.
    """

    province_code = models.CharField(
        max_length=8,
        validators=[
            RegexValidator(
                PROVINCE_CODE_RE,
                _("Use the ISO 3166-2:TH code, for example TH-10 for Bangkok."),
            )
        ],
        db_index=True,
        verbose_name=_("Province code"),
        help_text=_("ISO 3166-2:TH, for example TH-10 for Bangkok."),
    )
    province_name = models.CharField(
        max_length=100,
        verbose_name=_("Province"),
    )
    daily_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name=_("Minimum daily wage"),
    )
    effective_from = models.DateField(verbose_name=_("Effective from"))
    note = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("Note"),
        help_text=_("Where the figure comes from, e.g. the announcement it was taken from."),
    )

    class Meta:
        ordering = ["province_code", "-effective_from"]
        verbose_name = _("Minimum Wage Rate")
        verbose_name_plural = _("Minimum Wage Rates")
        constraints = [
            models.UniqueConstraint(
                fields=["province_code", "effective_from"],
                name="unique_minimum_wage_province_date",
            )
        ]

    def clean(self):
        # Checked here as well as by the field validators: HorillaModel's
        # clean_fields only screens text for XSS and never runs them.
        super().clean()
        errors = {}
        self.province_code = (self.province_code or "").strip().upper()
        if not re.match(PROVINCE_CODE_RE, self.province_code):
            errors["province_code"] = _(
                "Use the ISO 3166-2:TH code, for example TH-10 for Bangkok."
            )
        if not (self.province_name or "").strip():
            errors["province_name"] = _("This field is required")
        if self.daily_amount is None or self.daily_amount <= 0:
            errors["daily_amount"] = _("The minimum daily wage must be more than zero.")
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.province_code} {self.province_name} {self.daily_amount} ({self.effective_from})"

    @classmethod
    def rate_for(cls, province, as_of=None):
        """
        The rate in force on ``as_of`` (default today) for a province given by
        its code or its name, or None when there is none.

        A name is matched without regard to case or surrounding space, because
        it usually comes from a free-text field (a company's state).
        """
        key = (province or "").strip()
        if not key:
            return None
        as_of = as_of or datetime.date.today()
        rows = cls.objects.filter(effective_from__lte=as_of, is_active=True)
        by_code = rows.filter(province_code__iexact=key)
        if by_code.exists():
            return by_code.order_by("-effective_from").first()
        return rows.filter(province_name__iexact=key).order_by("-effective_from").first()
