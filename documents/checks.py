from pathlib import Path
from django.conf import settings
from django.core.checks import Error, register


@register()
def research_storage_check(app_configs, **kwargs):
    private = Path(settings.RESEARCH_PRIVATE_ROOT).resolve()
    media = Path(settings.MEDIA_ROOT).resolve()
    if private == media or media in private.parents:
        return [Error("RESEARCH_PRIVATE_ROOT must be outside public MEDIA_ROOT.", id="documents.E001")]
    return []
