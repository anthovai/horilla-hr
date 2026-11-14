from django.contrib import admin

from orvahr_geo.models import District, Province, Subdistrict


@admin.register(Province)
class ProvinceAdmin(admin.ModelAdmin):
    list_display = ("code", "iso_code", "name_th", "name_en")
    search_fields = ("code", "name_th", "name_en")


@admin.register(District)
class DistrictAdmin(admin.ModelAdmin):
    list_display = ("code", "name_th", "name_en", "province")
    search_fields = ("code", "name_th", "name_en")
    list_select_related = ("province",)


@admin.register(Subdistrict)
class SubdistrictAdmin(admin.ModelAdmin):
    list_display = ("code", "name_th", "name_en", "postcodes", "district")
    search_fields = ("code", "name_th", "name_en", "postcodes")
    list_select_related = ("district",)
