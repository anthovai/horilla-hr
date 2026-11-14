"""URLs for orvahr_geo."""

from django.urls import path

from orvahr_geo import views

app_name = "orvahr_geo"

urlpatterns = [
    path("provinces/", views.provinces, name="provinces"),
    path("districts/", views.districts, name="districts"),
    path("subdistricts/", views.subdistricts, name="subdistricts"),
    path("postcode/", views.postcode_search, name="postcode-search"),
]
