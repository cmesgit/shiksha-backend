# PLACEMENT: backend/backend/accounts/skill_file_view.py   (NEW FILE — FILE 3 of 3)
#
# WHY: the teacher Private Details page saves an updated certificate with
# PATCH /accounts/teacher/skill/<application_id>/  (multipart, field
# "skill_file") — no such route existed, so the file part of "Save" silently
# 404'd while the rest of the form saved, and the teacher believed the upload
# worked. The SkillApplication model (accounts.models, related_name
# skill_applications, FileField supporting_file) already holds the file; this
# endpoint just accepts the update the page has been sending all along.

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework import status


class SkillApplicationFileView(APIView):
    """PATCH /api/accounts/teacher/skill/<application_id>/
    Body (multipart): skill_file=<file>
    Only the application's own teacher may update it."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def patch(self, request, application_id):
        tp = getattr(request.user, "teacher_profile", None)
        if tp is None:
            return Response(
                {"detail": "No teacher identity on this account."},
                status=status.HTTP_403_FORBIDDEN,
            )

        app_obj = tp.skill_applications.filter(id=application_id).first()
        if app_obj is None:
            return Response(
                {"detail": "Skill application not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        file = request.FILES.get("skill_file") or request.FILES.get("supporting_file")
        if not file:
            return Response(
                {"detail": "skill_file is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Replace the stored file (drop the old blob so storage doesn't leak).
        if app_obj.supporting_file:
            try:
                app_obj.supporting_file.delete(save=False)
            except Exception:
                pass
        app_obj.supporting_file = file
        app_obj.save(update_fields=["supporting_file"])

        return Response({
            "id": str(app_obj.id),
            "skill_supporting_file": app_obj.supporting_file.url,
        })
