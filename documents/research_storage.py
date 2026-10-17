"""Manuscripts never use public MEDIA_ROOT or the default CDN storage."""
import uuid
from pathlib import Path
from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible
from django.utils.functional import cached_property


@deconstructible
class ResearchPrivateStorage(FileSystemStorage):
    @cached_property
    def base_location(self):
        return str(getattr(settings, "RESEARCH_PRIVATE_ROOT", Path(settings.BASE_DIR) / "private_research"))

    def _clear_cached_properties(self, setting, **kwargs):
        if setting == "RESEARCH_PRIVATE_ROOT":
            self.__dict__.pop("base_location", None)
            self.__dict__.pop("location", None)
        super()._clear_cached_properties(setting=setting, **kwargs)

    def url(self, name):
        raise ValueError("Research manuscripts require an authorized API download.")


def research_file_path(instance, filename):
    return f"{instance.submission_id}/{uuid.uuid4().hex}.pdf"
