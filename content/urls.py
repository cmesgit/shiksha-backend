# PLACEMENT: backend/content/urls.py
#
# Mount in config/urls.py:
#     path("api/content/", include("content.urls")),
#
# NOTE: the blog detail route uses <path:slug> because blog slugs contain
# slashes (class-9/economics/chapter-1) — keep it LAST among /blogs/ routes.

from django.urls import path
from rest_framework.routers import DefaultRouter

from . import (admin_views, ai_views, contact_views, forms_views,
               redirect_views, studio_views, views)

app_name = "content"

urlpatterns = [
    path("ai/general-studies/", ai_views.GeneralStudiesAIView.as_view(), name="general-studies-ai"),

    # The public contact form's only write endpoint. Anonymous + throttled —
    # see content/contact_views.py for the threat model.
    path("contact/", contact_views.ContactMessageCreateView.as_view(), name="contact-create"),
    path("newsletter/", contact_views.NewsletterSubscribeView.as_view(), name="newsletter-subscribe"),

    path("blogs/", views.BlogPostListView.as_view(), name="blog-list"),
    # BEFORE the detail route, not after. `<path:slug>` matches slashes, so
    # `blogs/<path:slug>/` would otherwise swallow this as a post whose slug
    # ends in "/related" — a 404 that looks like missing content.
    path("blogs/<path:slug>/related/", views.BlogPostRelatedView.as_view(), name="blog-related"),
    path("blogs/<path:slug>/", views.BlogPostDetailView.as_view(), name="blog-detail"),

    path("current-affairs/", views.CurrentAffairListView.as_view(), name="ca-list"),
    path("current-affairs/<slug:slug>/", views.CurrentAffairDetailView.as_view(), name="ca-detail"),

    path("faqs/", views.FAQListView.as_view(), name="faq-list"),
    path("announcements/", views.AnnouncementListView.as_view(), name="announcement-list"),
    path("showcase/", views.ShowcaseListView.as_view(), name="showcase-list"),

    path("home-content/", views.HomeContentListView.as_view(), name="home-content-list"),
    path("home-list-items/", views.HomeListItemListView.as_view(), name="home-list-item-list"),
    path("home-floaters/", views.HomeFloaterListView.as_view(), name="home-floater-list"),
    path("home-section-order/", views.HomeSectionOrderListView.as_view(), name="home-section-order-list"),

    path("demo-videos/", views.DemoVideoListView.as_view(), name="demo-video-list"),

    path("home/hero-highlights/", views.HomeHeroHighlightsView.as_view(), name="home-hero-highlights"),
]

# ── Staff-only CMS admin API (content/admin_views.py) ──────────────
admin_router = DefaultRouter()
admin_router.register("admin/blogs", admin_views.BlogPostAdminViewSet, basename="admin-blog")
admin_router.register("admin/current-affairs", admin_views.CurrentAffairAdminViewSet, basename="admin-ca")
admin_router.register("admin/faqs", admin_views.FAQItemAdminViewSet, basename="admin-faq")
admin_router.register("admin/announcements", admin_views.AnnouncementAdminViewSet, basename="admin-announcement")
admin_router.register("admin/showcase", admin_views.ShowcaseCourseAdminViewSet, basename="admin-showcase")
# Registered AFTER admin/showcase, but the router matches on the full prefix so
# "admin/showcase-categories" cannot be swallowed by "admin/showcase"'s detail
# route — that would need a slash. Kept as a sibling rather than nested under
# showcase/ because a tab is not owned by any one card.
admin_router.register(
    "admin/showcase-categories",
    admin_views.ShowcaseCategoryAdminViewSet,
    basename="admin-showcase-category",
)
admin_router.register("admin/tags", admin_views.TagAdminViewSet, basename="admin-tag")
admin_router.register("admin/home-content", admin_views.HomeContentBlockAdminViewSet, basename="admin-home-content")
admin_router.register("admin/home-list-items", admin_views.HomeListItemAdminViewSet, basename="admin-home-list-item")
admin_router.register("admin/home-floaters", admin_views.HomeFloaterAdminViewSet, basename="admin-home-floater")
admin_router.register("admin/home-section-order", admin_views.HomeSectionOrderAdminViewSet, basename="admin-home-section-order")
admin_router.register("admin/demo-videos", admin_views.DemoVideoAdminViewSet, basename="admin-demo-video")
admin_router.register("admin/editor-images", admin_views.ContentImageAdminViewSet, basename="admin-editor-image")
urlpatterns += admin_router.urls

