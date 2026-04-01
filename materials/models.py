import uuid
from django.db import models
from django.conf import settings
from courses.models import Chapter


class StudyMaterial(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    chapter = models.ForeignKey(
        Chapter,
        on_delete=models.CASCADE,
        related_name="materials"
    )

    title = models.CharField(max_length=255)

    description = models.TextField(blank=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="uploaded_materials"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class MaterialFile(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    material = models.ForeignKey(
        StudyMaterial,
        on_delete=models.CASCADE,
        related_name="files"
    )

    file = models.FileField(upload_to="study_materials/")

    uploaded_at = models.DateTimeField(auto_now_add=True)

    def filename(self):
        return self.file.name.split("/")[-1]

    def __str__(self):
        return self.filename()
