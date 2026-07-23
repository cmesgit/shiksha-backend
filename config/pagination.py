"""Shared DRF pagination for gradual per-endpoint adoption.

Not wired up as DEFAULT_PAGINATION_CLASS on purpose: pagination changes the
list response shape from a bare array to {count, next, previous, results},
and most frontend consumers still expect bare arrays. Opt endpoints in one
at a time, updating their frontend callers in the same release.

Usage:
    from config.pagination import StandardPagination

    class MyListView(generics.ListAPIView):
        pagination_class = StandardPagination
"""
from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
