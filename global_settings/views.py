"""
global_settings/views.py  (NEW FILE)

GET  /api/admin/settings/  → current global settings (secret redacted)
PATCH /api/admin/settings/ → update payment mode / creds / contact

Staff-gated, reusing the same IsAdmin permission the other admin endpoints use
(accounts.permissions.IsAdmin → checks request.user.is_staff).
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated

from accounts.permissions import IsAdmin
from .models import GlobalSettings
from .serializers import GlobalSettingsSerializer


class PublicConfigView(APIView):
    """GET /api/public-config/ — the handful of flags an ANONYMOUS visitor
    needs before the marketing site can decide what to render.

    This exists because feature flags previously reached the apps only through
    ``feature_flags`` on ``/accounts/me/``, which requires a login. The public
    Quiz Hub at /quiz is browsable by guests, so gating it on an authenticated
    endpoint would mean either showing every visitor the placeholder or
    shipping the real page to everyone regardless of the switch.

    ⚠ ALLOWLIST, NEVER A SERIALIZER DUMP. GlobalSettings holds the Razorpay
    key id, the platform UPI payee, the contact email and the whole live-session
    rule set. Returning the model's serializer here would publish all of it to
    anyone with curl. Only the names in PUBLIC_FLAGS below are ever emitted, and
    anything added to that tuple is a deliberate decision to make it public.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    PUBLIC_FLAGS = ("public_quiz_hub_enabled",)

    def get(self, request):
        gs = GlobalSettings.load()
        return Response({name: getattr(gs, name) for name in self.PUBLIC_FLAGS})


class AdminGlobalSettingsView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        obj = GlobalSettings.load()
        return Response(GlobalSettingsSerializer(obj).data)

    def patch(self, request):
        obj = GlobalSettings.load()
        serializer = GlobalSettingsSerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # content.permissions.IsStudioEditor caches content_studio_enabled for a
        # minute, since it is read on every Studio request. Drop it here so an
        # admin who just flipped the switch sees it take effect immediately
        # rather than at the next TTL boundary.
        from django.core.cache import cache

        from content.permissions import IsStudioEditor
        cache.delete(IsStudioEditor.CACHE_KEY)

        # Re-serialize from the saved row so effective_mode/flags are fresh.
        return Response(GlobalSettingsSerializer(GlobalSettings.load()).data,
                        status=status.HTTP_200_OK)
