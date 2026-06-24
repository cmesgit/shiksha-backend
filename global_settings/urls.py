"""
global_settings/urls.py  (NEW FILE — replaces the empty one)

Mounted in config/urls.py under /api/admin/  (see install note).
"""
from django.urls import path
from .views import AdminGlobalSettingsView

urlpatterns = [
    path("settings/", AdminGlobalSettingsView.as_view()),
]
