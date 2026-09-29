from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path, re_path
from django.views.static import serve

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
if not settings.DEBUG:
    # ponytail: Django serves collected admin static where no nginx sits in front (Railway);
    # nginx still intercepts /static/ on Debian. Move to WhiteNoise/CDN if static traffic grows.
    urlpatterns.append(
        re_path(r"^static/(?P<path>.*)$", serve, {"document_root": settings.STATIC_ROOT})
    )
