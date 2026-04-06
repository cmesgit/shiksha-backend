import requests
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.views.decorators.cache import cache_page
from django.conf import settings

GNEWS_API_KEY = settings.GNEWS_API_KEY
GNEWS_BASE_URL = "https://gnews.io/api/v4"


def fetch_gnews(endpoint, params):
    """Helper to call GNews API and return parsed JSON."""
    params["token"] = GNEWS_API_KEY
    params["lang"] = "en"
    params["max"] = params.get("max", 9)

    try:
        response = requests.get(
            f"{GNEWS_BASE_URL}/{endpoint}", params=params, timeout=8)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": str(e), "articles": []}


@require_GET
@cache_page(60 * 30)  # Cache for 30 minutes to save API quota
def top_headlines(request):
    """
    GET /api/news/top-headlines/
    Returns top general headlines.
    Optional query param: ?max=9
    """
    max_articles = int(request.GET.get("max", 9))
    data = fetch_gnews(
        "top-headlines", {"max": max_articles, "topic": "breaking-news"})

    articles = [
        {
            "title": a.get("title", ""),
            "description": a.get("description", ""),
            "url": a.get("url", ""),
            "image": a.get("image", ""),
            "publishedAt": a.get("publishedAt", ""),
            "source": a.get("source", {}).get("name", "Unknown"),
            "category": "General",
        }
        for a in data.get("articles", [])
    ]

    return JsonResponse({"articles": articles, "totalArticles": data.get("totalArticles", 0)})


@require_GET
@cache_page(60 * 30)  # Cache for 30 minutes to save API quota
def education_news(request):
    """
    GET /api/news/education/
    Returns education-related news articles.
    Optional query param: ?max=9
    """
    max_articles = int(request.GET.get("max", 9))
    data = fetch_gnews(
        "search", {"q": "education school university learning", "max": max_articles})

    articles = [
        {
            "title": a.get("title", ""),
            "description": a.get("description", ""),
            "url": a.get("url", ""),
            "image": a.get("image", ""),
            "publishedAt": a.get("publishedAt", ""),
            "source": a.get("source", {}).get("name", "Unknown"),
            "category": "Education",
        }
        for a in data.get("articles", [])
    ]

    return JsonResponse({"articles": articles, "totalArticles": data.get("totalArticles", 0)})
