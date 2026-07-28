# PLACEMENT: backend/content/sitemaps.py
#
# Mounted at config/urls.py's "sitemap.xml" (see CONTENT_SITEMAPS below).
# This API lives on api.shikshacom.com, but every <loc> must point at the
# marketing site (www.shikshacom.com / dev.shikshacom.com). Sitemap._urls()
# unconditionally builds each <loc> as f"{protocol}://{domain}{location}" —
# it does NOT check whether location() already returned an absolute URL, so
# just returning one (as an earlier version of this file did) doubles up
# into "https://api...https://dev...". The correct fix is to force the
# domain/protocol Django uses via get_urls(), while location() stays a plain
# relative path exactly as get_absolute_url() already returns.

from urllib.parse import urlparse

from django.conf import settings
from django.contrib.sitemaps import Sitemap

from courses.models import Course

from .models import BlogPost, CurrentAffair

_frontend = urlparse(settings.FRONTEND_BASE_URL)


class _FrontendSite:
    domain = _frontend.netloc


class FrontendSitemap(Sitemap):
    def get_urls(self, page=1, site=None, protocol=None):
        return super().get_urls(page=page, site=_FrontendSite(), protocol=_frontend.scheme)


class BlogPostSitemap(FrontendSitemap):
    changefreq = "monthly"
    priority = 0.7

    def items(self):
        return BlogPost.objects.published()

    def lastmod(self, obj):
        return obj.updated_at


class CurrentAffairSitemap(FrontendSitemap):
    changefreq = "daily"
    priority = 0.5

    def items(self):
        return CurrentAffair.objects.published()

    def lastmod(self, obj):
        return obj.updated_at


class CourseSitemap(FrontendSitemap):
    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return Course.objects.filter(status=Course.STATUS_PUBLISHED).order_by("slug")


CONTENT_SITEMAPS = {
    "blog": BlogPostSitemap,
    "current-affairs": CurrentAffairSitemap,
    "courses": CourseSitemap,
}
