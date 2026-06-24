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
from rest_framework.permissions import IsAuthenticated

from accounts.permissions import IsAdmin
from .models import GlobalSettings
from .serializers import GlobalSettingsSerializer


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
        # Re-serialize from the saved row so effective_mode/flags are fresh.
        return Response(GlobalSettingsSerializer(GlobalSettings.load()).data,
                        status=status.HTTP_200_OK)
