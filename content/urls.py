# PLACEMENT: backend/content/urls.py
#
# Mount in config/urls.py:
#     path("api/content/", include("content.urls")),
#
# NOTE: the blog detail route uses <path:slug> because blog slugs contain
# slashes (class-9/economics/chapter-1) — keep it LAST among /blogs/ routes.

from django.urls import path

from . import views

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
