"""
Thai administrative areas: province (changwat, incl. Bangkok), district
(amphoe/khet) and subdistrict (tambon/khwaeng) with postcodes.

Reference data shared by all companies, loaded from ``data/th_geo.json`` by
the 0002 data migration and the ``load_th_geo`` command. Codes are the
DOPA administrative codes (2/4/6 digits, each prefixed by its parent's).
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class Province(models.Model):
    code = models.CharField(max_length=2, unique=True, verbose_name=_("Code"))
    # ISO 3166-2:TH ("TH-10" for Bangkok). For the 77 provinces the ISO
    # number equals the DOPA province code.
    iso_code = models.CharField(
        max_length=6, unique=True, verbose_name=_("ISO 3166-2 code")
    )
    name_th = models.CharField(max_length=100, verbose_name=_("Name (Thai)"))
    name_en = models.CharField(max_length=100, verbose_name=_("Name (English)"))

    class Meta:
        ordering = ["name_th"]
        verbose_name = _("Province")
        verbose_name_plural = _("Provinces")

    def __str__(self):
        return self.name_th


class District(models.Model):
    code = models.CharField(max_length=4, unique=True, verbose_name=_("Code"))
    province = models.ForeignKey(
        Province,
        on_delete=models.CASCADE,
        related_name="districts",
        verbose_name=_("Province"),
    )
    name_th = models.CharField(max_length=100, verbose_name=_("Name (Thai)"))
    name_en = models.CharField(max_length=100, verbose_name=_("Name (English)"))

    class Meta:
        ordering = ["name_th"]
        verbose_name = _("District")
        verbose_name_plural = _("Districts")

    def __str__(self):
        return self.name_th


class Subdistrict(models.Model):
    code = models.CharField(max_length=6, unique=True, verbose_name=_("Code"))
    district = models.ForeignKey(
        District,
        on_delete=models.CASCADE,
        related_name="subdistricts",
        verbose_name=_("District"),
    )
    name_th = models.CharField(max_length=100, verbose_name=_("Name (Thai)"))
    name_en = models.CharField(max_length=100, verbose_name=_("Name (English)"))
    # The primary postcode (the one covering most of the subdistrict).
    postcode = models.CharField(max_length=5, db_index=True, verbose_name=_("Postcode"))
    # Every postcode serving the subdistrict, space separated, primary first.
    # 86 subdistricts are served by more than one.
    postcodes = models.CharField(max_length=64, verbose_name=_("All postcodes"))

    class Meta:
        ordering = ["name_th"]
        verbose_name = _("Subdistrict")
        verbose_name_plural = _("Subdistricts")

    def __str__(self):
        return self.name_th

    @property
    def postcode_list(self):
        return self.postcodes.split() or [self.postcode]
