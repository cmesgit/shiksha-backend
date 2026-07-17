# PLACEMENT: backend/content/sitemaps.py
#
# Optional SEO sitemaps. To enable, add to config/settings*.py:
#     INSTALLED_APPS += ["django.contrib.sitemaps"]
# and to config/urls.py:
#     from django.contrib.sitemaps.views import sitemap
#     from content.sitemaps import CONTENT_SITEMAPS
#     urlpatterns += [
#         path("sitemap.xml", sitemap, {"sitemaps": CONTENT_SITEMAPS},
#              name="django.contrib.sitemaps.views.sitemap"),
#     ]
#
# NOTE: locations are frontend paths (get_absolute_url), served from the
# www domain. If the API lives on api.shikshacom.com, set the request's
# host correctly at the proxy or expose sitemap.xml through the www vhost.

from django.contrib.sitemaps import Sitemap

from .models import BlogPost, CurrentAffair


class BlogPostSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.7

    def items(self):
        return BlogPost.objects.published()

    def lastmod(self, obj):
        return obj.updated_at


class CurrentAffairSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.5

    def items(self):
        return CurrentAffair.objects.published()

    def lastmod(self, obj):
        return obj.updated_at


CONTENT_SITEMAPS = {
    "blog": BlogPostSitemap,
    "current-affairs": CurrentAffairSitemap,
}
