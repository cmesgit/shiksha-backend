"""The news wire's freshness signal.

The Current Affairs page tells readers headlines are "updated through the
day" and had nothing to back it with. The response is cached for 30 minutes
(`@cache_page`), so the figure on screen could be half an hour old with no
way for a reader — or for us — to tell.

`fetchedAt` is stamped inside the cached body, which is the whole point: a
timestamp computed per-request would be served fresh alongside stale
articles, which is worse than no timestamp at all.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

URL = "/api/news/top-headlines/"

_GNEWS = {
    "totalArticles": 1,
    "articles": [{
        "title": "A headline",
        "description": "Something happened.",
        "url": "https://example.invalid/1",
        "image": "",
        "publishedAt": "2026-11-13T09:15:00Z",
        "source": {"name": "CNBC"},
    }],
}


class TopHeadlinesFreshnessTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_response_carries_a_fetched_at(self):
        with patch("news.views.fetch_gnews", return_value=_GNEWS):
            body = self.client.get(URL).json()
        self.assertIn("fetchedAt", body)
        self.assertTrue(body["fetchedAt"], "fetchedAt must not be empty")
        self.assertFalse(body["stale"])

    def test_fetched_at_is_the_fetch_time_not_the_request_time(self):
        """The regression that matters. A per-request timestamp would make a
        30-minute-old cached payload advertise itself as current."""
        with patch("news.views.fetch_gnews", return_value=_GNEWS):
            first = self.client.get(URL).json()["fetchedAt"]
            # Second call is served from @cache_page — fetch_gnews is not
            # re-entered, so the stamp must be byte-identical.
            second = self.client.get(URL).json()["fetchedAt"]
        self.assertEqual(first, second)

    def test_an_upstream_failure_is_marked_stale(self):
        """fetch_gnews swallows RequestException into {"error":…,"articles":[]},
        so the page cannot otherwise tell "no news" from "GNews is down"."""
        with patch("news.views.fetch_gnews",
                   return_value={"error": "boom", "articles": []}):
            body = self.client.get(URL).json()
        self.assertTrue(body["stale"])
        self.assertEqual(body["articles"], [])

    def test_articles_still_have_their_original_shape(self):
        with patch("news.views.fetch_gnews", return_value=_GNEWS):
            body = self.client.get(URL).json()
        a = body["articles"][0]
        self.assertEqual(
            set(a),
            {"title", "description", "url", "image", "publishedAt",
             "source", "category"},
        )
        self.assertEqual(a["source"], "CNBC")
        # Still hardcoded — this is WHY the page cannot category-filter the
        # wire, and the frontend copy says so. Pinned so a future change to
        # real categories is a deliberate one.
        self.assertEqual(a["category"], "General")
