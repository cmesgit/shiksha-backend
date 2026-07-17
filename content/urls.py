# PLACEMENT: backend/content/urls.py
#
# Mount in config/urls.py:
#     path("api/content/", include("content.urls")),
#
# NOTE: the blog detail route uses <path:slug> because blog slugs contain
# slashes (class-9/economics/chapter-1) — keep it LAST among /blogs/ routes.

from django.urls import path
from rest_framework.routers import DefaultRouter

from . import admin_views, views

app_name = "content"

urlpatterns = [
    path("blogs/", views.BlogPostListView.as_view(), name="blog-list"),
    path("blogs/<path:slug>/", views.BlogPostDetailView.as_view(), name="blog-detail"),

    path("current-affairs/", views.CurrentAffairListView.as_view(), name="ca-list"),
    path("current-affairs/<slug:slug>/", views.CurrentAffairDetailView.as_view(), name="ca-detail"),

    path("faqs/", views.FAQListView.as_view(), name="faq-list"),
    path("announcements/", views.AnnouncementListView.as_view(), name="announcement-list"),
    path("showcase/", views.ShowcaseListView.as_view(), name="showcase-list"),
]

# ── Staff-only CMS admin API (content/admin_views.py) ──────────────
admin_router = DefaultRouter()
admin_router.register("admin/blogs", admin_views.BlogPostAdminViewSet, basename="admin-blog")
admin_router.register("admin/current-affairs", admin_views.CurrentAffairAdminViewSet, basename="admin-ca")
admin_router.register("admin/faqs", admin_views.FAQItemAdminViewSet, basename="admin-faq")
admin_router.register("admin/announcements", admin_views.AnnouncementAdminViewSet, basename="admin-announcement")
admin_router.register("admin/showcase", admin_views.ShowcaseCourseAdminViewSet, basename="admin-showcase")
admin_router.register("admin/tags", admin_views.TagAdminViewSet, basename="admin-tag")
urlpatterns += admin_router.urls
