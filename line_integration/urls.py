from django.urls import path

from line_integration import views, webhook

urlpatterns = [
    path("webhook/<uuid:webhook_key>/", webhook.line_webhook, name="line-webhook"),
    path("settings/", views.line_settings, name="line-settings"),
    path("settings/channel/create/", views.channel_create, name="line-channel-create"),
    path("settings/channel/<int:pk>/update/", views.channel_update, name="line-channel-update"),
    path("settings/channel/<int:pk>/delete/", views.channel_delete, name="line-channel-delete"),
    path("settings/test-message/", views.send_test_message, name="line-test-message"),
    path("my-link/", views.my_line_link, name="line-my-link"),
]
