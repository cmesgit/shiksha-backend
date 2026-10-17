import logging
from celery import shared_task
from django.db import transaction
from .models import ResearchSubmission
from .research_workflow import LIVE_STATES, maintain_paper

logger = logging.getLogger(__name__)


@shared_task(name="documents.sweep_research_reviews")
def sweep_research_reviews():
    totals = {"papers": 0, "expired": 0, "reminded": 0, "invited": 0, "failed": 0}
    ids = ResearchSubmission.objects.filter(status__in=LIVE_STATES).order_by("pk").values_list("pk", flat=True)
    for pk in ids.iterator(chunk_size=100):
        try:
            with transaction.atomic():
                paper = ResearchSubmission.objects.select_for_update().get(pk=pk)
                outcome = maintain_paper(paper)
                totals["papers"] += 1
                for key, value in outcome.items():
                    totals[key] += value
        except Exception:
            totals["failed"] += 1
            logger.exception("Research deadline sweep failed for paper %s", pk)
    return totals
