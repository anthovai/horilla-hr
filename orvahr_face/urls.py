"""Face check-in URLs (mounted under /face/ by OrvahrFaceConfig.ready)."""

from django.urls import path

from orvahr_face import views

app_name = "orvahr_face"

urlpatterns = [
    path("", views.my_face, name="my-face"),
    path("enrol/", views.enrol_page, name="enrol"),
    path("enrol/<int:employee_id>/", views.enrol_page, name="enrol-employee"),
    path("check-in/", views.check_in_page, name="check-in"),
    path("api/enrol/", views.enrol_api, name="api-enrol"),
    path("api/check-in/", views.check_in_api, name="api-check-in"),
    path("enrolments/", views.enrolments, name="enrolments"),
    path("enrolments/<int:employee_id>/delete/", views.enrolment_delete, name="enrolment-delete"),
    path("settings/", views.face_settings, name="settings"),
]
