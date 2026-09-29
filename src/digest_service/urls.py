from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path

from digest_service.core.views import dashboard, health
from digest_service.telegram_bot.views import telegram_webhook

admin.site.site_header = "Наши AI-дайджесты"
admin.site.site_title = "Управление дайджестами"
admin.site.index_title = "Управление сервисом"

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("health/", health),
    path("telegram/webhook/", telegram_webhook, name="telegram_webhook"),
    path("admin/", admin.site.urls),
]
urlpatterns += staticfiles_urlpatterns()