# ── Content Studio workflow (content/studio_views.py) ──────────────
# design_handoff_content_studio Phase 1b. These sit alongside the CRUD router
# above, so the full paths are /api/content/admin/… — the handoff spec's
# /admin/content/… is wrong on every row.
urlpatterns += [
    path(
        "admin/pages/<slug:key>/checklist/",
        studio_views.PageChecklistView.as_view(), name="studio-page-checklist",
    ),
    path("admin/link-targets/", studio_views.LinkTargetsView.as_view(), name="studio-link-targets"),
    path(
        "admin/exams/readiness/",
        studio_views.ExamReadinessView.as_view(), name="studio-exam-readiness",
    ),
    # Keep these ABOVE any future "admin/exams/<pk>/" route — "options" and
    # "readiness" would otherwise be captured as an id by a greedy converter.
    path(
        "admin/exams/options/",
        studio_views.ExamOptionsView.as_view(), name="studio-exam-options",
    ),
    path(
        "admin/exams/",
        studio_views.ExamCreateView.as_view(), name="studio-exam-create",
    ),
    path("admin/nav-menu/", studio_views.NavMenuListView.as_view(), name="studio-nav-menu"),
    path(
        "admin/nav-menu/reorder/",
        studio_views.NavMenuReorderView.as_view(), name="studio-nav-menu-reorder",
    ),
    path(
        "admin/nav-menu/adopt/",
        studio_views.NavMenuAdoptView.as_view(), name="studio-nav-menu-adopt",
    ),
    path(
        "admin/nav-menu/<int:pk>/",
        studio_views.NavMenuDetailView.as_view(), name="studio-nav-menu-detail",
    ),
    path("admin/labels/", studio_views.LabelListView.as_view(), name="studio-labels"),
    path("admin/labels/merge/", studio_views.LabelMergeView.as_view(), name="studio-labels-merge"),
    path(
        "admin/labels/<slug:kind>/<int:pk>/",
        studio_views.LabelDetailView.as_view(), name="studio-label-detail",
    ),
    path("admin/media/", studio_views.MediaListView.as_view(), name="studio-media"),
    path("admin/media/<int:pk>/", studio_views.MediaDetailView.as_view(), name="studio-media-detail"),
    path("admin/inbox/", studio_views.InboxView.as_view(), name="studio-inbox"),
    path("admin/calendar/", studio_views.CalendarView.as_view(), name="studio-calendar"),
    path("admin/search/", studio_views.StudioSearchView.as_view(), name="studio-search"),
    # Redirects (Admin v2 Phase 5). The resolver is PUBLIC and unauthenticated
    # by necessity — the SPA's catch-all calls it for anonymous visitors
    # before rendering a 404. It exposes a destination and nothing else.
    path("redirects/resolve/", redirect_views.RedirectResolveView.as_view(),
         name="redirect-resolve"),
    # Forms — a DIRECTORY of the site's real forms, not a builder. Contact
    # enquiries are worked here because nothing else surfaces them.
    path("admin/forms/", forms_views.AdminFormDirectoryView.as_view(),
         name="admin-forms"),
    path("admin/forms/contact/", forms_views.AdminContactSubmissionsView.as_view(),
         name="admin-form-contact"),
    path("admin/forms/contact/<int:pk>/",
         forms_views.AdminContactSubmissionDetailView.as_view(),
         name="admin-form-contact-detail"),
    path("admin/redirects/", redirect_views.AdminRedirectListView.as_view(),
         name="admin-redirects"),
    path("admin/redirects/<int:pk>/", redirect_views.AdminRedirectDetailView.as_view(),
         name="admin-redirect-detail"),
    path("admin/activity/", studio_views.ActivityFeedView.as_view(), name="studio-activity"),
    path(
        "admin/revisions/<int:pk>/restore/",
        studio_views.RevisionRestoreView.as_view(), name="studio-revision-restore",
    ),
    path(
        "admin/pages/<slug:key>/draft/",
        studio_views.PageDraftView.as_view(), name="studio-page-draft",
    ),
    path(
        "admin/pages/<slug:key>/publish/",
        studio_views.PagePublishView.as_view(), name="studio-page-publish",
    ),
]
